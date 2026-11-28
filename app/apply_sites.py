"""Job sites the bot knows for applying, and how each one can be used.

kind:
    public   the apply form is a public page that needs no account (company ATS boards)
    login    applying needs the user's own account on that site (they log in once themselves)
Modes per site (chosen in the 📝 Application form): off · ask (Claude fills, you tap ✅ to submit) · auto.

Signing in (login sites): `jobhunter connect <site>` opens the site's real login page in Chrome on the server;
the user logs in there themselves and only that site's cookies are saved (data/users/<id>/sessions/<site>.json,
0600). The bot never asks for, sees or stores a password: these sites offer no sign-in for outside apps, and a
password typed into our page would be an account takeover risk and trip their OTP / CAPTCHA checks anyway.

Hard rules, enforced in code: no CAPTCHA solving, no bot-detection evasion. If a site shows a CAPTCHA,
a login wall or a block, Claude stops and the bot sends the link for the user to finish.
"""
from __future__ import annotations

from urllib.parse import urlparse

MODES = ("off", "ask", "auto")
MODE_LABELS = {"off": "Off", "ask": "Ask me", "auto": "Auto"}

SITES: dict[str, dict] = {
    "linkedin": {"label": "LinkedIn Easy Apply", "kind": "login", "hosts": ("linkedin.com",),
                 "login_url": "https://www.linkedin.com/login",
                 "note": "Needs your LinkedIn login. LinkedIn restricts accounts that automate applying."},
    "naukri": {"label": "Naukri", "kind": "login", "hosts": ("naukri.com",),
               "login_url": "https://www.naukri.com/nlogin/login",
               "note": "Needs your Naukri login. Naukri uses CAPTCHAs; when one appears you finish by hand."},
    "indeed": {"label": "Indeed", "kind": "login", "hosts": ("indeed.com",),
               "login_url": "https://secure.indeed.com/auth",
               "note": "Needs your Indeed login. Indeed blocks most automated browsers (Cloudflare), so expect "
                       "many “finish here” links."},
    "wellfound": {"label": "Wellfound", "kind": "login", "hosts": ("wellfound.com", "angel.co"),
                  "login_url": "https://wellfound.com/login",
                  "note": "Needs your Wellfound login. Applying is a short note to the startup."},
    "gfg": {"label": "GeeksforGeeks Jobs", "kind": "login", "hosts": ("geeksforgeeks.org",),
            "login_url": "https://auth.geeksforgeeks.org/",
            "note": "Needs your GFG login. External-link jobs go to the company site."},
    "greenhouse": {"label": "Greenhouse (company sites)", "kind": "public",
                   "hosts": ("greenhouse.io",), "note": "Public company application forms, no account needed."},
    "lever": {"label": "Lever (company sites)", "kind": "public", "hosts": ("lever.co",),
              "note": "Public company application forms, no account needed."},
    "ashby": {"label": "Ashby (company sites)", "kind": "public", "hosts": ("ashbyhq.com",),
              "note": "Public company application forms, no account needed."},
}
DEFAULT_DAILY_CAP = 10
MAX_DAILY_CAP = 25


def site_for(url: str) -> str:
    """'https://boards.greenhouse.io/acme/jobs/1' -> 'greenhouse'; '' when it's no known site."""
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except ValueError:
        return ""
    for key, s in SITES.items():
        if any(host == h or host.endswith("." + h) for h in s["hosts"]):
            return key
    return ""


def default_modes() -> dict[str, str]:
    return {k: "off" for k in SITES}


def login_sites() -> list[str]:
    return [k for k, s in SITES.items() if s["kind"] == "login"]
