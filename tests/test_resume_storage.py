import io
import json
import os
import stat
from datetime import date

import docx
import pytest

from app import storage
from app.config import settings
from app.resume import ResumeError, ResumeProfile, build_profile, experience_years, extract_text, find_titles
from tests.conftest import SAMPLE_RESUME, make_pdf

TODAY = date(2026, 11, 27)


def test_extract_txt():
    assert "Acme AI Labs" in extract_text(SAMPLE_RESUME.encode(), "cv.txt")


def test_extract_pdf():
    text = extract_text(make_pdf(SAMPLE_RESUME.splitlines()), "cv.PDF")
    assert "Node.js" in text and "Jan 2024" in text


def test_extract_docx():
    d = docx.Document()
    for line in SAMPLE_RESUME.splitlines():
        d.add_paragraph(line)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Kafka", "Redis"
    buf = io.BytesIO()
    d.save(buf)
    text = extract_text(buf.getvalue(), "cv.docx")
    assert "LangChain" in text and "Kafka | Redis" in text


@pytest.mark.parametrize("data,name,msg", [
    (b"hello", "cv.exe", "PDF, DOCX or TXT"),
    (b"short", "cv.txt", "couldn't find text"),
    (b"%PDF-1.4 garbage", "cv.pdf", "Couldn't read"),
])
def test_extract_errors(data, name, msg):
    with pytest.raises(ResumeError, match=msg):
        extract_text(data, name)


def test_years_from_dates_merges_and_skips_education():
    years, src = experience_years(SAMPLE_RESUME, TODAY)
    # Jun 2023 - Present (Dec 2023 and Jan 2024 are back to back) = 42 months; the B.Tech 2019-2023 is skipped
    assert src == "dates" and years == 3.5


def test_years_overlapping_ranges_not_double_counted():
    text = "Company A  Jan 2022 - Dec 2022\nCompany B  Jun 2022 - Jun 2023\n"
    assert experience_years(text, TODAY) == (1.5, "dates")


@pytest.mark.parametrize("text,expected", [
    ("Engineer 03/2021 - 02/2023", (2.0, "dates")),
    ("Dev at X, Feb '24 – Nov 2024", (0.8, "dates")),
    ("I have 4+ years of professional experience", (4.0, "stated")),
    ("no dates here", (None, "")),
    ("Worked 2030 - 2031", (None, "")),         # future dates are ignored
])
def test_years_formats(text, expected):
    assert experience_years(text, TODAY) == expected


def test_profile_from_sample():
    p = build_profile(SAMPLE_RESUME, "cv.txt", TODAY)
    for s in ("Node.js", "TypeScript", "RAG", "MCP", "AI Agents", "LangChain", "PostgreSQL", "AWS Lambda"):
        assert s in p.skills
    assert p.years == 3.5
    assert "Backend Developer" in p.titles and "Software Engineer" in p.titles
    assert p.suggested_keywords[:2] == ["AI Engineer", "Generative AI Engineer"]
    assert "Node.js Developer" in p.suggested_keywords and len(p.suggested_keywords) <= 6
    assert ResumeProfile.from_dict(p.to_dict()) == p


def test_titles_clean_up():
    assert find_titles("Senior genai engineer and ai/ml stuff, Sr. Backend Developer") == [
        "Senior GenAI Engineer", "Sr. Backend Developer"]


def test_profile_without_skills_suggests_software_engineer():
    assert build_profile("x " * 80, "a.txt").suggested_keywords == ["Software Engineer"]


# ---------- per-user storage ----------

def test_save_resume_creates_private_user_folder():
    path = storage.save_resume(42, "My CV.PDF", b"%PDF data", "text", {"skills": ["Python"]})
    d = settings.users_dir / "42"
    assert path == d / "resume.pdf" and path.read_bytes() == b"%PDF data"
    assert (d / "resume.txt.extracted").read_text() == "text"
    assert json.loads((d / "profile.json").read_text()) == {"skills": ["Python"]}
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert storage.resume_file(42) == path


def test_new_resume_replaces_old_one():
    storage.save_resume(7, "a.pdf", b"1", "t", {})
    storage.save_resume(7, "b.docx", b"2", "t", {})
    files = sorted(p.name for p in (settings.users_dir / "7").iterdir())
    assert files == ["profile.json", "resume.docx", "resume.txt.extracted"]


def test_users_are_isolated_and_deletable():
    storage.save_resume(1, "a.txt", b"one", "t", {})
    storage.save_resume(2, "a.txt", b"two", "t", {})
    storage.delete_user(1)
    assert not (settings.users_dir / "1").exists()
    assert (settings.users_dir / "2" / "resume.txt").read_bytes() == b"two"
    storage.delete_user(999)   # no folder: no error


@pytest.mark.parametrize("bad", ["../etc", "1/../../x", 1.5, True])
def test_user_dir_rejects_non_int_ids(bad):
    with pytest.raises(ValueError):
        storage.user_dir(bad)


def test_save_resume_rejects_bad_extension():
    with pytest.raises(ValueError):
        storage.save_resume(3, "evil.sh", b"x", "t", {})
