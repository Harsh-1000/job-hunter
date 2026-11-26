"""Job-specific referral request messages (template-based, no AI, nothing leaves the server).

For one job + one user's resume it builds:
    note      LinkedIn connection-request note, always <= NOTE_LIMIT characters
    message   the full referral ask (send after they accept, or as DM / email)
    followup  a short nudge for a few days later
    subject   an email subject line
plus LinkedIn people-search links to find employees / recruiters at the company.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from .resume import ResumeProfile

NOTE_LIMIT = 200          # LinkedIn's limit for connection notes on free accounts
GENERIC_TITLE_WORDS = {"senior", "sr", "junior", "jr", "lead", "i", "ii", "iii", "iv", "-", "–", "|", "(", ")"}


@dataclass
class Referral:
    note: str
    message: str
    followup: str
    subject: str
    employees_url: str
    recruiters_url: str


def _years_text(years: float | None) -> str:
    if years is None or years < 0.5:
        return ""
    return f"{int(years)}+ years" if years >= 1 else "1 year"


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def clean_company(company: str) -> str:
    c = re.sub(r"\s+", " ", company or "").strip()
    return re.sub(r"\s*(pvt\.?|private|ltd\.?|limited|inc\.?|llc|llp)\b\.?", "", c, flags=re.I).strip(" ,.") or c


def people_search_url(*words: str) -> str:
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(" ".join(w for w in words if w))


def _role_words(title: str) -> str:
    words = [w for w in re.split(r"[\s/,]+", title)
             if w.lower() not in GENERIC_TITLE_WORDS and re.fullmatch(r"[A-Za-z][A-Za-z.+#]*", w)]
    return " ".join(words[:3])


def build(title: str, company: str, job_id: str, url: str, matched: list[str],
          profile: ResumeProfile | None, name: str, my_years: float | None, fallback_role: str = "") -> Referral:
    title = re.sub(r"\s+", " ", title or "this role").strip()
    comp = clean_company(company) or "your company"
    name = (name or "").strip()
    role = (profile.titles[0] if profile and profile.titles else "") or fallback_role or "Software Engineer"
    role = re.sub(r"^(senior|sr\.?|junior|jr\.?|lead) ", "", role, flags=re.I)
    years = _years_text(my_years)
    skills = list(dict.fromkeys(matched or (profile.skills if profile else [])))
    me = f"I'm {name}, a {role}" if name else f"I'm a {role}"

    # --- connection note: try richer versions first, keep the first that fits ---
    candidates = []
    for n_sk in (2, 1, 0):
        sk = "/".join(skills[:n_sk])
        bits = ", ".join(x for x in [years, sk] if x)
        about = f"{me}{f' ({bits})' if bits else ''}"
        candidates.append(f"Hi [Name], {about}. I saw the {title} role at {comp} (Job ID {job_id}). "
                          f"Would you be open to referring me? Happy to share my resume. Thanks!")
        candidates.append(f"Hi [Name], {about}. Saw the {title} role at {comp} (ID {job_id}). "
                          f"Could you refer me? Thanks!")
    candidates.append(f"Hi, I'd love a referral for the {title} role at {comp} (ID {job_id}). Could you help? Thanks!")
    note = next((c for c in candidates if len(c) <= NOTE_LIMIT), candidates[-1][:NOTE_LIMIT - 1] + "…")

    # --- full message ---
    exp = f" with {years} of experience" if years else ""
    # your strongest skills overall; the job-specific ones get their own sentence below
    own = [s for s in (profile.skills if profile else []) if s not in matched[:3]] or skills
    sk4 = _join(own[:4])
    lines = [
        "Hi [Name],",
        "",
        f"I hope you're doing well! I came across the {title} opening at {comp} and I'm really excited about it:",
        url,
        "",
        f"{me}{exp}" + (f", working mainly with {sk4}." if sk4 else "."),
    ]
    if matched:
        lines.append(f"The role asks for {_join(matched[:3])}, which I've used hands-on in my current work, "
                     "so I believe I'd be a strong fit.")
    lines += [
        "",
        f"Would you be comfortable referring me for this position? I'm happy to send my resume and anything "
        f"else that makes it easier for you.",
        "",
        f"Job ID: {job_id}",
        "",
        "Thank you so much for your time!",
        name or "[Your name]",
    ]
    message = "\n".join(lines)

    followup = (f"Hi [Name], just following up on my note about the {title} role at {comp} (Job ID {job_id}). "
                "I know you're busy, so no worries if it's not possible. If you could refer me, I'd really "
                f"appreciate it, and I'm happy to share my resume. Thanks again!{' ' + name if name else ''}")
    subject = f"Referral request: {title} at {comp} (Job ID {job_id})"
    rw = _role_words(title)
    return Referral(note=note, message=message, followup=followup, subject=subject,
                    employees_url=people_search_url(comp, rw),
                    recruiters_url=people_search_url(comp, "recruiter talent acquisition"))
