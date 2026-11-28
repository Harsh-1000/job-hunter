"""📝 Application form (Mini App) + ✍️ Prepare: validation, storage, sites, page, bot handlers."""
import asyncio
import base64
import json
import re
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import ai, bot, db, storage, ui
from app import apply_profile as ap
from app.apply_sites import SITES, site_for
from app.config import settings
from app.linkedin import Job
from tests.fakes import FakeBot, make_ctx, make_update
from tests.test_scanner import ME, add_user

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "apply-form" / "index.html"

FORM = {"full_name": "Asha Verma", "email": "asha@example.com", "phone": "+91 98765 43210", "city": "New Delhi",
        "linkedin": "https://www.linkedin.com/in/asha", "github": "", "portfolio": "", "current_company": "Acme",
        "current_title": "Software Engineer", "total_years": "2.8", "notice": "30", "current_ctc_lpa": "12",
        "expected_ctc_lpa": "18.5", "relocate": "yes", "sponsorship": "no", "preferred_locations": "Delhi NCR",
        "work_auth": "Indian citizen", "degree": "B.Tech CSE", "college": "XYZ", "grad_year": "2023",
        "cgpa": "8.2/10", "gender": "", "why_default": "I like building\nAI products.", "cover_note": "",
        "daily_cap": "10", "consent": True, "sites": {"greenhouse": "auto", "naukri": "ask"}}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def ai_on():
    old = settings.ai_url
    object.__setattr__(settings, "ai_url", "http://ai:8787")
    yield
    object.__setattr__(settings, "ai_url", old)


# ---------------- validation ----------------

def test_valid_form():
    p, errs = ap.from_form(FORM)
    assert errs == [] and p.full_name == "Asha Verma" and p.total_years == 2.8 and p.expected_ctc_lpa == 18.5
    assert p.grad_year == 2023 and p.sites["greenhouse"] == "auto" and p.sites["indeed"] == "off"
    assert p.active_sites() == {"greenhouse": "auto", "naukri": "ask"} and p.daily_cap == 10
    assert p.why_default == "I like building\nAI products."     # newlines kept in long answers
    assert p.missing() == []


@pytest.mark.parametrize("change,msg", [
    ({"email": "nope"}, "email"), ({"phone": "12"}, "phone"), ({"linkedin": "http://x.com/a"}, "https"),
    ({"linkedin": "javascript:alert(1)"}, "https"), ({"total_years": "99"}, "years"),
    ({"total_years": "abc"}, "number"), ({"notice": "45"}, "notice"), ({"relocate": "maybe"}, "relocate"),
    ({"sites": {"monster": "auto"}}, "unknown site"), ({"sites": {"lever": "always"}}, "mode"),
    ({"consent": False}, "consent"), ({"consent": "yes"}, "consent"), ({"full_name": "x" * 61}, "too long"),
    ({"city": 5}, "text"), ({"daily_cap": "99"}, "daily limit"), ({"cgpa": "great"}, "CGPA"),
    ({"grad_year": "1800"}, "graduation"), ({"sites": []}, "sites")])
def test_invalid_form(change, msg):
    p, errs = ap.from_form({**FORM, **change})
    assert p is None and any(msg in x for x in errs), errs


def test_form_not_an_object():
    assert ap.from_form(["x"]) == (None, ["form data must be an object"])


def test_control_chars_stripped_and_all_off_needs_no_consent():
    p, errs = ap.from_form({**FORM, "city": "New\x00 Delhi\x1b", "consent": False, "sites": {}})
    assert errs == [] and p.city == "New  Delhi" and p.active_sites() == {}


def test_from_dict_drops_unknown_and_for_claude_hides_contact():
    p = ap.ApplyProfile.from_dict({**ap.from_form(FORM)[0].to_dict(), "sites": {"lever": "ask", "x": "auto",
                                                                                "ashby": "bad"}, "zzz": 1})
    assert p.sites["lever"] == "ask" and "x" not in p.sites and p.sites["ashby"] == "off"
    c = p.for_claude()
    assert "email" not in c and "phone" not in c and "sites" not in c and c["expected_ctc_lpa"] == 18.5
    assert ap.ApplyProfile.from_dict(None) is None


def test_active_sites_needs_consent():
    p = ap.ApplyProfile(sites={**ap.default_modes(), "lever": "auto"}, consent=False)
    assert p.active_sites() == {}


def test_guess_from_resume():
    g = ap.guess_from_resume("Asha\nasha.v+jobs@mail.co.in | +91 98765-43210\nlinkedin.com/in/asha-v\n"
                             "https://github.com/ashav", "Asha Verma", 2.5)
    assert g.email == "asha.v+jobs@mail.co.in" and g.phone == "+919876543210" and g.total_years == 2.5
    assert g.linkedin == "https://linkedin.com/in/asha-v" and g.github == "https://github.com/ashav"
    assert ap.guess_from_resume("", "", None).email == ""


# ---------------- sites ----------------

@pytest.mark.parametrize("url,site", [
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"), ("https://job-boards.greenhouse.io/x/jobs/2", "greenhouse"),
    ("https://jobs.lever.co/acme/abc", "lever"), ("https://jobs.ashbyhq.com/acme/1", "ashby"),
    ("https://in.indeed.com/viewjob?jk=1", "indeed"), ("https://www.naukri.com/job-listings-1", "naukri"),
    ("https://wellfound.com/jobs/1", "wellfound"), ("https://www.geeksforgeeks.org/jobs/x", "gfg"),
    ("https://evilgreenhouse.io/x", ""), ("https://greenhouse.io.evil.com/x", ""), ("not a url", ""), ("", "")])
def test_site_for(url, site):
    assert site_for(url) == site


def test_every_site_has_label_kind_note():
    for k, s in SITES.items():
        assert s["label"] and s["kind"] in ("login", "public") and s["note"] and s["hosts"]


# ---------------- storage ----------------

def test_apply_profile_storage_private_and_deleted():
    path = storage.save_apply_profile(7, {"full_name": "A"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and storage.load_apply_profile(7) == {"full_name": "A"}
    assert storage.load_apply_profile(8) is None
    path.write_text("{bad")
    assert storage.load_apply_profile(7) is None
    storage.delete_user(7)
    assert not path.exists()


# ---------------- the page ----------------

def page():
    if not PAGE.exists():
        pytest.skip("docs not in the image")
    return PAGE.read_text()


def test_page_sites_match_python():
    keys = re.findall(r'^\s*\["(\w+)", "[^"]+", "(login|public)"', page(), re.M)
    assert [k for k, _ in keys] == list(SITES) and all(SITES[k]["kind"] == kind for k, kind in keys)


def test_page_fields_match_validator():
    names = set(re.findall(r'<(?:input|select|textarea)[^>]*\bname="(\w+)"', page()))
    expected = set(ap.TEXT) | {"total_years", "notice", "current_ctc_lpa", "expected_ctc_lpa", "relocate",
                               "sponsorship", "grad_year", "cgpa", "daily_cap", "consent", "dob", "relevant_years",
                               "work_mode", "twelfth", "tenth", "disability"}
    assert names == expected


def test_page_loads_only_telegram_and_has_no_backend():
    html = page()
    assert re.findall(r'<script src="([^"]+)"', html) == ["https://telegram.org/js/telegram-web-app.js"]
    assert "fetch(" not in html and "XMLHttpRequest" not in html and "localStorage" not in html
    assert "MAX_BYTES = 4096" in html and "sendData" in html and 'name="viewport"' in html


def test_form_url_roundtrip():
    url = ui.form_url("https://x.github.io/job-hunter/apply-form/#old", {"full_name": "Āsha", "sites": {"a": "b"}})
    assert url.startswith("https://x.github.io/job-hunter/apply-form/#d=")
    b64 = url.split("#d=")[1]
    assert json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))) == {"full_name": "Āsha",
                                                                                   "sites": {"a": "b"}}


def test_form_keyboard_is_web_app_reply_button():
    kb = ui.form_kb("https://example.org/f#d=x")
    btn = kb.keyboard[0][0]
    assert btn.web_app.url == "https://example.org/f#d=x" and kb.keyboard[1][0].text == ui.BTN_BACK
    assert ui.BTN_FORM in ui.MENU_BUTTONS and ui.BTN_FORM in [b.text for r in ui.MAIN_KB.keyboard for b in r]


# ---------------- bot: form ----------------

def with_resume(uid):
    add_user(uid)
    storage.save_resume(uid, "cv.txt", b"x", "Asha Verma\nasha@example.com +91 9876543210\n" + "x" * 50,
                        {**ME.to_dict(), "name": "Asha Verma"})


def test_cmd_form_prefills_from_resume_then_saved():
    with_resume(1)
    b = FakeBot()
    run(bot.cmd_form(make_update(uid=1, text=ui.BTN_FORM), make_ctx(b)))
    msg = b.sent[-1]
    url = msg.kw["reply_markup"].keyboard[0][0].web_app.url
    b64 = url.split("#d=")[1]
    data = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    assert data["full_name"] == "Asha Verma" and data["email"] == "asha@example.com" and data["total_years"] == 2.5
    assert "pre-filled" in msg.text
    storage.save_apply_profile(1, ap.from_form(FORM)[0].to_dict())
    run(bot.cmd_form(make_update(uid=1), make_ctx(b)))
    assert "saved answers" in b.sent[-1].text


def web_app_update(uid, raw):
    upd = make_update(uid=uid)
    upd.effective_message.web_app_data = SimpleNamespace(data=raw)
    return upd


def test_web_app_data_saved():
    with_resume(1)
    b = FakeBot()
    run(bot.on_web_app_data(web_app_update(1, json.dumps(FORM)), make_ctx(b)))
    saved = storage.load_apply_profile(1)
    assert saved["expected_ctc_lpa"] == 18.5 and saved["sites"]["greenhouse"] == "auto"
    t = b.sent[-1].text
    assert "saved" in t and "Greenhouse" in t and "Auto" in t and "Naukri" in t and "jobhunter connect naukri" in t
    assert "never sees your password" in t
    assert b.sent[-1].kw["reply_markup"] is ui.MAIN_KB


@pytest.mark.parametrize("raw,msg", [("{bad", "Not saved"), (json.dumps({**FORM, "email": "x"}), "email"),
                                     ("x" * 5000, "too large"), ("[1]", "Not saved")])
def test_web_app_data_rejected(raw, msg):
    with_resume(1)
    b = FakeBot()
    run(bot.on_web_app_data(web_app_update(1, raw), make_ctx(b)))
    assert msg in b.sent[-1].text and storage.load_apply_profile(1) is None


def test_web_app_data_only_for_active_users():
    db.upsert_user(5, 50, "p")                     # pending, not approved
    b = FakeBot()
    run(bot.on_web_app_data(web_app_update(5, json.dumps(FORM)), make_ctx(b)))
    assert storage.load_apply_profile(5) is None


# ---------------- ✍️ Prepare ----------------

KIT = {"cover_note": "I build RAG systems…", "answers": [{"question": "Notice period?", "answer": "30 days"}],
       "highlights": ["RAG at Acme", "Node.js APIs", "AWS"], "referral_note": "Hi [Name], could you refer me?"}


def test_parse_kit():
    k = ai.parse_kit({**KIT, "answers": KIT["answers"] * 12 + [{"question": "", "answer": "x"}, "bad"],
                      "highlights": ["a", "b", "c", "d", 5], "referral_note": "x " * 300})
    assert len(k["answers"]) == 8 and k["highlights"] == ["a", "b", "c"] and len(k["referral_note"]) <= 200
    assert ai.parse_kit({**KIT, "cover_note": ""}) is None and ai.parse_kit("x") is None


def test_client_prepare():
    c = ai.AIClient(base_url="http://ai", transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"kit": KIT})))
    assert run(c.prepare({}))["highlights"] == KIT["highlights"]
    c2 = ai.AIClient(base_url="http://ai", transport=httpx.MockTransport(
        lambda r: httpx.Response(429, json={"error": "x"})))
    assert run(c2.prepare({})) is None and "budget" in c2.last_error


def test_prep_messages_split_without_breaking_blocks():
    big = {**KIT, "answers": [{"question": f"Q{i}?", "answer": "a" * 800} for i in range(8)]}
    msgs = ui.prep_messages(big, "AI <Eng>", "Acme", ["phone"])
    assert len(msgs) > 1 and all(len(m) <= 3800 for m in msgs)
    assert all(m.count("<pre>") == m.count("</pre>") for m in msgs)
    assert "AI &lt;Eng&gt;" in msgs[0] and "missing phone" in msgs[-1]


def job_for(uid, jid="1"):
    db.upsert_cards([Job(id=jid, title="AI Engineer", company="Acme", location="Delhi", posted="5 minutes ago")])
    db.add_waiting(uid, [jid], "AI Engineer")
    db.set_result(uid, jid, "sent", 80, details={"match": {}})


class FakeClient:
    calls = 0

    def __init__(self, *a, **kw):
        self.last_error = ""

    async def prepare(self, body):
        FakeClient.calls += 1
        assert "email" not in body["profile"] and body["job"]["title"] == "AI Engineer"
        return KIT

    async def close(self):
        pass


def test_prepare_not_allowed_without_ai():
    with_resume(1)
    job_for(1)
    upd = make_update(uid=1, data="prep:1")
    run(bot.on_prepare_button(upd, make_ctx()))
    assert "hasn't turned on" in upd.callback_query.answer.call_args.args[0]


def test_prepare_writes_kit_and_caches(ai_on, monkeypatch):
    with_resume(1)
    db.update_user(1, is_admin=1)
    storage.save_apply_profile(1, ap.from_form(FORM)[0].to_dict())
    job_for(1)
    monkeypatch.setattr(bot.ai_mod, "AIClient", FakeClient)
    FakeClient.calls = 0
    b = FakeBot()
    run(bot.on_prepare_button(make_update(uid=1, data="prep:1"), make_ctx(b)))
    assert FakeClient.calls == 1 and any("Application kit" in m.text for m in b.sent)
    assert any("30 days" in m.text for m in b.sent) and db.get_prepared(1, "1") == KIT
    run(bot.on_prepare_button(make_update(uid=1, data="prep:1"), make_ctx(b)))
    assert FakeClient.calls == 1                       # cached
    db.delete_user(1)
    assert db.get_prepared(1, "1") is None


def test_prepare_failure_message(ai_on, monkeypatch):
    with_resume(1)
    db.update_user(1, is_admin=1)
    job_for(1)

    class Down(FakeClient):
        async def prepare(self, body):
            self.last_error = "offline (ConnectError)"
            return None
    monkeypatch.setattr(bot.ai_mod, "AIClient", Down)
    b = FakeBot()
    run(bot.on_prepare_button(make_update(uid=1, data="prep:1"), make_ctx(b)))
    b.sent[-1].edit_text.assert_called_once()
    assert "offline" in b.sent[-1].edit_text.call_args.args[0] and db.get_prepared(1, "1") is None


def test_job_keyboard_has_prepare():
    row = {"job_id": "9", "url": "https://x", "easy_apply": 1}
    datas = [b.callback_data for r in ui.job_kb(row).inline_keyboard for b in r]
    assert "prep:9" in datas and "ref:9" in datas


# ---------------- worker /prepare ----------------

sys.path.insert(0, str(ROOT / "ai"))


def test_worker_prepare_prompt():
    import server
    p = server.prepare_prompt({"resume": {"text": "CV"}, "profile": {"notice": "30", "expected_ctc_lpa": 18},
                               "job": {"title": "AI Eng", "description": "Do X"}})
    assert "<profile>" in p and "notice: 30" in p and p.index("<job>") < p.index("Do X")
    assert "never invent" in server.PREP_SYSTEM.lower() or "never" in server.PREP_SYSTEM.lower()
    with pytest.raises(server.AIError):
        server.prepare_prompt({"job": {"title": "x"}})
