"""Per-user folders for resumes.

    data/users/<telegram_user_id>/
        resume.pdf | resume.docx | resume.txt   the file the user sent (latest only)
        resume.txt.extracted                    plain text pulled out of it
        profile.json                            skills / years / keywords parsed from it
        apply_profile.json                      the 📝 Application form (contact, CTC, notice, auto-apply sites)

The folder name is the numeric Telegram user ID (Telegram guarantees it is unique and it never
changes), so a user can only ever touch their own folder. Folders are created 0700 and files
0600; /deleteme removes the whole folder.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import settings
from .resume import SUPPORTED


def user_dir(user_id: int, create: bool = False) -> Path:
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        raise ValueError("user_id must be an int")
    d = settings.users_dir / str(user_id)
    if create:
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(0o600)
    tmp.replace(path)


def save_resume(user_id: int, filename: str, data: bytes, text: str, profile: dict) -> Path:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ".txt"
    if ext not in SUPPORTED:
        raise ValueError(f"unsupported file type {ext}")
    d = user_dir(user_id, create=True)
    for old in d.glob("resume.*"):           # keep only the latest resume
        old.unlink()
    path = d / f"resume{ext}"
    _write(path, data)
    _write(d / "resume.txt.extracted", text.encode())
    _write(d / "profile.json", json.dumps(profile, indent=2).encode())
    return path


def resume_file(user_id: int) -> Path | None:
    d = user_dir(user_id)
    for ext in SUPPORTED:
        p = d / f"resume{ext}"
        if p.exists():
            return p
    return None


def save_apply_profile(user_id: int, data: dict) -> Path:
    d = user_dir(user_id, create=True)
    path = d / "apply_profile.json"
    _write(path, json.dumps(data, indent=2).encode())
    return path


def load_apply_profile(user_id: int) -> dict | None:
    p = user_dir(user_id) / "apply_profile.json"
    try:
        return json.loads(p.read_text()) if p.exists() else None
    except (OSError, ValueError):
        return None


def resume_text(user_id: int) -> str:
    p = user_dir(user_id) / "resume.txt.extracted"
    return p.read_text(errors="ignore") if p.exists() else ""


def delete_user(user_id: int) -> None:
    d = user_dir(user_id)
    if d.exists():
        shutil.rmtree(d)
