"""Resume -> text -> profile (skills, years of experience, suggested search keywords).

Supported: PDF, DOCX, TXT/MD. Everything runs locally; the resume never leaves the server.
"""
from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, field
from datetime import date

from .skills import AI, BACKEND, CLOUD, DATA, DEVOPS, FRONTEND, MOBILE, category, find_skills

SUPPORTED = {".pdf", ".docx", ".txt", ".md"}


class ResumeError(Exception):
    pass


@dataclass
class ResumeProfile:
    skills: list[str] = field(default_factory=list)       # strongest first
    years: float | None = None
    years_source: str = ""                                 # "stated" | "dates" | ""
    titles: list[str] = field(default_factory=list)       # job titles found in the resume
    suggested_keywords: list[str] = field(default_factory=list)
    file_name: str = ""
    chars: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict | None) -> "ResumeProfile | None":
        if not d:
            return None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------- text extraction ----------

def extract_text(data: bytes, filename: str) -> str:
    ext = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    if ext not in SUPPORTED:
        raise ResumeError("Please send a PDF, DOCX or TXT file.")
    try:
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))
            text = "\n".join((p.extract_text() or "") for p in reader.pages[:10])
        elif ext == ".docx":
            import docx
            d = docx.Document(io.BytesIO(data))
            parts = [p.text for p in d.paragraphs]
            for table in d.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text for c in row.cells))
            text = "\n".join(parts)
        else:
            text = data.decode("utf-8", errors="ignore")
    except ResumeError:
        raise
    except Exception as e:  # corrupt / encrypted files
        raise ResumeError(f"Couldn't read that file ({type(e).__name__}). Try exporting it as PDF again.") from e
    text = text.replace("\x00", "")
    if len(text.strip()) < 100:
        raise ResumeError("I couldn't find text in that file. If it's a scanned image, export a text PDF instead.")
    return text


# ---------- years of experience ----------

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
END = r"(present|current|now|till date|today|ongoing)"
DATE = rf"(?:{MON}\s*['’]?\s*(\d{{4}}|\d{{2}})|(\d{{1,2}})\s*/\s*(\d{{4}})|(\d{{4}}))"
RANGE_RE = re.compile(rf"{DATE}\s*(?:-|–|—|to|until)\s*(?:{DATE}|{END})", re.I)
STATED_RE = re.compile(r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:\w+\s+){0,3}experience", re.I)
EDU_WORDS = re.compile(r"universit|college|school|b\.?\s?tech|m\.?\s?tech|bachelor|master|degree|cgpa|gpa|"
                       r"\bbca\b|\bmca\b|\bb\.?e\b|\bb\.?sc\b|education|class x|high school|semester", re.I)


def _to_month(groups: tuple, today: date) -> tuple[int, int] | None:
    mon, yy, mm, yyyy, y_only = groups
    if mon:
        year = int(yy) + (2000 if len(yy) == 2 else 0)
        return year, MONTHS[mon[:3].lower()]
    if mm:
        return int(yyyy), max(1, min(12, int(mm)))
    if y_only:
        return int(y_only), 1
    return None


def experience_years(text: str, today: date | None = None) -> tuple[float | None, str]:
    """Total professional experience. Merges overlapping date ranges and skips education lines."""
    today = today or date.today()
    stated = [float(m.group(1)) for m in STATED_RE.finditer(text) if float(m.group(1)) <= 40]
    intervals: list[tuple[int, int]] = []
    for line_no, line in enumerate(text.splitlines()):
        for m in RANGE_RE.finditer(line):
            context = line + " " + " ".join(text.splitlines()[max(0, line_no - 1):line_no + 2])
            if EDU_WORDS.search(context):
                continue
            start = _to_month(m.groups()[0:5], today)
            if m.group(11):  # present
                end = (today.year, today.month)
            else:
                end = _to_month(m.groups()[5:10], today)
            if not start or not end:
                continue
            s, e = start[0] * 12 + start[1], end[0] * 12 + end[1]
            if 1980 * 12 <= s <= e <= today.year * 12 + today.month:
                intervals.append((s, e))
    if intervals:
        intervals.sort()
        merged = [list(intervals[0])]
        for s, e in intervals[1:]:
            if s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e])
        months = sum(e - s + 1 for s, e in merged)
        return round(months / 12, 1), "dates"
    if stated:
        return max(stated), "stated"
    return None, ""


# ---------- titles & keyword suggestions ----------

TITLE_RE = re.compile(
    r"\b((?:senior |sr\.? |junior |jr\.? |lead |associate |staff )?"
    r"(?:software|backend|back[- ]end|frontend|front[- ]end|full[- ]?stack|application|ai|ml|machine learning|"
    r"gen(?:erative)? ?ai|llm|data|devops|cloud|java|python|node(?:\.js)?|mobile|android|ios|platform|sde)"
    r"(?: \w+)?? (?:engineer|developer|scientist|analyst|architect|intern|consultant))\b", re.I)


def find_titles(text: str) -> list[str]:
    seen: list[str] = []
    for m in TITLE_RE.finditer(text):
        t = re.sub(r"\s+", " ", m.group(1)).strip().title().replace("Ai ", "AI ").replace("Ml ", "ML ")
        t = t.replace("Llm", "LLM").replace("Sde", "SDE").replace("Ios", "iOS").replace("Genai", "GenAI")
        if t.lower() not in {s.lower() for s in seen}:
            seen.append(t)
    return seen[:6]


def suggest_keywords(skills: list[str], titles: list[str]) -> list[str]:
    cats: dict[str, int] = {}
    for s in skills:
        cats[category(s)] = cats.get(category(s), 0) + 1
    has = set(skills)
    out: list[str] = []

    def add(k: str) -> None:
        if k.lower() not in {o.lower() for o in out}:
            out.append(k)

    if cats.get(AI, 0) >= 3:
        add("AI Engineer")
        add("Generative AI Engineer")
        if has & {"LLMs", "RAG", "AI Agents", "LangChain"}:
            add("LLM Engineer")
    if cats.get(BACKEND, 0) >= 2:
        add("Backend Developer")
        add("Software Engineer")
    if "Node.js" in has:
        add("Node.js Developer")
    if "Java" in has and "Spring Boot" in has:
        add("Java Developer")
    if "Python" in has and cats.get(BACKEND, 0) >= 1:
        add("Python Developer")
    if cats.get(FRONTEND, 0) >= 2:
        add("Full Stack Developer" if cats.get(BACKEND, 0) >= 2 else "Frontend Developer")
    if cats.get(DATA, 0) >= 2:
        add("Data Engineer")
    if cats.get(DEVOPS, 0) + cats.get(CLOUD, 0) >= 5:
        add("DevOps Engineer")
    if cats.get(MOBILE, 0) >= 2:
        add("Mobile Developer")
    for t in titles:
        clean = re.sub(r"^(senior|sr\.?|junior|jr\.?|lead|associate|staff) ", "", t, flags=re.I)
        if "intern" not in clean.lower():
            add(clean)
    return out[:6] or ["Software Engineer"]


def build_profile(text: str, file_name: str = "", today: date | None = None) -> ResumeProfile:
    found = find_skills(text)
    skills = [s for s, _ in sorted(found.items(), key=lambda kv: (-kv[1], kv[0]))]
    years, source = experience_years(text, today)
    titles = find_titles(text)
    return ResumeProfile(skills=skills, years=years, years_source=source, titles=titles,
                         suggested_keywords=suggest_keywords(skills, titles), file_name=file_name,
                         chars=len(text))
