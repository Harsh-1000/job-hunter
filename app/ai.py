"""Bot side of the Claude worker (the `ai` container, see ai/server.py).

judge(): one job + one user's resume -> AIVerdict (score, verdict, why, missing skills), or None when the
worker is off, busy, over its hourly budget or returned something invalid. None always means "fall back to
the keyword match" so alerts never wait on Claude.

Verdicts are cached per (job, resume+targets) in db.ai_verdicts: a job is judged once per resume.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field

import httpx

from . import db
from .config import settings

log = logging.getLogger(__name__)

VERDICTS = ("strong", "good", "stretch", "skip")
VERDICT_LABELS = {"strong": "Strong fit", "good": "Good fit", "stretch": "Stretch", "skip": "Not a fit"}
MAX_TEXT = 12_000
NO_LOGIN = "not logged in to Claude (log in with `claude` on the server, or run: jobhunter ai login)"
NOTE_LIMIT = 200


@dataclass
class AIVerdict:
    score: int
    verdict: str
    why: str
    missing: list[str] = field(default_factory=list)
    seniority_ok: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "AIVerdict | None":
        if not d:
            return None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def parse_verdict(d) -> AIVerdict | None:
    """Strictly validate what the worker returned. Anything malformed -> None."""
    if not isinstance(d, dict):
        return None
    score, verdict, why = d.get("score"), d.get("verdict"), d.get("why")
    if isinstance(score, bool) or not isinstance(score, (int, float)) or verdict not in VERDICTS:
        return None
    if not isinstance(why, str) or not why.strip():
        return None
    missing = d.get("missing_skills", d.get("missing", []))
    if not isinstance(missing, list):
        missing = []
    missing = [str(m).strip()[:40] for m in missing if isinstance(m, str) and m.strip()][:6]
    why = " ".join(why.split())
    if len(why) > 240:
        why = why[:239].rstrip() + "…"
    return AIVerdict(score=max(0, min(100, int(round(score)))), verdict=verdict, why=why, missing=missing,
                     seniority_ok=d.get("seniority_ok") is not False)


def profile_hash(resume_text: str, targets: list[str], years: float | None) -> str:
    raw = json.dumps([resume_text or "", sorted(t.lower() for t in targets), years])
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def payload(row, resume_text: str, skills: list[str], targets: list[str], years: float | None) -> dict:
    return {
        "resume": {"text": (resume_text or "")[:MAX_TEXT], "skills": skills[:40], "targets": targets[:10],
                   "years": years},
        "job": {k: (row[k] or "") for k in ("title", "company", "location", "seniority", "employment_type")}
        | {"description": (row["description"] or "")[:MAX_TEXT]},
    }


class AIClient:
    def __init__(self, base_url: str | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (base_url if base_url is not None else settings.ai_url).rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url or "http://ai-disabled", transport=transport,
                                      timeout=settings.ai_timeout_s)
        self.last_error = ""

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    async def health(self) -> dict | None:
        if not self.enabled:
            return None
        try:
            r = await self.http.get("/health", timeout=10)
            return r.json() if r.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            return None

    async def judge(self, body: dict) -> AIVerdict | None:
        """One retry on a malformed answer or worker error; no retry on budget / busy / bad request."""
        if not self.enabled:
            self.last_error = "off"
            return None
        for attempt in (1, 2):
            try:
                r = await self.http.post("/judge", json=body)
            except httpx.HTTPError as err:
                self.last_error = f"offline ({type(err).__name__})"
                return None
            if r.status_code == 429:
                self.last_error = "over hourly budget"
                return None
            if r.status_code in (400, 413, 503):
                self.last_error = _err(r)
                if r.status_code == 503 and "login" in self.last_error:
                    self.last_error = NO_LOGIN
                return None
            if r.status_code == 200:
                try:
                    v = parse_verdict(r.json().get("verdict"))
                except (ValueError, AttributeError):
                    v = None
                if v:
                    self.last_error = ""
                    return v
                self.last_error = "invalid answer"
            else:
                self.last_error = _err(r)
            log.info("AI judge attempt %s failed: %s", attempt, self.last_error)
        return None

    async def _post(self, path: str, body: dict, key: str):
        """POST to the worker; returns the answer's `key` field, or None with last_error set."""
        if not self.enabled:
            self.last_error = "off"
            return None
        try:
            r = await self.http.post(path, json=body)
        except httpx.HTTPError as err:
            self.last_error = f"offline ({type(err).__name__})"
            return None
        if r.status_code != 200:
            self.last_error = NO_LOGIN if (r.status_code == 503 and "login" in _err(r)) else (
                "over hourly budget" if r.status_code == 429 else _err(r))
            return None
        try:
            return r.json().get(key)
        except (ValueError, AttributeError):
            self.last_error = "invalid answer"
            return None

    async def prepare(self, body: dict) -> dict | None:
        kit = parse_kit(await self._post("/prepare", body, "kit"))
        if not kit and not self.last_error:
            self.last_error = "invalid answer"
        return kit

    async def referral(self, body: dict) -> dict | None:
        """🤝 Claude-written referral messages to one named person."""
        ref = parse_referral(await self._post("/referral", body, "referral"))
        if not ref and not self.last_error:
            self.last_error = "invalid answer"
        return ref

    async def close(self) -> None:
        await self.http.aclose()


def _err(r: httpx.Response) -> str:
    try:
        return f"HTTP {r.status_code}: {r.json().get('error', '')}"[:200]
    except ValueError:
        return f"HTTP {r.status_code}"


def parse_kit(d) -> dict | None:
    """Validate a ✍️ Prepare kit: {cover_note, answers[{question, answer}], highlights[3], referral_note}."""
    if not isinstance(d, dict) or not isinstance(d.get("cover_note"), str) or not d["cover_note"].strip():
        return None
    answers = []
    for a in d.get("answers") or []:
        if isinstance(a, dict) and isinstance(a.get("question"), str) and isinstance(a.get("answer"), str) \
                and a["question"].strip() and a["answer"].strip():
            answers.append({"question": a["question"].strip()[:200], "answer": a["answer"].strip()[:900]})
    highlights = [h.strip()[:200] for h in d.get("highlights") or [] if isinstance(h, str) and h.strip()][:3]
    note = d.get("referral_note") if isinstance(d.get("referral_note"), str) else ""
    note = " ".join(note.split())
    if len(note) > 200:
        note = note[:199].rstrip() + "…"
    return {"cover_note": d["cover_note"].strip()[:1500], "answers": answers[:8], "highlights": highlights,
            "referral_note": note}


def fit_note(note: str, limit: int = NOTE_LIMIT) -> str:
    """LinkedIn connection notes are cut at 200 characters: shorten at a word, never mid-word."""
    note = " ".join((note or "").split())
    if len(note) <= limit:
        return note
    cut = note[:limit - 1]
    cut = cut[:cut.rfind(" ")] if " " in cut else cut
    return cut.rstrip(" ,;:") + "…"


def parse_referral(d) -> dict | None:
    """Validate Claude's referral messages: {note <= 200 chars, message, followup, subject}."""
    if not isinstance(d, dict):
        return None
    out = {}
    for k, n in (("note", 600), ("message", 1500), ("followup", 600), ("subject", 120)):
        v = d.get(k)
        if not isinstance(v, str) or not v.strip():
            return None
        out[k] = v.strip()[:n]
    out["note"] = fit_note(out["note"])
    out["subject"] = " ".join(out["subject"].split())
    return out


def referral_payload(row, resume_text: str, person: str, name: str, years: float | None) -> dict:
    return {"resume": {"text": (resume_text or "")[:MAX_TEXT], "years": years}, "person": person[:200],
            "name": name[:60],
            "job": {k: (row[k] or "") for k in ("title", "company", "url")}
            | {"id": row["job_id"], "description": (row["description"] or "")[:MAX_TEXT // 2]}}


def prepare_payload(row, resume_text: str, profile: dict) -> dict:
    return {"resume": {"text": (resume_text or "")[:MAX_TEXT]}, "profile": profile,
            "job": {k: (row[k] or "") for k in ("title", "company", "location")}
            | {"description": (row["description"] or "")[:MAX_TEXT]}}


# ---------------- status (for "Is it working?" and the watchdog) ----------------

def record_status(error: str) -> None:
    if error:
        if not db.get_meta("ai_failing_since"):
            db.set_meta("ai_failing_since", db.iso())
        db.set_meta("ai_last_error", error)
    else:
        db.set_meta("ai_last_ok", db.iso())
        db.set_meta("ai_failing_since", "")
        db.set_meta("ai_last_error", "")


def status() -> dict:
    """{'enabled', 'state': ok|offline|budget|unknown, 'error', 'failing_since', 'last_ok'}"""
    if not settings.ai_url:
        return {"enabled": False, "state": "off", "error": "", "failing_since": "", "last_ok": ""}
    err, since, ok = db.get_meta("ai_last_error"), db.get_meta("ai_failing_since"), db.get_meta("ai_last_ok")
    if not (err or ok):
        state = "unknown"
    elif not err:
        state = "ok"
    else:
        state = "budget" if "budget" in err else ("nologin" if "not logged in" in err else "offline")
    return {"enabled": True, "state": state, "error": err, "failing_since": since, "last_ok": ok}
