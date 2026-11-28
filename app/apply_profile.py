"""The one-time application profile ("📝 Application form"): everything job forms usually ask.

Filled in a Telegram Mini App (docs/apply-form/index.html), sent back with Telegram.WebApp.sendData
(max 4096 bytes), strictly validated here and stored in data/users/<id>/apply_profile.json (0600).
Used by ✍️ Prepare (Claude writes answers) and by auto-apply (Claude fills forms on the sites you chose).
"""
from __future__ import annotations

import re
from datetime import date
from dataclasses import asdict, dataclass, field, fields

from .apply_sites import DEFAULT_DAILY_CAP, MAX_DAILY_CAP, MODES, SITES, default_modes

MAX_FORM_BYTES = 4096
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{6,18}[0-9]$")
NOTICE = ("immediate", "15", "30", "60", "90")
YES_NO = ("yes", "no")
WORK_MODES = ("any", "remote", "hybrid", "onsite")
DISABILITY = ("", "no", "yes")
EDU_LEVELS = {"phd": "PhD", "masters": "Master's", "bachelors": "Bachelor's", "diploma": "Diploma",
              "class12": "Class XII", "class10": "Class X", "other": "Other"}
MAX_EDUCATION = 6
MARKS_RE = re.compile(r"\d{1,3}(\.\d{1,2})?\s*(%|/\s*10|/\s*4)?")

# field -> max length (text fields)
TEXT = {"full_name": 60, "email": 120, "phone": 20, "city": 60, "linkedin": 150, "github": 150,
        "portfolio": 150, "current_company": 80, "current_title": 80, "preferred_locations": 120,
        "work_auth": 80, "gender": 20, "why_default": 400, "cover_note": 600,
        "pincode": 6, "headline": 120, "summary": 400, "key_skills": 200, "languages": 80}
MULTILINE = ("why_default", "cover_note", "summary")
URLS = ("linkedin", "github", "portfolio")


@dataclass
class ApplyProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    pincode: str = ""
    dob: str = ""                      # YYYY-MM-DD (some Indian forms ask; optional)
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    current_company: str = ""
    current_title: str = ""
    total_years: float | None = None
    relevant_years: float | None = None
    headline: str = ""                 # one line, like a Naukri resume headline
    summary: str = ""                  # 2-4 line profile summary
    key_skills: str = ""
    languages: str = ""
    work_mode: str = "any"             # any | remote | hybrid | onsite
    notice: str = "30"                 # immediate | 15 | 30 | 60 | 90 (days)
    current_ctc_lpa: float | None = None
    expected_ctc_lpa: float | None = None
    relocate: str = "yes"
    preferred_locations: str = ""
    work_auth: str = "Indian citizen"
    sponsorship: str = "no"
    # [{level, degree, college, start_year, end_year, score}], newest first; "+ Add education" in the form
    education: list[dict] = field(default_factory=list)
    gender: str = ""
    disability: str = ""               # "" (prefer not to say) | no | yes
    why_default: str = ""
    cover_note: str = ""
    sites: dict[str, str] = field(default_factory=default_modes)
    daily_cap: int = DEFAULT_DAILY_CAP
    consent: bool = False             # "Claude may fill and submit applications for me on Ask/Auto sites"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ApplyProfile | None":
        if not d:
            return None
        known = {f.name for f in fields(cls)}
        p = cls(**{k: v for k, v in d.items() if k in known})
        if not p.education:
            p.education = _old_education(d)
        p.sites = {**default_modes(), **{k: v for k, v in (p.sites or {}).items() if k in SITES and v in MODES}}
        return p

    # ---- derived ----
    def missing(self) -> list[str]:
        """Fields almost every application asks for."""
        need = {"full_name": "name", "email": "email", "phone": "phone", "city": "city"}
        out = [label for f, label in need.items() if not getattr(self, f)]
        if self.total_years is None:
            out.append("years of experience")
        return out

    def active_sites(self) -> dict[str, str]:
        return {k: m for k, m in self.sites.items() if m != "off"} if self.consent else {}

    def for_claude(self) -> dict:
        """What Claude may see to write answers: no contact details or birth date (not needed to write text)."""
        d = self.to_dict()
        for k in ("email", "phone", "pincode", "dob", "disability", "sites", "daily_cap", "consent"):
            d.pop(k, None)
        return {k: v for k, v in d.items() if v not in ("", None)}


def _old_education(d: dict) -> list[dict]:
    """Forms saved before education became a list: degree / college / grad_year / cgpa / twelfth / tenth."""
    out = []
    if d.get("degree") or d.get("college"):
        out.append({"level": "bachelors", "degree": d.get("degree") or "", "college": d.get("college") or "",
                    "start_year": None, "end_year": d.get("grad_year"), "score": d.get("cgpa") or ""})
    for key, level in (("twelfth", "class12"), ("tenth", "class10")):
        if d.get(key):
            out.append({"level": level, "degree": "", "college": "", "start_year": None, "end_year": None,
                        "score": d[key]})
    return out


def _education(items, errors: list[str]) -> list[dict]:
    if items in (None, ""):
        return []
    if not isinstance(items, list):
        errors.append("education must be a list")
        return []
    if len(items) > MAX_EDUCATION:
        errors.append(f"at most {MAX_EDUCATION} education entries")
        return []
    out = []
    for i, e in enumerate(items, 1):
        if not isinstance(e, dict):
            errors.append(f"education {i}: must be an object")
            continue
        vals = {k: str(e.get(k) or "").strip() for k in ("level", "degree", "college", "start_year", "end_year",
                                                          "score")}
        vals = {k: re.sub(r"[\x00-\x1f\x7f]", " ", v) for k, v in vals.items()}
        if not any(vals[k] for k in ("degree", "college", "end_year", "score")):
            continue                                   # an empty row the user added and didn't fill
        level = vals["level"] or "other"
        if level not in EDU_LEVELS:
            errors.append(f"education {i}: unknown level")
            continue
        for k, n in (("degree", 80), ("college", 120)):
            if len(vals[k]) > n:
                errors.append(f"education {i}: {k} is too long (max {n})")
        if vals["score"] and not MARKS_RE.fullmatch(vals["score"]):
            errors.append(f"education {i}: score should look like 8.2, 8.2/10 or 76%")
        label = f"education {i}"
        start = _num(vals["start_year"], 1960, 2035, f"{label}: start year", errors, integer=True)
        end = _num(vals["end_year"], 1960, 2035, f"{label}: end year", errors, integer=True)
        if start and end and start > end:
            errors.append(f"{label}: start year is after end year")
        out.append({"level": level, "degree": vals["degree"][:80], "college": vals["college"][:120],
                    "start_year": start, "end_year": end, "score": vals["score"][:12]})
    return out


def _num(v, lo: float, hi: float, name: str, errors: list[str], integer: bool = False):
    if v in (None, ""):
        return None
    try:
        n = float(str(v).strip())
    except ValueError:
        errors.append(f"{name} must be a number")
        return None
    if not lo <= n <= hi:
        errors.append(f"{name} must be between {lo:g} and {hi:g}")
        return None
    return int(n) if integer else round(n, 1)


def from_form(data) -> tuple[ApplyProfile | None, list[str]]:
    """Validate what the web form sent. Returns (profile, errors); profile is None when errors."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return None, ["form data must be an object"]
    p = ApplyProfile()
    for k, n in TEXT.items():
        v = data.get(k, "")
        if not isinstance(v, str):
            errors.append(f"{k} must be text")
            continue
        v = re.sub(r"[\x00-\x1f\x7f]", " ", v) if k not in MULTILINE else \
            re.sub(r"[\x00-\x09\x0b-\x1f\x7f]", " ", v)
        v = v.strip()
        if len(v) > n:
            errors.append(f"{k} is too long (max {n})")
            continue
        setattr(p, k, v)
    if p.email and not EMAIL_RE.match(p.email):
        errors.append("email doesn't look right")
    if p.phone and not PHONE_RE.match(p.phone):
        errors.append("phone doesn't look right (digits, optional +country code)")
    for k in URLS:
        v = getattr(p, k)
        if v and not re.match(r"^https://[^\s/]+\.[^\s]+$", v):
            errors.append(f"{k} must be a full https:// link")
    if p.pincode and not re.fullmatch(r"[1-9]\d{5}", p.pincode):
        errors.append("PIN code must be 6 digits")
    dob = str(data.get("dob", "") or "").strip()
    if dob:
        try:
            ok = re.fullmatch(r"(19[4-9]\d|20[01]\d)-\d\d-\d\d", dob) and date.fromisoformat(dob)
        except ValueError:
            ok = False
        if not ok:
            errors.append("date of birth must be a real date (YYYY-MM-DD)")
        else:
            p.dob = dob
    p.total_years = _num(data.get("total_years"), 0, 40, "years of experience", errors)
    p.relevant_years = _num(data.get("relevant_years"), 0, 40, "relevant experience", errors)
    if p.relevant_years is not None and p.total_years is not None and p.relevant_years > p.total_years:
        errors.append("relevant experience can't be more than total experience")
    p.current_ctc_lpa = _num(data.get("current_ctc_lpa"), 0, 500, "current CTC", errors)
    p.expected_ctc_lpa = _num(data.get("expected_ctc_lpa"), 0, 500, "expected CTC", errors)
    p.education = _education(data.get("education"), errors)
    for k, allowed in (("notice", NOTICE), ("relocate", YES_NO), ("sponsorship", YES_NO),
                       ("work_mode", WORK_MODES), ("disability", DISABILITY)):
        v = str(data.get(k, getattr(p, k)) or "").strip().lower()
        if v not in allowed:
            errors.append(f"{k} must be one of {', '.join(allowed)}")
        else:
            setattr(p, k, v)
    sites = data.get("sites", {})
    if not isinstance(sites, dict):
        errors.append("sites must be an object")
        sites = {}
    for k, v in sites.items():
        if k not in SITES:
            errors.append(f"unknown site {k!r}")
        elif v not in MODES:
            errors.append(f"{k}: mode must be off, ask or auto")
        else:
            p.sites[k] = v
    cap = _num(data.get("daily_cap", DEFAULT_DAILY_CAP), 1, MAX_DAILY_CAP, "daily limit", errors, integer=True)
    p.daily_cap = cap if cap is not None else DEFAULT_DAILY_CAP
    p.consent = data.get("consent") is True
    if any(m != "off" for m in p.sites.values()) and not p.consent:
        errors.append("tick the consent box to turn on auto-apply for any site")
    return (None, errors) if errors else (p, [])


# ---------- prefill from the resume (first time the form opens) ----------

def guess_from_resume(text: str, name: str = "", years: float | None = None) -> ApplyProfile:
    p = ApplyProfile(full_name=name, total_years=years)
    t = text or ""
    m = re.search(r"[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63})+", t)
    if m and EMAIL_RE.match(m.group(0)):
        p.email = m.group(0)
    m = re.search(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", t)
    if m:
        p.phone = re.sub(r"[\s-]", "", m.group(0))
    for key, pat in (("linkedin", r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w%-]+/?"),
                     ("github", r"(?:https?://)?(?:www\.)?github\.com/[\w-]+/?")):
        m = re.search(pat, t, re.I)
        if m:
            url = m.group(0)
            setattr(p, key, url if url.startswith("http") else "https://" + url)
    return p
