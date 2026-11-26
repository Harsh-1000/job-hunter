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
/keywords AI Engineer, GenAI Engineer – set keywords
/location India, Remote – set locations
/threshold 20 – only send jobs with match ≥ 20%
/years 2.5 – set your experience
/resume – your resume · /saved – saved & applied
/insights – skills you're missing most
/status – stats · /pause – pause or resume alerts
/clear – clear this chat · /deleteme – delete all your data"""


def onboarding(name: str) -> str:
    return (f"👋 Hi {e(name or 'there')}! I'm <b>Job Hunter</b>.\n\n"
            "Send me your <b>resume</b> (PDF, DOCX or TXT) and I'll find LinkedIn jobs that match it, "
            "as soon as they're posted, with a match %.\n\nTap ❓ Help any time.")


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
        [B("🔎 Keywords", callback_data="f:kw"), B("📍 Location", callback_data="f:loc")],
        [B("🕒 Date posted", callback_data="f:posted"), B("🎓 Experience level", callback_data="f:exp")],
        [B("📄 Job type", callback_data="f:jt"), B("🏠 On-site/Remote", callback_data="f:wt")],
        [B("🎯 Match threshold", callback_data="f:thr"), B("🧮 Max years asked", callback_data="f:maxy")],
        [B("👤 My experience", callback_data="f:myy"), B("👥 Max applicants", callback_data="f:apps")],
        [B("🚫 Skip title words", callback_data="f:excl"), B("🏢 Skip companies", callback_data="f:comp")],
        [B(f"⚡ Easy Apply only: {'ON' if f.easy_apply else 'off'}", callback_data="f:ea"),
         B("🌙 Quiet hours", callback_data="f:quiet")],
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


CANCEL_KB = InlineKeyboardMarkup([[B("✖️ Cancel", callback_data="f:menu")]])

PROMPTS = {
    "kw": "🔎 Send your <b>search keywords</b>, separated by commas (max 8).\n"
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
    rows = [[B("✅ Use suggested keywords", callback_data="r:usekw"), B("✏️ Edit keywords", callback_data="f:kw")],
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
