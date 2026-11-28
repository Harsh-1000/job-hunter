"""The one-time application profile ("📝 Application form"): everything job forms usually ask.

Filled in a Telegram Mini App (docs/apply-form/index.html), sent back with Telegram.WebApp.sendData
(max 4096 bytes), strictly validated here and stored in data/users/<id>/apply_profile.json (0600).
Used by ✍️ Prepare (Claude writes answers) and by auto-apply (Claude fills forms on the sites you chose).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields

from .apply_sites import DEFAULT_DAILY_CAP, MAX_DAILY_CAP, MODES, SITES, default_modes

MAX_FORM_BYTES = 4096
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{6,18}[0-9]$")
NOTICE = ("immediate", "15", "30", "60", "90")
YES_NO = ("yes", "no")

# field -> max length (text fields)
TEXT = {"full_name": 60, "email": 120, "phone": 20, "city": 60, "linkedin": 150, "github": 150,
        "portfolio": 150, "current_company": 80, "current_title": 80, "preferred_locations": 120,
        "work_auth": 80, "degree": 80, "college": 120, "gender": 20, "why_default": 400, "cover_note": 600}
URLS = ("linkedin", "github", "portfolio")


@dataclass
class ApplyProfile:
    full_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""
    current_company: str = ""
    current_title: str = ""
    total_years: float | None = None
    notice: str = "30"                 # immediate | 15 | 30 | 60 | 90 (days)
    current_ctc_lpa: float | None = None
    expected_ctc_lpa: float | None = None
    relocate: str = "yes"
    preferred_locations: str = ""
    work_auth: str = "Indian citizen"
    sponsorship: str = "no"
    degree: str = ""
    college: str = ""
    grad_year: int | None = None
    cgpa: str = ""
    gender: str = ""
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
        """What Claude may see to write answers: no email / phone (not needed to write text)."""
        d = self.to_dict()
        for k in ("email", "phone", "sites", "daily_cap", "consent"):
            d.pop(k, None)
        return {k: v for k, v in d.items() if v not in ("", None)}


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
        v = re.sub(r"[\x00-\x1f\x7f]", " ", v) if k not in ("why_default", "cover_note") else \
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
    p.total_years = _num(data.get("total_years"), 0, 40, "years of experience", errors)
    p.current_ctc_lpa = _num(data.get("current_ctc_lpa"), 0, 500, "current CTC", errors)
    p.expected_ctc_lpa = _num(data.get("expected_ctc_lpa"), 0, 500, "expected CTC", errors)
    p.grad_year = _num(data.get("grad_year"), 1970, 2035, "graduation year", errors, integer=True)
    cgpa = str(data.get("cgpa", "") or "").strip()
    if cgpa and not re.fullmatch(r"\d{1,3}(\.\d{1,2})?\s*(%|/\s*10|/\s*4)?", cgpa):
        errors.append("CGPA should look like 8.2, 8.2/10 or 76%")
    p.cgpa = cgpa
    for k, allowed in (("notice", NOTICE), ("relocate", YES_NO), ("sponsorship", YES_NO)):
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
