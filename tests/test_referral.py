import json
from urllib.parse import unquote

import pytest

from app import db, referral, ui
from app.resume import ResumeProfile, build_profile, find_name
from tests.conftest import SAMPLE_RESUME

ME = ResumeProfile(skills=["Node.js", "TypeScript", "RAG", "AWS", "MCP"], years=2.8, titles=["Backend Developer"],
                   name="Asha Verma")
URL = "https://www.linkedin.com/jobs/view/4495024990/"


def build(**kw):
    args = dict(title="AI Engineer", company="Acme AI Pvt. Ltd.", job_id="4495024990", url=URL,
                matched=["RAG", "MCP", "LLMs"], profile=ME, name="Asha Verma", my_years=2.8)
    args.update(kw)
    return referral.build(**args)


def test_messages_are_job_specific():
    r = build()
    for text in (r.note, r.message, r.followup, r.subject):
        assert "AI Engineer" in text and "Acme AI" in text and "4495024990" in text
    assert URL in r.message and "Asha Verma" in r.message
    assert "Backend Developer" in r.message and "2+ years" in r.message
    assert "RAG, MCP and LLMs" in r.message                   # what this job asks for that you have
    assert "Pvt" not in r.subject                             # company name cleaned


def test_skills_not_repeated_in_message():
    r = build()
    intro_line = [ln for ln in r.message.splitlines() if ln.startswith("I'm")][0]
    assert "RAG" not in intro_line and "Node.js" in intro_line


@pytest.mark.parametrize("title,company", [
    ("AI Engineer", "Acme"),
    ("Senior Staff Generative AI / LLM Platform Engineer (Agents, RAG, Evaluation)",
     "International Business Machines Corporation Global Services India Private Limited"),
])
def test_connection_note_always_fits_linkedin_limit(title, company):
    r = build(title=title, company=company)
    assert len(r.note) <= referral.NOTE_LIMIT
    assert "4495024990" in r.note or r.note.endswith("…")


def test_note_uses_richest_version_that_fits():
    r = build()
    assert "Would you be open to referring me" in r.note or "Could you refer me" in r.note
    assert "2+ years" in r.note


def test_works_without_resume():
    r = referral.build("Backend Developer", "Beta", "1", URL, [], None, "", None, fallback_role="Node.js Developer")
    assert "I'm a Node.js Developer" in r.message and "[Your name]" in r.message
    assert "years" not in r.note and len(r.note) <= 200


@pytest.mark.parametrize("years,text", [(None, ""), (0.2, ""), (0.8, "1 year"), (2.8, "2+ years"), (5, "5+ years")])
def test_years_text(years, text):
    assert referral._years_text(years) == text


def test_people_search_links():
    r = build(title="AI Engineer – P&B (Sr.)")
    assert r.employees_url.startswith("https://www.linkedin.com/search/results/people/?keywords=")
    assert unquote(r.employees_url.split("=", 1)[1]) == "Acme AI AI Engineer"
    assert "recruiter" in unquote(r.recruiters_url)


@pytest.mark.parametrize("raw,clean", [("Acme Pvt. Ltd.", "Acme"), ("Foo Private Limited", "Foo"),
                                       ("Bar Inc.", "Bar"), ("Plain", "Plain"), ("", "")])
def test_clean_company(raw, clean):
    assert referral.clean_company(raw) == clean


# ---------- name from resume ----------

@pytest.mark.parametrize("text,name", [
    ("Harsh Sharma\nDayal Bagh, Agra", "Harsh Sharma"),
    ("RESUME\nASHA VERMA\nBengaluru", "Asha Verma"),
    ("Curriculum Vitae\nJohn D. Smith | john@x.com", "John D. Smith"),
    ("asha@example.com\n+91 99999 99999", ""),
])
def test_find_name(text, name):
    assert find_name(text) == name


def test_profile_includes_name():
    assert build_profile(SAMPLE_RESUME, "cv.txt").name == "Asha Verma"


# ---------- bot ----------

def test_referral_button_on_every_job_alert():
    row = {"job_id": "9", "url": URL, "easy_apply": 1}
    for state in ("", "✅ Applied"):
        kb = ui.job_kb(row, state).to_dict()["inline_keyboard"]
        assert kb[-1][0]["callback_data"] == "ref:9"


def test_referral_handler_sends_copyable_messages():
    import asyncio
    from app import bot
    from tests.fakes import make_ctx, make_update
    from tests.test_bot import active, pending_job
    active(1)
    db.update_user(1, resume=json.dumps(ME.to_dict()))
    pending_job(1, "77", title="AI <Engineer>", company="Acme & Co")
    ctx = make_ctx()
    upd = make_update(uid=1, data="ref:77")
    asyncio.run(bot.on_referral_button(upd, ctx))
    msg = ctx.bot.sent[-1]
    assert "Referral messages" in msg.text and "<pre>" in msg.text
    assert "AI &lt;Engineer&gt;" in msg.text and "Acme &amp; Co" in msg.text and "Asha Verma" in msg.text
    buttons = msg.kw["reply_markup"].to_dict()["inline_keyboard"]
    assert buttons[0][0]["url"].startswith("https://www.linkedin.com/search/results/people/")


def test_referral_for_someone_elses_job_refused():
    import asyncio
    from app import bot
    from tests.fakes import make_ctx, make_update
    from tests.test_bot import active, pending_job
    active(1)
    active(2)
    pending_job(1, "77")
    ctx = make_ctx()
    upd = make_update(uid=2, data="ref:77")
    asyncio.run(bot.on_referral_button(upd, ctx))
    assert not ctx.bot.sent
    assert "no longer available" in upd.callback_query.answer.await_args.args[0]


def test_referral_handler_is_wired():
    from app import bot
    app = bot.build()
    patterns = [getattr(h, "pattern", None) for hs in app.handlers.values() for h in hs]
    assert any(p is not None and p.pattern == "^ref:" for p in patterns)
