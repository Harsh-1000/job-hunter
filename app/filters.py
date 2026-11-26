"""Per-user search filters: what to search on LinkedIn and what to keep afterwards.

LinkedIn-side filters (sent as URL parameters, same codes the LinkedIn website uses):
    keywords, location, f_TPR (date posted), f_E (experience level), f_JT (job type),
    f_WT (on-site/remote/hybrid), f_AL (Easy Apply)
Bot-side filters (applied after fetching the full job description):
    match threshold, max years required, excluded title words / companies, max applicants, quiet hours
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields

EXPERIENCE_LEVELS = {"1": "Internship", "2": "Entry level", "3": "Associate", "4": "Mid-Senior",
                     "5": "Director", "6": "Executive"}
JOB_TYPES = {"F": "Full-time", "P": "Part-time", "C": "Contract", "T": "Temporary", "I": "Internship",
             "V": "Volunteer", "O": "Other"}
WORK_TYPES = {"1": "On-site", "2": "Remote", "3": "Hybrid"}
POSTED = {"r3600": "Past hour", "r21600": "Past 6 hours", "r86400": "Past 24 hours",
          "r604800": "Past week", "r2592000": "Past month"}

SENIOR_WORDS = ["senior", "sr", "lead", "principal", "staff", "architect", "manager", "director", "head", "vp"]
MAX_KEYWORDS = 8
MAX_LOCATIONS = 3


@dataclass
class Filters:
    keywords: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=lambda: ["India"])
    posted: str = "r86400"
    experience_levels: list[str] = field(default_factory=lambda: ["2", "3", "4"])
    job_types: list[str] = field(default_factory=list)      # empty = any
    work_types: list[str] = field(default_factory=list)     # empty = any
    easy_apply: bool = False

    min_score: int = 0                     # 0 = send everything
    max_years: float | None = None         # None = auto (your years + 2)
    my_years: float | None = None          # override the years read from the resume
    exclude_words: list[str] = field(default_factory=lambda: list(SENIOR_WORDS))
    exclude_companies: list[str] = field(default_factory=list)
    max_applicants: int | None = None      # None = any
    quiet_start: int | None = None         # hour 0-23, None = no quiet hours
    quiet_end: int | None = None

    # ---- (de)serialisation ----
    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str | None) -> "Filters":
        if not raw:
            return cls()
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    # ---- derived ----
    def years(self, resume_years: float | None) -> float:
        if self.my_years is not None:
            return self.my_years
        return resume_years if resume_years is not None else 0.0

    def max_years_allowed(self, resume_years: float | None) -> float | None:
        if self.max_years is not None:
            return None if self.max_years < 0 else self.max_years
        return self.years(resume_years) + 2

    def in_quiet_hours(self, hour: int) -> bool:
        if self.quiet_start is None or self.quiet_end is None or self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= hour < self.quiet_end
        return hour >= self.quiet_start or hour < self.quiet_end   # wraps midnight, e.g. 23 -> 8


@dataclass(frozen=True)
class Query:
    """One LinkedIn search. Users with identical searches share it (fetched once per cycle)."""
    keyword: str
    location: str
    posted: str
    experience_levels: tuple[str, ...] = ()
    job_types: tuple[str, ...] = ()
    work_types: tuple[str, ...] = ()
    easy_apply: bool = False

    @property
    def key(self) -> str:
        raw = json.dumps([self.keyword.lower(), self.location.lower(), self.posted, sorted(self.experience_levels),
                          sorted(self.job_types), sorted(self.work_types), self.easy_apply])
        return hashlib.sha1(raw.encode()).hexdigest()[:16]

    def params(self, start: int = 0) -> dict[str, str | int]:
        p: dict[str, str | int] = {"keywords": self.keyword, "location": self.location, "f_TPR": self.posted,
                                   "sortBy": "DD", "start": start}
        if self.experience_levels:
            p["f_E"] = ",".join(sorted(self.experience_levels))
        if self.job_types:
            p["f_JT"] = ",".join(sorted(self.job_types))
        if self.work_types:
            p["f_WT"] = ",".join(sorted(self.work_types))
        if self.easy_apply:
            p["f_AL"] = "true"
        return p


def queries_for(f: Filters) -> list[Query]:
    return [Query(kw, loc, f.posted, tuple(f.experience_levels), tuple(f.job_types), tuple(f.work_types),
                  f.easy_apply)
            for kw in f.keywords for loc in (f.locations or ["India"])]


# ---- parsing what users type in the chat ----

def parse_list(text: str, limit: int = 20, max_len: int = 60) -> list[str]:
    """'AI Engineer, GenAI engineer\nLLM' -> ['AI Engineer', 'GenAI engineer', 'LLM'] (deduped)."""
    out: list[str] = []
    for part in re.split(r"[,;\n|]+", text or ""):
        p = re.sub(r"\s+", " ", part).strip()[:max_len]
        if p and p.lower() not in {o.lower() for o in out}:
            out.append(p)
    return out[:limit]


def parse_threshold(text: str) -> int:
    m = re.fullmatch(r"\s*(\d{1,3})\s*%?\s*", text or "")
    if not m or not 0 <= int(m.group(1)) <= 100:
        raise ValueError("Send a number from 0 to 100, e.g. 30")
    return int(m.group(1))


def parse_years(text: str) -> float:
    m = re.fullmatch(r"\s*(\d{1,2}(?:\.\d)?)\s*(?:y|yr|yrs|years?)?\s*", text or "", re.I)
    if not m or float(m.group(1)) > 40:
        raise ValueError("Send years as a number, e.g. 2 or 2.5")
    return float(m.group(1))


def parse_quiet_hours(text: str) -> tuple[int | None, int | None]:
    t = (text or "").strip().lower()
    if t in ("off", "none", "no", "0"):
        return None, None
    m = re.fullmatch(r"(\d{1,2})(?::00)?\s*(?:-|to|–)\s*(\d{1,2})(?::00)?", t)
    if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 24):
        raise ValueError("Send hours like 23-8 (24h clock), or 'off'")
    return int(m.group(1)), int(m.group(2))


def parse_applicants(text: str) -> int | None:
    t = (text or "").strip().lower()
    if t in ("any", "off", "none", "0"):
        return None
    if not t.isdigit():
        raise ValueError("Send a number like 100, or 'any'")
    return int(t)


def summary(f: Filters, resume_years: float | None = None) -> str:
    """Human-readable list of the filters (plain text, caller escapes for HTML)."""
    def names(codes, table):
        return ", ".join(table.get(c, c) for c in codes) or "Any"
    max_y = f.max_years_allowed(resume_years)
    quiet = f"{f.quiet_start:02d}:00–{f.quiet_end:02d}:00" if f.quiet_start is not None else "Off"
    return "\n".join([
        f"🔎 Keywords: {', '.join(f.keywords) or '— (set some!)'}",
        f"📍 Location: {', '.join(f.locations) or 'India'}",
        f"🕒 Posted: {POSTED.get(f.posted, f.posted)}",
        f"🎓 Experience level: {names(f.experience_levels, EXPERIENCE_LEVELS)}",
        f"📄 Job type: {names(f.job_types, JOB_TYPES)}",
        f"🏠 Work type: {names(f.work_types, WORK_TYPES)}",
        f"⚡ Easy Apply only: {'Yes' if f.easy_apply else 'No'}",
        f"🎯 Match threshold: {f.min_score}%" + (" (send all)" if f.min_score == 0 else ""),
        f"👤 My experience: {f.years(resume_years):g} yrs" + (" (set by you)" if f.my_years is not None else ""),
        f"🧮 Max years a job may ask: {'Any' if max_y is None else f'{max_y:g}'}"
        + (" (auto)" if f.max_years is None else ""),
        f"🚫 Skip titles with: {', '.join(f.exclude_words) or '—'}",
        f"🏢 Skip companies: {', '.join(f.exclude_companies) or '—'}",
        f"👥 Max applicants: {f.max_applicants or 'Any'}",
        f"🌙 Quiet hours: {quiet}",
    ])
