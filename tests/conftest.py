"""Test setup: every test gets an empty database in a throwaway data dir; no network, no Telegram."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="jobhunter-tests-")
# Must be set before `app` is imported (settings are read once, at import).
os.environ.update({
    "DATA_DIR": _TMP, "TELEGRAM_BOT_TOKEN": "123:test", "ADMIN_IDS": "", "ACCESS_MODE": "approval",
    "REQUEST_DELAY_MIN": "0", "REQUEST_DELAY_MAX": "0", "PAGES_FIRST_SCAN": "3", "MAX_PER_CYCLE": "10",
    "SCAN_EVERY_MINUTES": "5", "TIMEZONE": "Asia/Kolkata",
})

import pytest  # noqa: E402

from app import db  # noqa: E402
from app.config import settings  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def fresh_db():
    import shutil
    for p in Path(_TMP).iterdir():
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    db.init()
    yield


def make_pdf(lines: list[str]) -> bytes:
    """A minimal one-page text PDF (no extra dependencies)."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 40 780 Td 13 TL " + " ".join(f"({esc(line)}) '" for line in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
            "/Contents 5 0 R >>",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


SAMPLE_RESUME = """Asha Verma
Bengaluru · asha@example.com
Summary
Backend Developer building AI agents, RAG pipelines and MCP servers with Node.js and TypeScript.
Experience
Acme AI Labs — Software Engineer                       Jan 2024 – Present
- Built RAG search with pgvector and LangChain, LLM agents on Azure OpenAI.
- REST APIs and microservices in Node.js, TypeScript, PostgreSQL, Docker; AWS Lambda + SQS.
Beta Corp — Backend Developer (Intern)                Jun 2023 – Dec 2023
- Spring Boot, Java, MySQL, JWT auth.
Education
B.Tech Computer Science, XYZ University               2019 – 2023
Skills
JavaScript, TypeScript, Java, Node.js, Express, MongoDB, Git, System Design
"""
