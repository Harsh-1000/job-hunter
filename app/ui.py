"""Everything the user sees: message texts and keyboards (no Telegram I/O here, so it's easy to test)."""
from __future__ import annotations

import html
import json
from collections import Counter

from telegram import InlineKeyboardButton as B
from telegram import InlineKeyboardMarkup, ReplyKeyboardMarkup

from .filters import EXPERIENCE_LEVELS, JOB_TYPES, POSTED, WORK_TYPES, Filters, summary
from .resume import ResumeProfile

e = html.escape

# ---------- main menu (always visible under the message box) ----------
BTN_SCAN, BTN_FILTERS, BTN_RESUME, BTN_STATUS = "🔍 Find jobs now", "⚙️ Filters", "📄 My resume", "📊 Status"
BTN_SAVED, BTN_INSIGHTS, BTN_PAUSE, BTN_CLEAR, BTN_HELP = ("⭐ Saved", "💡 Insights", "⏯ Pause / Resume",
                                                           "🧹 Clear chat", "❓ Help")
MENU_BUTTONS = [BTN_SCAN, BTN_FILTERS, BTN_RESUME, BTN_STATUS, BTN_SAVED, BTN_INSIGHTS, BTN_PAUSE, BTN_CLEAR,
                BTN_HELP]
MAIN_KB = ReplyKeyboardMarkup(
    [[BTN_SCAN, BTN_FILTERS], [BTN_RESUME, BTN_STATUS], [BTN_SAVED, BTN_INSIGHTS], [BTN_PAUSE, BTN_CLEAR, BTN_HELP]],
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
     "Every few minutes I search LinkedIn for <b>new</b> jobs (newest first), read each job description, "
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
     "📍 Location · 🕒 Date posted (1 h / 6 h / 24 h / week)\n"
     "🎓 Experience level · 📄 Job type · 🏠 On-site / Remote / Hybrid · ⚡ Easy Apply\n\n"
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
     "I skip jobs that ask for far more years than you have, and fresher-only roles."),
    ("📬 Step 4: Job alerts",
     "Each alert shows the title, company, location, when it was posted, applicants, experience asked, "
     "your <b>match %</b>, the skills <b>you have</b> and the skills <b>they also want</b>.\n\n"
     "Buttons on every job:\n"
     "🔗 <b>Open on LinkedIn</b>: read & apply\n"
     "✅ <b>Applied</b>: track what you applied to\n"
     "⭐ <b>Save</b>: keep it for later\n"
     "🙈 <b>Hide</b>: not for me (↩️ Undo is available)\n\n"
     "The same role posted in several cities is sent only once."),
    ("🧭 The menu buttons",
     "🔍 <b>Find jobs now</b>: search right away (otherwise automatic every few minutes)\n"
     "⚙️ <b>Filters</b>: everything you can tune\n"
     "📄 <b>My resume</b>: what I read from it, suggested keywords\n"
     "📊 <b>Status</b>: last/next scan, how many sent, applied, queued\n"
     "⭐ <b>Saved</b>: your saved & applied jobs\n"
     "💡 <b>Insights</b>: skills jobs want that you don't have yet (what to learn next)\n"
     "⏯ <b>Pause / Resume</b>: stop or restart alerts\n"
     "🧹 <b>Clear chat</b>: tidy this chat (optionally keep job alerts)\n"
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
    score = row["score"]
    if score is None:
        lines.append("🎯 Match: <i>send your resume to see a match %</i>")
    else:
        bar = "🟩" * round(score / 20) + "⬜" * (5 - round(score / 20))
        lines.append(f"🎯 <b>Match {score}%</b> {bar}")
        if m.get("matched"):
            lines.append(f"✅ You have: {e(', '.join(m['matched'][:8]))}")
        if m.get("missing"):
            lines.append(f"📚 They also want: {e(', '.join(m['missing'][:6]))}")
    if d.get("keyword"):
        lines.append(f"🔎 <i>via “{e(d['keyword'])}”</i>")
    if state:
        lines.append(f"\n{state}")
    return "\n".join(lines)


def job_kb(row, state: str = "") -> InlineKeyboardMarkup:
    jid = row["job_id"]
    rows = [[B("🔗 Open on LinkedIn", url=row["url"])]]
    if not state:
        rows.append([B("✅ Applied", callback_data=f"j:applied:{jid}"), B("⭐ Save", callback_data=f"j:saved:{jid}"),
                     B("🙈 Hide", callback_data=f"j:hidden:{jid}")])
    else:
        rows.append([B("↩️ Undo", callback_data=f"j:sent:{jid}")])
    return InlineKeyboardMarkup(rows)


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
        [B("🌙 Quiet hours", callback_data="f:quiet")],
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
    "loc": "📍 Send <b>locations</b>, separated by commas (max 3).\n"
           "e.g. <code>India</code> or <code>Bengaluru, Noida, Remote</code>",
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
