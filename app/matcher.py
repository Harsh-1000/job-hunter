"""Match one job against one user's resume + filters.

Hard filters (job is skipped, with a reason):
    title contains an excluded word · excluded company · too many applicants ·
    asks for more years than allowed · fresher role well below your experience ·
    title unrelated to your keywords (and the description doesn't fit your skills either) ·
    duplicate of a job already sent (checked by the scanner)
Score 0-100 (shown as "Match %"):
    65%  skill coverage  - of the skills the job description asks for, how many are on your resume
                           (skills named in the title or mentioned twice count double)
    20%  title relevance - how well the job title fits one of your search keywords
    15%  experience fit  - full marks when the job asks for <= your years, -30% per extra year
Then the user's threshold: score < min_score -> skipped.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .filters import Filters
from .linkedin import applicants_count
from .resume import ResumeProfile
from .skills import BOUNDARY, find_skills

W_SKILLS, W_TITLE, W_EXP = 0.65, 0.20, 0.15

NUM = r"(\d{1,2}(?:\.\d)?)"
YEARS = r"\s*\+?\s*(?:years?|yrs?)"
RANGE_RE = re.compile(NUM + r"\s*\+?\s*(?:-|–|—|to)\s*" + NUM + YEARS, re.I)
SINGLE_RE = re.compile(NUM + YEARS, re.I)
EXP_CONTEXT = re.compile(r"experience|\bexp\b|relevant|industry|professional|work(?:ing)?", re.I)
WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10}

# ---------- years ----------


def _near_experience(text: str, m: re.Match) -> bool:
    return bool(EXP_CONTEXT.search(text[max(0, m.start() - 60): m.end() + 60]))


def required_years(text: str) -> tuple[float | None, float | None, str]:
    """(min, max, label) of experience a description asks for. Largest minimum wins
    ("5+ years overall, 2 years of Kafka" -> 5+)."""
    text = text or ""
    found: list[tuple[float, float | None]] = []
    spans = []
    for m in RANGE_RE.finditer(text):
        spans.append(m.span())
        lo, hi = float(m.group(1)), float(m.group(2))
        if _near_experience(text, m) and lo <= hi <= 30:
            found.append((lo, hi))
    for m in SINGLE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in spans) or not _near_experience(text, m):
            continue
        if float(m.group(1)) <= 30:
            found.append((float(m.group(1)), None))
    if not found:
        for word, n in WORD_NUMS.items():
            if re.search(rf"\b{word}\s*\+?\s*(?:years?|yrs?)\b.{{0,40}}experience", text, re.I):
                found.append((float(n), None))
                break
    if not found:
        return None, None, "not specified"
    lo, hi = max(found, key=lambda f: f[0])
    return lo, hi, (f"{lo:g}-{hi:g} yrs" if hi is not None else f"{lo:g}+ yrs")


# ---------- title relevance ----------

GENERIC = {"engineer", "engineers", "developer", "developers", "dev", "specialist", "programmer", "sr", "jr",
           "senior", "junior", "i", "ii", "iii", "iv", "role", "remote", "the", "and", "of", "for", "with", "in",
           "-", "&"}
NORMALISE = [
    (r"artificial intelligence", "ai"), (r"machine learning", "ml"), (r"generative ai|gen ai|gen-ai", "genai"),
    (r"large language models?|llms", "llm"), (r"back[- ]end", "backend"), (r"front[- ]end", "frontend"),
    (r"full[- ]stack|fullstack", "fullstack"), (r"node\.?js", "node"), (r"\bsde\b", "software"),
    (r"software development engineer", "software"), (r"ai/ml|ai-ml", "ai ml"), (r"\.net", "dotnet"),
]
IMPLIES = {"genai": {"ai"}, "llm": {"ai", "genai"}, "ml": {"ai"}, "fullstack": {"backend", "frontend"},
           "agentic": {"ai"}, "nlp": {"ai", "ml"}}


def _tokens(text: str) -> set[str]:
    t = text.lower()
    for pat, rep in NORMALISE:
        t = re.sub(pat, rep, t)
    toks = set(re.findall(r"[a-z0-9+#]+", t))
    for tok in list(toks):
        toks |= IMPLIES.get(tok, set())
    return toks


def title_relevance(title: str, keywords: list[str]) -> float:
    """0..1: best overlap between the job title and any of the user's keywords."""
    if not keywords:
        return 1.0
    title_toks = _tokens(title)
    best = 0.0
    for kw in keywords:
        sig = {t for t in _tokens(kw) if t not in GENERIC}
        if not sig:   # keyword like "Engineer": any engineer/developer title fits
            best = max(best, 1.0 if title_toks & GENERIC else 0.3)
            continue
        best = max(best, len(sig & title_toks) / len(sig))
    return round(best, 2)


def excluded_word(title: str, words: list[str]) -> str | None:
    for w in words:
        w = w.strip()
        if w and re.search(BOUNDARY.format(re.escape(w)), title, re.I):
            return w
    return None


# ---------- result ----------


@dataclass
class Match:
    ok: bool
    score: int | None = None
    reason: str = ""
    code: str = ""            # title | company | applicants | years | fresher | unrelated | threshold
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    jd_skill_count: int = 0
    years_text: str = "not specified"
    years_min: float | None = None
    title_rel: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate(title: str, company: str, description: str, applicants: str,
             profile: ResumeProfile | None, f: Filters) -> Match:
    title, description = title or "", description or ""

    w = excluded_word(title, f.exclude_words)
    if w:
        return Match(False, reason=f"title has '{w}'", code="title")
    for c in f.exclude_companies:
        if c.strip() and c.strip().lower() in (company or "").lower():
            return Match(False, reason=f"company '{c}' excluded", code="company")
    n_app = applicants_count(applicants)
    if f.max_applicants and n_app is not None and n_app > f.max_applicants:
        return Match(False, reason=f"{n_app}+ applicants", code="applicants")

    resume_years = profile.years if profile else None
    my_years = f.years(resume_years)
    lo, hi, label = required_years(description)
    max_allowed = f.max_years_allowed(resume_years)
    if lo is not None and max_allowed is not None and lo > max_allowed:
        return Match(False, reason=f"needs {label}", code="years", years_text=label, years_min=lo)
    if hi is not None and my_years >= 2 and hi + 1 < my_years:
        return Match(False, reason=f"fresher role ({label})", code="fresher", years_text=label, years_min=lo)

    rel = title_relevance(title, f.keywords)

    if not profile:  # no resume yet: keyword relevance only, no score
        if rel == 0:
            return Match(False, reason="title doesn't match your keywords", code="unrelated", title_rel=rel)
        return Match(True, score=None, years_text=label, years_min=lo, title_rel=rel)

    jd = find_skills(f"{title}\n{description}")
    title_skills = set(find_skills(title))
    weights = {s: (2 if s in title_skills or n >= 2 else 1) for s, n in jd.items()}
    mine = set(profile.skills)
    total = sum(weights.values())
    got = sum(wt for s, wt in weights.items() if s in mine)
    coverage = got / total if total else 0.25   # no tech skills named: unknown, lean low
    if 0 < total < 4:   # very short description: don't over-trust a couple of keywords
        coverage = (coverage * total + 0.4 * (4 - total)) / 4

    exp_fit = 1.0 if lo is None or lo <= my_years else max(0.0, 1 - 0.3 * (lo - my_years))
    score = round(100 * (W_SKILLS * coverage + W_TITLE * rel + W_EXP * exp_fit))

    by_weight = sorted(weights, key=lambda s: (-weights[s], s))
    m = Match(True, score=score, matched=[s for s in by_weight if s in mine][:10],
              missing=[s for s in by_weight if s not in mine][:6], jd_skill_count=len(jd),
              years_text=label, years_min=lo, title_rel=rel)
    if rel == 0 and (total == 0 or coverage < 0.4):
        m.ok, m.reason, m.code = False, "unrelated to your keywords and skills", "unrelated"
    elif score < f.min_score:
        m.ok, m.reason, m.code = False, f"match {score}% < your {f.min_score}% threshold", "threshold"
    return m
