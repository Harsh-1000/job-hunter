"""Everything the user sees: message texts and keyboards (no Telegram I/O here, so it's easy to test)."""
from __future__ import annotations

import base64
import html
import json
from collections import Counter

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo

from .filters import EXPERIENCE_LEVELS, JOB_TYPES, POSTED, WORK_TYPES, Filters, summary
from .resume import ResumeProfile

e = html.escape

# ---------- main menu (always visible under the message box) ----------
BTN_SCAN, BTN_FILTERS, BTN_RESUME, BTN_STATUS = "🔍 Find jobs now", "⚙️ Filters", "📄 My resume", "📊 Status"
BTN_SAVED, BTN_INSIGHTS, BTN_PAUSE, BTN_CLEAR, BTN_HELP = ("⭐ Saved", "💡 Insights", "⏯ Pause / Resume",
                                                           "🧹 Clear chat", "❓ Help")
BTN_ALIVE = "🟢 Is it working?"
BTN_FORM = "📝 Application form"
BTN_BACK = "⬅️ Back to menu"
MENU_BUTTONS = [BTN_ALIVE, BTN_FORM, BTN_SCAN, BTN_FILTERS, BTN_RESUME, BTN_STATUS, BTN_SAVED, BTN_INSIGHTS, BTN_PAUSE,
                BTN_CLEAR, BTN_HELP]
MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_ALIVE, BTN_FORM], [BTN_SCAN, BTN_FILTERS], [BTN_RESUME, BTN_STATUS], [BTN_SAVED, BTN_INSIGHTS],
     [BTN_PAUSE, BTN_CLEAR, BTN_HELP]],
    resize_keyboard=True, is_persistent=True)

HELP = """<b>🎯 Job Hunter</b>
I watch LinkedIn for new jobs that fit <b>your</b> resume and send them the moment they're posted, with a match %.

<b>Get started</b>
1️⃣ Send me your resume (PDF / DOCX / TXT): I read your skills and experience
2️⃣ Check <b>⚙️ Filters</b>: keywords, location, experience level, job/work type, date posted, match threshold…
3️⃣ Sit back. New matches arrive automatically, every few minutes.

<b>Buttons on each job</b>: 🔗 open on LinkedIn · ✅ Applied · ⭐ Save · 🙈 Hide

<b>Commands</b>
/scan – search right now
/filters – all filters
/titles – add / remove job titles (search keywords)
/addtitle ML Engineer, Python Developer – add titles
/keywords AI Engineer, GenAI Engineer – replace all titles
/location India, Remote – set locations
/threshold 20 – only send jobs with match ≥ 20%
/years 2.5 – set your experience
/resume – your resume · /saved – saved & applied
/insights – skills you're missing most
/alive – 🟢 is it working? (is the bot hunting for me right now)
/form – 📝 application form (fill once; choose auto-apply sites)
🤖 Claude judges the fit when your admin allowed it (⚙️ Filters → 🤖 to switch off)
/status – stats · /pause – pause or resume alerts
/clear – clear this chat · /deleteme – delete all your data
/intro – 📖 guided tour of every feature"""


def onboarding(name: str) -> str:
    return (f"👋 Hi {e(name or 'there')}! I'm <b>Job Hunter</b>.\n\n"
            "Send me your <b>resume</b> (PDF, DOCX or TXT) and I'll find LinkedIn jobs that match it, "
            "as soon as they're posted, with a match %.\n\n"
            "New here? Tap <b>📖 Take the tour</b> (1 minute) or send /intro any time.")


# ---------- guided tour (/intro) ----------

INTRO_PAGES: list[tuple[str, str]] = [
    ("🎯 What is Job Hunter?",
     "I'm your personal LinkedIn job scout.\n\n"
     "Every 3 minutes I search LinkedIn for jobs <b>posted in the last few minutes</b>, read each description, "
     "compare it with <b>your resume</b>, and message you only the ones that fit, with a "
     "<b>match %</b>.\n\n"
     "✅ No LinkedIn account or login needed\n"
     "✅ Jobs usually reach you minutes after they're posted\n"
     "✅ Every user gets their own resume, filters and alerts"),
    ("📄 Step 1: Send your resume",
     "Send your resume as a <b>file</b>: PDF, DOCX or TXT (max 5 MB).\n\n"
     "I read it and pull out:\n"
     "• your <b>skills</b> (Node.js, Python, RAG, AWS…)\n"
     "• your <b>years of experience</b> (from your job dates)\n"
     "• your <b>job titles</b>, plus <b>suggested search keywords</b>\n\n"
     "Wrong years? Fix them with <code>/years 2.5</code>.\n"
     "Send a new file any time to replace it. Tap <b>📄 My resume</b> to see what I found."),
    ("⚙️ Step 2: Set your filters",
     "Tap <b>⚙️ Filters</b>. The same filters LinkedIn has:\n"
     "🔎 Job titles / keywords: add or remove as many as you like (up to 10)\n"
     "📍 Location (default <b>Delhi NCR</b>: Delhi, Gurugram, Noida) · 🕒 Date posted: default <b>⚡ Just posted</b> (only jobs a few minutes old), "
     "or past hour / 6 h / 24 h / week / month\n"
     "🎓 Experience level · 📄 Job type · 🏠 On-site / Remote / Hybrid\n"
     "⚡ Easy Apply only: <b>ON by default</b>, so every job can be applied to in a minute on LinkedIn. "
     "Turn it off to also get jobs that apply on the company's website.\n\n"
     "Plus extra filters of mine:\n"
     "🎯 Match threshold · 🧮 Max years a job may ask · 🚫 Skip title words (senior, lead…)\n"
     "🏢 Skip companies · 👥 Max applicants · 🌙 Quiet hours (no pings at night)\n\n"
     "Quick commands: <code>/addtitle ML Engineer, Python Developer</code> · <code>/titles</code> · "
     "<code>/location India</code>"),
    ("🎯 Step 3: The match %",
     "Each job gets a score from 0 to 100:\n\n"
     "• <b>65%</b>: how many of the skills the job asks for are on your resume\n"
     "• <b>20%</b>: how well the job title fits your keywords\n"
     "• <b>15%</b>: whether your experience fits what they ask\n\n"
     "Set a <b>threshold</b> to get only good matches, e.g. <code>/threshold 40</code> sends only jobs ≥ 40%.\n"
     "Default is <b>0</b>: you get every relevant job.\n\n"
     "I skip jobs that ask for far more years than you have, and fresher-only roles.\n\n"
     "🤖 <b>Claude judge</b> (when the admin turned it on for you): jobs that pass these checks are also read "
     "by Claude, like a recruiter would. You then see <b>Claude's %</b>, a one-line <i>why</i> and what's "
     "missing, and jobs Claude calls “not a fit” are skipped. Switch it off in ⚙️ Filters → 🤖."),
    ("📬 Step 4: Job alerts",
     "Each alert shows the title, company, location, when it was posted, applicants, experience asked, "
     "your <b>match %</b>, the skills <b>you have</b> and the skills <b>they also want</b>.\n\n"
     "Buttons on every job:\n"
     "🔗 <b>Open on LinkedIn</b>: read & apply\n"
     "✅ <b>Applied</b>: track what you applied to\n"
     "⭐ <b>Save</b>: keep it for later\n"
     "🙈 <b>Hide</b>: not for me (↩️ Undo is available)\n"
     "🤝 <b>Referral message</b>: ready-to-send referral request for that job (connection note, full "
     "message, follow-up) + links to find people at the company\n\n"
     "The same role posted in several cities is sent only once."),
    ("🧭 The menu buttons",
     "🟢 <b>Is it working?</b>: proof I'm hunting for you right now: last scan, next scan, today's numbers\n"
     "📝 <b>Application form</b>: fill once (contact, notice, CTC, education) + choose the sites Claude may "
     "apply on for you\n"
     "🔍 <b>Find jobs now</b>: search right away (otherwise automatic every few minutes)\n"
     "⚙️ <b>Filters</b>: everything you can tune\n"
     "📄 <b>My resume</b>: what I read from it, suggested keywords\n"
     "📊 <b>Status</b>: last/next scan, how many sent, applied, queued\n"
     "⭐ <b>Saved</b>: your saved & applied jobs\n"
     "💡 <b>Insights</b>: skills jobs want that you don't have yet (what to learn next)\n"
     "⏯ <b>Pause / Resume</b>: stop or restart alerts\n"
     "🧹 <b>Clear chat</b>: tidy this chat (optionally keep job alerts)\n"
     "🌅 Every morning at 9:00 you get a <b>daily report</b> of the last 24 h (turn it off in Filters)\n"
     "❓ <b>Help</b>: commands list · /intro: this tour"),
    ("🔒 Privacy & tips",
     "• Your resume is stored only on this bot's server, in a folder that belongs to your Telegram ID. "
     "It is never shared or sent anywhere.\n"
     "• <code>/deleteme</code> deletes your resume, filters and history completely.\n\n"
     "<b>Tips</b>\n"
     "• Use 3–6 specific keywords (e.g. <i>Node.js Developer</i> rather than <i>Developer</i>)\n"
     "• Too many alerts? Raise the threshold or narrow the date posted\n"
     "• Too few? Add keywords, set threshold to 0, add 'Remote' as a location\n\n"
     "Ready? Send your resume now 📎"),
]


def intro_text(page: int) -> str:
    page = max(0, min(page, len(INTRO_PAGES) - 1))
    title, body = INTRO_PAGES[page]
    return f"<b>{title}</b>\n\n{body}"


def intro_kb(page: int) -> InlineKeyboardMarkup:
    page = max(0, min(page, len(INTRO_PAGES) - 1))
    last = len(INTRO_PAGES) - 1
    nav = []
    if page > 0:
        nav.append(B("◀️ Back", callback_data=f"i:{page - 1}"))
    nav.append(B(f"{page + 1}/{last + 1}", callback_data=f"i:{page}"))
    nav.append(B("Next ▶️", callback_data=f"i:{page + 1}") if page < last else
               B("🚀 Get started", callback_data="i:done"))
    return InlineKeyboardMarkup([nav])


TOUR_KB = InlineKeyboardMarkup([[B("📖 Take the tour", callback_data="i:0")]])
AI_LABELS = {"strong": "Strong fit", "good": "Good fit", "stretch": "Stretch", "skip": "Not a fit"}


# ---------- job alert ----------

def job_text(row, state: str = "") -> str:
    d = json.loads(row["details"] or "{}")
    m = d.get("match", {})
    lines = [f"🆕 <b>{e(row['title'] or '')}</b>",
             f"🏢 {e(row['company'] or '')}  ·  📍 {e(row['location'] or '')}"]
    meta = " · ".join(x for x in [row["posted"], row["applicants"]] if x)
    if meta:
        lines.append(f"🕒 {e(meta)}")
    kind = " · ".join(x for x in [row["seniority"], row["employment_type"]] if x and x != "Not Applicable")
    if kind:
        lines.append(f"💼 {e(kind)}")
    lines.append(f"🎓 Experience asked: {e(m.get('years_text', 'not specified'))}")
    ea = _get(row, "easy_apply")
    if ea == 1:
        lines.append("⚡ <b>Easy Apply</b> — apply in a minute on LinkedIn")
    elif ea == 0:
        lines.append("🌐 Applies on the company's website")
    score = row["score"]
    ai = d.get("ai")
    if score is None:
        lines.append("🎯 Match: <i>send your resume to see a match %</i>")
    else:
        bar = "🟩" * round(score / 20) + "⬜" * (5 - round(score / 20))
        if ai:
            lines.append(f"🤖 <b>Claude: {score}% · {e(AI_LABELS.get(ai.get('verdict'), ''))}</b> {bar}")
            lines.append(f"💬 <i>{e(ai.get('why', ''))}</i>")
            if d.get("ai_rescued"):
                lines.append("🔍 <i>Found by Claude: the keyword match had missed this one</i>")
        else:
            lines.append(f"🎯 <b>Match {score}%</b> {bar}"
                         + (" <i>(keyword match — Claude unavailable)</i>" if d.get("ai_offline") else ""))
        if m.get("matched"):
            lines.append(f"✅ You have: {e(', '.join(m['matched'][:8]))}")
        missing = (ai or {}).get("missing") or m.get("missing")
        if missing:
            lines.append(f"📚 They also want: {e(', '.join(missing[:6]))}")
    if d.get("keyword"):
        lines.append(f"🔎 <i>via “{e(d['keyword'])}”</i>")
    if state:
        lines.append(f"\n{state}")
    return "\n".join(lines)


def _get(row, key):
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def job_kb(row, state: str = "") -> InlineKeyboardMarkup:
    jid = row["job_id"]
    label = "⚡ Easy Apply on LinkedIn" if _get(row, "easy_apply") == 1 else "🔗 Open on LinkedIn"
    rows = [[B(label, url=row["url"])]]
    if not state:
        rows.append([B("✅ Applied", callback_data=f"j:applied:{jid}"), B("⭐ Save", callback_data=f"j:saved:{jid}"),
                     B("🙈 Hide", callback_data=f"j:hidden:{jid}")])
    else:
        rows.append([B("↩️ Undo", callback_data=f"j:sent:{jid}")])
    rows.append([B("🤝 Referral message", callback_data=f"ref:{jid}"),
                 B("✍️ Prepare application", callback_data=f"prep:{jid}")])
    return InlineKeyboardMarkup(rows)


def prep_messages(kit: dict, title: str, company: str, missing: list[str], limit: int = 3800) -> list[str]:
    """The ✍️ Prepare kit as one or more messages (Telegram max ~4096 chars; blocks are never split)."""
    blocks = [f"✍️ <b>Application kit</b> — {e(title)} at {e(company)}\n"
              "<i>Written by Claude from your resume and 📝 form. Tap a grey box to copy it.</i>",
              f"<b>📄 Cover note</b>\n<pre>{e(kit['cover_note'])}</pre>"]
    if kit.get("answers"):
        blocks.append("<b>❓ Likely questions</b>")
        blocks += [f"<b>{e(a['question'])}</b>\n<pre>{e(a['answer'])}</pre>" for a in kit["answers"]]
    if kit.get("highlights"):
        blocks.append("<b>⭐ Stress these from your resume</b>\n" + "\n".join(f"• {e(h)}" for h in kit["highlights"]))
    if kit.get("referral_note"):
        blocks.append(f"<b>🤝 Referral note</b> ({len(kit['referral_note'])}/200)\n<pre>{e(kit['referral_note'])}</pre>")
    if missing:
        blocks.append(f"⚠️ Your 📝 form is missing {e(', '.join(missing))}: some answers may say “Open to discussion”.")
    out, cur = [], ""
    for blk in blocks:
        if cur and len(cur) + 2 + len(blk) > limit:
            out.append(cur)
            cur = blk
        else:
            cur = f"{cur}\n\n{blk}" if cur else blk
    if cur:
        out.append(cur)
    return out


def referral_text(ref, title: str, company: str) -> str:
    return (f"🤝 <b>Referral messages</b> — {e(title)} at {e(company)}\n"
            "<i>Tap a grey box to copy it. Replace [Name] with the person's first name.</i>\n\n"
            f"<b>1️⃣ Connection request note</b> ({len(ref.note)}/200 characters)\n"
            f"<pre>{e(ref.note)}</pre>\n\n"
            "<b>2️⃣ Full message</b>: send after they accept, or as a DM / email\n"
            f"<pre>{e(ref.message)}</pre>\n\n"
            f"<b>✉️ Email subject</b>\n<pre>{e(ref.subject)}</pre>\n\n"
            f"<b>3️⃣ Follow-up</b>: if there's no reply in 4–5 days\n<pre>{e(ref.followup)}</pre>\n\n"
            "💡 <b>Who to ask:</b> 2–3 people in a similar role (they get referral bonuses), or a recruiter. "
            "Be quick: referrals work best in the first days after a job is posted.")


def referral_kb(ref, company: str, job_id: str = "") -> InlineKeyboardMarkup:
    short = company[:22]
    rows = [[B(f"🔎 People at {short}", url=ref.employees_url)],
            [B(f"🔎 Recruiters at {short}", url=ref.recruiters_url)]]
    if job_id:
        rows.append([B("🤖 Personal message by Claude", callback_data=f"refai:{job_id}")])
    return InlineKeyboardMarkup(rows)


REFERRAL_PERSON_PROMPT = (
    "🤖 <b>Who are you asking for the referral?</b>\n\n"
    "Send their name and anything that helps, in one message, for example:\n"
    "<code>Priya Sharma, Senior SDE there, we were at DTU together</code>\n"
    "<code>Rahul, recruiter, met at a meetup</code>\n"
    "<code>Ankit</code>\n\n"
    "Claude writes the note, message and follow-up to that person, from your resume and this job. "
    "Send <code>-</code> for a general one.")


def ai_referral_text(ref: dict, title: str, company: str, person: str) -> str:
    who = f" to <b>{e(person)}</b>" if person else ""
    return (f"🤖 <b>Referral messages by Claude</b>{who} — {e(title)} at {e(company)}\n"
            "<i>Tap a grey box to copy it. Check it reads like you before sending.</i>\n\n"
            f"<b>1️⃣ Connection request note</b> ({len(ref['note'])}/200 characters)\n"
            f"<pre>{e(ref['note'])}</pre>\n\n"
            "<b>2️⃣ Full message</b>: after they accept, or as a DM / email\n"
            f"<pre>{e(ref['message'])}</pre>\n\n"
            f"<b>✉️ Email subject</b>\n<pre>{e(ref['subject'])}</pre>\n\n"
            f"<b>3️⃣ Follow-up</b>: if there's no reply in 4–5 days\n<pre>{e(ref['followup'])}</pre>")


JOB_STATES = {"applied": "✅ <b>Applied</b>", "saved": "⭐ <b>Saved</b>", "hidden": "🙈 <i>Hidden</i>", "sent": ""}


# ---------- filters menu ----------

def filters_text(f: Filters, resume_years: float | None) -> str:
    return "<b>⚙️ Your filters</b>\n\n" + e(summary(f, resume_years)) + "\n\n<i>Tap what you want to change.</i>"


def filters_kb(f: Filters) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [B("🔎 Job titles / keywords", callback_data="f:kw")],
        [B("📍 Location", callback_data="f:loc"), B("🕒 Date posted", callback_data="f:posted")],
        [B("🎓 Experience level", callback_data="f:exp"), B("📄 Job type", callback_data="f:jt")],
        [B("🏠 On-site/Remote", callback_data="f:wt"), B(f"⚡ Easy Apply only: {'ON' if f.easy_apply else 'off'}", callback_data="f:ea")],
        [B("🎯 Match threshold", callback_data="f:thr"), B("🧮 Max years asked", callback_data="f:maxy")],
        [B("👤 My experience", callback_data="f:myy"), B("👥 Max applicants", callback_data="f:apps")],
        [B("🚫 Skip title words", callback_data="f:excl"), B("🏢 Skip companies", callback_data="f:comp")],
        [B("🌙 Quiet hours", callback_data="f:quiet"),
         B(f"🌅 Daily report: {'ON' if f.daily_report else 'off'}", callback_data="f:daily")],
        [B(f"🤖 Claude judges the fit: {'ON' if f.ai_judge else 'off'}", callback_data="f:ai")],
        [B("♻️ Reset", callback_data="f:reset"), B("✅ Done", callback_data="f:done")],
    ])


def multi_kb(field: str, table: dict[str, str], selected: list[str]) -> InlineKeyboardMarkup:
    btns = [B(("☑️ " if code in selected else "◻️ ") + label, callback_data=f"f:{field}:{code}")
            for code, label in table.items()]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([B("Any (clear)", callback_data=f"f:{field}:any"), B("⬅️ Back", callback_data="f:menu")])
    return InlineKeyboardMarkup(rows)


MULTI = {"exp": ("experience_levels", EXPERIENCE_LEVELS, "🎓 Experience level (LinkedIn filter)"),
         "jt": ("job_types", JOB_TYPES, "📄 Job type"),
         "wt": ("work_types", WORK_TYPES, "🏠 On-site / Remote / Hybrid")}


def posted_kb(current: str) -> InlineKeyboardMarkup:
    rows = [[B(("🔘 " if code == current else "⚪ ") + label, callback_data=f"f:posted:{code}")]
            for code, label in POSTED.items()]
    rows.append([B("⬅️ Back", callback_data="f:menu")])
    return InlineKeyboardMarkup(rows)


def threshold_kb(current: int) -> InlineKeyboardMarkup:
    vals = [0, 10, 20, 30, 40, 50, 60, 70, 80]
    btns = [B(("🔘 " if v == current else "") + ("All" if v == 0 else f"≥{v}%"), callback_data=f"f:thr:{v}")
            for v in vals]
    return InlineKeyboardMarkup([btns[0:3], btns[3:6], btns[6:9],
                                 [B("✏️ Custom", callback_data="f:thr:custom"), B("⬅️ Back", callback_data="f:menu")]])


def maxyears_kb(current: float | None) -> InlineKeyboardMarkup:
    opts = [("Auto", "auto"), ("1", "1"), ("2", "2"), ("3", "3"), ("4", "4"), ("5", "5"), ("6", "6"), ("8", "8"),
            ("10", "10"), ("Any", "any")]
    cur = "auto" if current is None else ("any" if current < 0 else f"{current:g}")
    btns = [B(("🔘 " if v == cur else "") + label, callback_data=f"f:maxy:{v}") for label, v in opts]
    return InlineKeyboardMarkup([btns[0:5], btns[5:10], [B("⬅️ Back", callback_data="f:menu")]])


def keywords_text(f: Filters, p: ResumeProfile | None) -> str:
    from .filters import MAX_KEYWORDS
    lines = [f"<b>🔎 Job titles / keywords</b> ({len(f.keywords)}/{MAX_KEYWORDS})",
             "I run one LinkedIn search per title (per location).", ""]
    lines += [f"• {e(k)}" for k in f.keywords] or ["— none yet —"]
    extra = [s for s in (p.suggested_keywords if p else []) if s.lower() not in {k.lower() for k in f.keywords}]
    if extra:
        lines += ["", f"📄 From your resume, not added yet: {e(', '.join(extra))}"]
    lines += ["", "<i>Tap ❌ to remove a title, ➕ to add more.</i>"]
    return "\n".join(lines)


def keywords_kb(f: Filters, p: ResumeProfile | None) -> InlineKeyboardMarkup:
    rm = [B(f"❌ {k[:28]}", callback_data=f"f:kwrm:{i}") for i, k in enumerate(f.keywords)]
    rows = [rm[i:i + 2] for i in range(0, len(rm), 2)]
    rows.append([B("➕ Add titles", callback_data="f:kwadd"), B("✏️ Replace all", callback_data="f:kwset")])
    if p and any(s.lower() not in {k.lower() for k in f.keywords} for s in p.suggested_keywords):
        rows.append([B("📄 Add resume suggestions", callback_data="f:kwsug")])
    rows.append([B("⬅️ Back to filters", callback_data="f:menu")])
    return InlineKeyboardMarkup(rows)


CANCEL_KB = InlineKeyboardMarkup([[B("✖️ Cancel", callback_data="f:menu")]])

PROMPTS = {
    "kwadd": "➕ Send the <b>job titles to add</b>, separated by commas.\n"
             "e.g. <code>ML Engineer, Python Developer, Full Stack Developer</code>",
    "kw": "✏️ Send your <b>full list of job titles / keywords</b>, separated by commas (max 10). "
          "This replaces the current list.\n"
          "e.g. <code>AI Engineer, Generative AI Engineer, Backend Developer</code>",
    "loc": "📍 Send <b>locations</b>, separated by commas (max 3). Default: Delhi NCR.\n"
           "e.g. <code>Delhi NCR</code>, <code>Bengaluru, Pune</code> or <code>India</code> (whole country)",
    "excl": "🚫 Send <b>words that should skip a job</b> when in the title, comma separated "
            "(or <code>none</code>).\ne.g. <code>senior, lead, manager, intern</code>",
    "comp": "🏢 Send <b>companies to skip</b>, comma separated (or <code>none</code>).",
    "apps": "👥 Skip jobs with more than how many <b>applicants</b>? Send a number like <code>100</code> "
            "or <code>any</code>.",
    "quiet": "🌙 Send <b>quiet hours</b> (no alerts, they wait until morning), like <code>23-8</code>, "
             "or <code>off</code>.",
    "myy": "👤 Send <b>your years of experience</b>, like <code>2.5</code> (or <code>auto</code> to use your resume).",
    "thr": "🎯 Send the <b>minimum match %</b> (0–100). <code>0</code> sends everything.",
}


# ---------- resume ----------

def resume_text(p: ResumeProfile, f: Filters) -> str:
    years = f"{p.years:g} yrs" if p.years is not None else "not found"
    src = {"dates": "from your job dates", "stated": "as stated in your resume"}.get(p.years_source, "")
    skills = ", ".join(p.skills[:25]) + (f" +{len(p.skills) - 25} more" if len(p.skills) > 25 else "")
    return (f"📄 <b>Your resume</b> — <code>{e(p.file_name)}</code>\n\n"
            f"👤 Experience: <b>{e(years)}</b> {e(src)}"
            + (f" · using <b>{f.my_years:g}</b> (set by you)" if f.my_years is not None else "") + "\n"
            f"🧠 Skills ({len(p.skills)}): {e(skills) or '—'}\n"
            f"💼 Titles found: {e(', '.join(p.titles)) or '—'}\n\n"
            f"🔎 Suggested keywords: <b>{e(', '.join(p.suggested_keywords))}</b>\n"
            f"🔎 Your keywords now: {e(', '.join(f.keywords)) or '—'}\n\n"
            "<i>Send a new file any time to replace it.</i>")


def resume_kb(has_resume: bool) -> InlineKeyboardMarkup:
    rows = [[B("✅ Use suggested keywords", callback_data="r:usekw"), B("✏️ Edit job titles", callback_data="f:kw")],
            [B("👤 Set my years", callback_data="f:myy"), B("🔍 Find jobs now", callback_data="s:scan")]]
    if has_resume:
        rows.append([B("🗑 Delete my resume", callback_data="r:delete")])
    return InlineKeyboardMarkup(rows)


# ---------- clear chat ----------

CLEAR_KB = InlineKeyboardMarkup([
    [B("🧹 Clear everything", callback_data="c:all")],
    [B("🧹 Clear, but keep job alerts", callback_data="c:keepjobs")],
    [B("✖️ Cancel", callback_data="c:cancel")],
])


# ---------- "is it working?" ----------

def _ago(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    if minutes < 1:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)} min ago"
    return f"{minutes / 60:.1f} h ago"


def health_verdict(last_age_min: float | None, last_error: str, cooldown: str, interval: float,
                   uptime_min: float) -> str:
    """ok | starting | slow | down — from how long ago the last scan cycle ran and how it went."""
    if cooldown:
        return "slow"
    if last_age_min is None:
        return "starting" if uptime_min <= 2 * interval + 1 else "down"
    if last_age_min > 3 * interval + 1:
        return "down"
    return "slow" if last_error else "ok"


VERDICTS = {
    "ok": "🟢 <b>Yes — I'm hunting for you right now.</b>",
    "starting": "🟢 <b>Just restarted</b> — the first scan starts in a moment.",
    "slow": "🟡 <b>Working, but slowed down.</b>",
    "down": "🔴 <b>Something's wrong — scans have stopped.</b> The admin has been alerted.",
}


AI_STATES = {"ok": "✅ working", "offline": "⚠️ offline (keyword match is used meanwhile)",
             "budget": "🐢 hourly limit reached (keyword match until it resets)", "unknown": "⏳ not used yet",
             "nologin": "🔑 not connected to Claude yet (keyword match is used)"}


def alive_text(s: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """s: verdict, last (dict|None), last_age, next_in, cooldown, error, interval, today, uptime_h, paused,
    keywords, locations, posted, easy_apply, has_resume."""
    lines = [VERDICTS[s["verdict"]]]
    kb_rows = []
    if s["verdict"] == "slow":
        lines.append(f"LinkedIn asked me to slow down — back at full speed at {e(s['cooldown'])} UTC."
                     if s["cooldown"] else f"Last scan had a problem: {e(s['error'])}. Retrying automatically.")
    if s["paused"]:
        lines.append("\n⏸ <b>But your alerts are paused</b> — I'm not searching for you.")
        kb_rows.append([B("▶️ Resume my alerts", callback_data="alive:resume")])
    if not s["keywords"]:
        lines.append("\n⚠️ <b>You have no job titles set</b> — I have nothing to search for.")
        kb_rows.append([B("🔎 Add job titles", callback_data="f:kw")])
    if not s["has_resume"]:
        lines.append("📄 No resume yet: jobs come without a match %. Send it as a file.")
    last = s["last"]
    lines.append("")
    if last:
        lines.append(f"🔁 Last scan: <b>{_ago(s['last_age'])}</b> — {last['searches']} searches, "
                     f"{last['listings']} listings, {last['new_jobs']} new")
    else:
        lines.append("🔁 Last scan: not yet since restart")
    if s["next_in"] is not None:
        lines.append(f"⏭ Next scan: in {max(0, round(s['next_in']))} min (every {s['interval']:g} min, day and night)")
    t = s["today"]
    lines.append(f"📅 Today for you: checked <b>{t['checked']}</b> new jobs · matched {t['matched']} · "
                 f"sent {t['sent']}" + (f" · best {t['best']}%" if t.get("best") is not None else ""))
    if s["keywords"]:
        lines.append(f"🔎 Searching: {e(', '.join(s['keywords']))}")
        lines.append(f"📍 {e(', '.join(s['locations']))} · 🕒 {e(s['posted'])}"
                     + (" · ⚡ Easy Apply only" if s["easy_apply"] else ""))
    ai = s.get("ai")
    if ai and ai.get("enabled"):
        lines.append("🤖 Claude judge: " + AI_STATES.get(ai["state"], ai["state"])
                     + (f" — {e(ai['error'])}" if ai.get("error") else ""))
    lines.append(f"🖥 Bot running for {s['uptime_h']}")
    if s["verdict"] == "ok" and not t["sent"]:
        lines.append("\n<i>No jobs yet today? That's normal: I only send fresh jobs that match your filters. "
                     "Widen 🕒 Date posted or turn off ⚡ Easy Apply in ⚙️ Filters for more.</i>")
    return "\n".join(lines), (InlineKeyboardMarkup(kb_rows) if kb_rows else None)


def daily_report_text(name: str, w: dict, cycles: dict, interval: float) -> str:
    ran = "✅ scans ran all day" if cycles["cycles"] >= (24 * 60 / interval) * 0.8 else \
        f"⚠️ only {cycles['cycles']} scans ran (expected ~{int(24 * 60 / interval)})"
    lines = [f"☀️ <b>Good morning{', ' + e(name) if name else ''}!</b> Your last 24 hours:",
             f"🔎 Checked <b>{w['checked']}</b> new jobs for you",
             f"🎯 Matched <b>{w['matched']}</b>" + (f" · best match {w['best']}%" if w.get("best") is not None else ""),
             f"📬 Sent you <b>{w['sent']}</b>",
             f"🖥 {ran} ({cycles['listings']} LinkedIn listings looked at)"]
    if not w["sent"]:
        lines.append("\n<i>Quiet day? Add job titles or widen 🕒 Date posted in ⚙️ Filters.</i>")
    lines.append("\n<i>Turn this report off in ⚙️ Filters → 🌅 Daily report.</i>")
    return "\n".join(lines)


# ---------- 📝 application form (Telegram Mini App) ----------

def form_url(base: str, data: dict) -> str:
    """The form page with the current answers in the #hash (never sent to any server)."""
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()
    return base.split("#")[0] + "#d=" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def form_kb(url: str) -> ReplyKeyboardMarkup:
    # Only a reply-keyboard Mini App button can send data back (Telegram.WebApp.sendData).
    return ReplyKeyboardMarkup([[KeyboardButton("📝 Open my application form", web_app=WebAppInfo(url=url))],
                                [BTN_BACK]], resize_keyboard=True, one_time_keyboard=True)


def form_intro_text(saved: bool) -> str:
    return ("<b>📝 Application form</b>\n"
            + ("Your saved answers are filled in, so change what you like and tap <b>Save</b>.\n"
               if saved else "Fill it <b>once</b>: I've pre-filled what I could read from your resume.\n")
            + "It holds what job forms usually ask (contact, notice period, CTC, education) and "
              "<b>which sites Claude may apply on for you</b> (Off / Ask me / Auto).\n\n"
              "👇 Tap the button below the message box to open it.\n"
              "<i>Private: the page runs inside Telegram, and your answers come straight back to this bot.</i>")


def apply_profile_text(p, login_sites: list[str], signed_in: dict | None = None) -> str:
    from .apply_sites import MODE_LABELS, SITES
    lines = ["✅ <b>Application form saved</b>",
             f"👤 {e(p.full_name)} · {e(p.city)} · {p.total_years if p.total_years is not None else '?'} yrs · "
             f"notice {e(p.notice if p.notice == 'immediate' else p.notice + ' days')}"]
    if p.expected_ctc_lpa is not None:
        lines.append(f"💰 Expected {p.expected_ctc_lpa:g} LPA" + (f" (current {p.current_ctc_lpa:g})"
                                                                 if p.current_ctc_lpa is not None else ""))
    active = p.active_sites()
    if active:
        lines.append("\n🤖 <b>Auto-apply on:</b>")
        lines += [f"  • {e(SITES[k]['label'])}: <b>{MODE_LABELS[m]}</b>" for k, m in active.items()]
        lines.append(f"  Daily limit: {p.daily_cap}")
        signed_in = signed_in or {}
        todo = [k for k in login_sites if k not in signed_in]
        done = [k for k in login_sites if k in signed_in]
        if done:
            lines.append("\n🔐 Signed in: " + e(", ".join(SITES[k]["label"] for k in done)))
        if todo:
            lines.append("\n🔑 <b>Sign in once</b> to: " + e(", ".join(SITES[k]["label"] for k in todo)) + ".\n"
                         "On the server run " + ", ".join(f"<code>jobhunter connect {k}</code>" for k in todo)
                         + ": Chrome opens the real site, you log in there yourself (OTP / CAPTCHA too), "
                           "and only that site's sign-in is saved. The bot never sees your password.")
    else:
        lines.append("\n🤖 Auto-apply: off for every site (you can still use ✍️ Prepare on any job).")
    if p.missing():
        lines.append("\n⚠️ Still missing: " + e(", ".join(p.missing())))
    return "\n".join(lines)


# ---------- insights ----------

def insights_text(rows) -> str:
    if not rows:
        return "💡 No data yet. Insights appear after a few jobs have been matched."
    missing, have, companies = Counter(), Counter(), Counter()
    scores = []
    for r in rows:
        m = json.loads(r["details"] or "{}").get("match", {})
        missing.update(m.get("missing", []))
        have.update(m.get("matched", []))
        companies[r["company"]] += 1
        if r["score"] is not None:
            scores.append(r["score"])
    lines = [f"💡 <b>Insights</b> — last 30 days, {len(rows)} matching jobs"]
    if scores:
        lines.append(f"🎯 Average match: <b>{sum(scores) // len(scores)}%</b> · best {max(scores)}%")
    if missing:
        lines.append("\n📚 <b>Learn next</b> (asked by jobs you matched, not on your resume):")
        lines += [f"  • {e(s)} — {n} jobs" for s, n in missing.most_common(8)]
    if have:
        lines.append("\n💪 <b>Your most wanted skills</b>: " + e(", ".join(s for s, _ in have.most_common(8))))
    if companies:
        lines.append("\n🏢 <b>Most active companies</b>: " + e(", ".join(f"{c} ({n})" for c, n in
                                                                      companies.most_common(5))))
    return "\n".join(lines)
