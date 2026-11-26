"""Telegram handlers, background jobs (scan, deliver, heartbeat, prune) and app wiring."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, TelegramError, TimedOut
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler,
                          TypeHandler, filters)

from . import db, referral, storage, ui
from .config import settings
from .filters import (MAX_KEYWORDS, MAX_LOCATIONS, POSTED, SENIOR_WORDS, Filters, location_label,
                      normalize_location, parse_applicants, parse_list,
                      parse_quiet_hours, parse_threshold, parse_years)
from .resume import ResumeError, build_profile, extract_text, find_name
from .scanner import Scanner, in_cooldown, reevaluate_user, user_context

log = logging.getLogger(__name__)
e = html.escape
SCAN_JOB, DELIVER_JOB = "scan-cycle", "deliver"
HEARTBEAT = settings.data_dir / "heartbeat"


# =============== sending (every bot message is tracked so Clear chat can delete it) ===============

async def send(bot, chat_id: int, text: str, kind: str = "ui", **kw):
    kw.setdefault("parse_mode", ParseMode.HTML)
    kw.setdefault("disable_web_page_preview", True)
    msg = await bot.send_message(chat_id, text, **kw)
    db.track_message(chat_id, msg.message_id, kind)
    return msg


async def reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str, **kw):
    return await send(ctx.bot, update.effective_chat.id, text, **kw)


async def track_incoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    m = update.effective_message
    if m and update.effective_chat and update.effective_chat.type == "private" and not update.callback_query:
        db.track_message(update.effective_chat.id, m.message_id, "user")


# =============== access control ===============

def _is_admin_id(uid: int) -> bool:
    return uid in settings.admin_ids


def register(update: Update) -> tuple:
    """Create/refresh the user row. Returns (row, is_new)."""
    tg = update.effective_user
    existing = db.get_user(tg.id)
    row = db.upsert_user(tg.id, update.effective_chat.id, tg.first_name or "", tg.username or "")
    if existing is None:
        if not row["filters"]:
            db.update_user(tg.id, filters=Filters().to_json())
        first_admin = not settings.admin_ids and not db.admins()
        if _is_admin_id(tg.id) or first_admin:
            db.update_user(tg.id, status="active", is_admin=1)
        elif settings.access_mode == "open" and db.count_users("active") < settings.max_users:
            db.update_user(tg.id, status="active")
        row = db.get_user(tg.id)
    elif _is_admin_id(tg.id) and not row["is_admin"]:
        db.update_user(tg.id, status="active", is_admin=1)
        row = db.get_user(tg.id)
    return row, existing is None


def active_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.effective_chat or update.effective_chat.type != "private":
            return
        user, _ = register(update)
        if user["status"] != "active":
            msg = ("⏳ Your access request is waiting for the admin's approval." if user["status"] == "pending"
                   else "🚫 Access to this bot was removed.")
            if update.callback_query:
                await update.callback_query.answer(msg, show_alert=True)
            else:
                await reply(update, ctx, msg)
            return
        return await fn(update, ctx, user)
    return wrapper


def admin_only(fn):
    @wraps(fn)
    @active_only
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user):
        if not user["is_admin"]:
            return await reply(update, ctx, "Admins only.")
        return await fn(update, ctx, user)
    return wrapper


def load(user_id: int) -> tuple[Filters, object]:
    return user_context(db.get_user(user_id))


def save_filters(user_id: int, f: Filters) -> int:
    db.update_user(user_id, filters=f.to_json())
    return reevaluate_user(user_id)


# =============== /start, help, approvals ===============

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        return
    user, is_new = register(update)
    name = update.effective_user.first_name
    if user["status"] == "blocked":
        await reply(update, ctx, "🚫 Access to this bot was removed.")
        return
    if user["status"] == "pending":
        await reply(update, ctx, f"👋 Hi {e(name or '')}! Your access request was sent to the admin. "
                                 "You'll get a message here as soon as you're approved.")
        if is_new:
            await notify_admins_new_user(ctx, user)
        return
    await reply(update, ctx, ui.onboarding(name) + ("\n\n👑 You are the admin of this bot." if user["is_admin"]
                                                    and is_new else ""), reply_markup=ui.MAIN_KB)
    if not json.loads(user["resume"] or "null"):
        await reply(update, ctx, "📎 Send your resume as a file now (PDF, DOCX or TXT).", reply_markup=ui.TOUR_KB)


async def notify_admins_new_user(ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    who = f"{e(user['first_name'] or '')} (@{e(user['username'])})" if user["username"] else e(user["first_name"] or "")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Approve", callback_data=f"a:approve:{user['id']}"),
                                InlineKeyboardButton("🚫 Block", callback_data=f"a:block:{user['id']}")]])
    for a in db.admins():
        try:
            await send(ctx.bot, a["chat_id"], f"🆕 Access request from {who}\nID: <code>{user['id']}</code>",
                       reply_markup=kb)
        except TelegramError as err:
            log.warning("admin notify failed: %s", err)


async def set_access(ctx: ContextTypes.DEFAULT_TYPE, target: int, status: str) -> str:
    u = db.get_user(target)
    if not u:
        return f"No user {target}."
    if u["is_admin"] and status == "blocked":
        return "Can't block an admin."
    db.update_user(target, status=status)
    try:
        if status == "active":
            await send(ctx.bot, u["chat_id"], "🎉 You're approved! " + ui.onboarding(u["first_name"]),
                       reply_markup=ui.MAIN_KB)
            await send(ctx.bot, u["chat_id"], "📎 Send your resume as a file now (PDF, DOCX or TXT).",
                       reply_markup=ui.TOUR_KB)
    except TelegramError:
        pass
    return f"{'✅ Approved' if status == 'active' else '🚫 Blocked'} {e(u['first_name'] or '')} ({target})."


@admin_only
async def on_admin_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    _, action, target = q.data.split(":")
    msg = await set_access(ctx, int(target), "active" if action == "approve" else "blocked")
    await q.answer()
    await q.edit_message_text(msg, parse_mode=ParseMode.HTML)


@admin_only
async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        return await reply(update, ctx, "Usage: /approve <user id>")
    await reply(update, ctx, await set_access(ctx, int(ctx.args[0]), "active"))


@admin_only
async def cmd_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    if not ctx.args or not ctx.args[0].isdigit():
        return await reply(update, ctx, "Usage: /block <user id>")
    await reply(update, ctx, await set_access(ctx, int(ctx.args[0]), "blocked"))


@admin_only
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    s = db.global_stats()
    lines = [f"<b>👥 Users</b> — {s['users']} active, {s['pending_users']} waiting · {s['jobs']} jobs stored · "
             f"{s['sent_today']} alerts today"]
    icon = {"active": "🟢", "pending": "⏳", "blocked": "🚫"}
    for u in db.users():
        f, p = user_context(u)
        lines.append(f"{icon.get(u['status'], '?')} <code>{u['id']}</code> {e(u['first_name'] or '')}"
                     + (f" @{e(u['username'])}" if u["username"] else "") + (" 👑" if u["is_admin"] else "")
                     + (" ⏸" if u["paused"] else "") + f" · {len(f.keywords)} kw · resume {'✅' if p else '—'}")
    await reply(update, ctx, "\n".join(lines))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, ctx, ui.HELP, reply_markup=ui.TOUR_KB)


async def cmd_intro(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Guided tour. Open to everyone (even users still waiting for approval)."""
    await reply(update, ctx, ui.intro_text(0), reply_markup=ui.intro_kb(0))


async def on_intro_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    arg = q.data.split(":", 1)[1]
    await q.answer()
    if arg == "done":
        user = db.get_user(update.effective_user.id)
        has_resume = bool(user and json.loads(user["resume"] or "null"))
        text = ("✅ You're all set — tap <b>⚙️ Filters</b> to fine-tune, or <b>🔍 Find jobs now</b>." if has_resume
                else "📎 <b>Send your resume as a file</b> (PDF, DOCX or TXT) to start getting matched jobs.")
        return await q.edit_message_text(text, parse_mode=ParseMode.HTML)
    page = int(arg) if arg.isdigit() else 0
    try:
        await q.edit_message_text(ui.intro_text(page), parse_mode=ParseMode.HTML, reply_markup=ui.intro_kb(page))
    except BadRequest as err:   # tapped the page counter: "message is not modified"
        if "not modified" not in str(err):
            raise


# =============== resume ===============

@active_only
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    doc = update.message.document
    name = doc.file_name or "resume"
    if doc.file_size and doc.file_size > settings.max_resume_mb * 1024 * 1024:
        return await reply(update, ctx, f"That file is too big (max {settings.max_resume_mb:g} MB).")
    status = await reply(update, ctx, "📄 Reading your resume…")
    try:
        data = bytes(await (await doc.get_file()).download_as_bytearray())
        text = extract_text(data, name)
    except ResumeError as err:
        return await status.edit_text(f"⚠️ {e(str(err))}")
    except TelegramError as err:
        return await status.edit_text(f"⚠️ Couldn't download the file: {e(str(err))}")
    await store_resume(update, ctx, user["id"], name, data, text, status)


async def store_resume(update, ctx, uid: int, name: str, data: bytes, text: str, status_msg) -> None:
    profile = build_profile(text, name)
    storage.save_resume(uid, name, data, text, profile.to_dict())
    f, _ = load(uid)
    first_keywords = not f.keywords
    if first_keywords:
        f.keywords = profile.suggested_keywords[:MAX_KEYWORDS]
    if profile.years is not None and profile.years >= 4 and f.exclude_words == SENIOR_WORDS:
        f.exclude_words = ["principal", "director", "vp", "head"]   # experienced: allow senior roles
    db.update_user(uid, resume=json.dumps(profile.to_dict()), filters=f.to_json())
    reevaluate_user(uid)
    await status_msg.delete()
    db.forget_messages(update.effective_chat.id, [status_msg.message_id])
    await reply(update, ctx, ui.resume_text(profile, f)
                + ("\n\n✅ I set your keywords from your resume. Change them any time." if first_keywords else ""),
                reply_markup=ui.resume_kb(True))


@active_only
async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    f, p = load(user["id"])
    if not p:
        return await reply(update, ctx, "📎 No resume yet. Send it as a file (PDF, DOCX or TXT).")
    await reply(update, ctx, ui.resume_text(p, f), reply_markup=ui.resume_kb(True))


@active_only
async def on_resume_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    f, p = load(user["id"])
    if q.data == "r:usekw":
        if not p:
            return await q.answer("Send your resume first.", show_alert=True)
        f.keywords = p.suggested_keywords[:MAX_KEYWORDS]
        save_filters(user["id"], f)
        await q.answer("Keywords updated ✅")
        await q.edit_message_text(ui.resume_text(p, f), parse_mode=ParseMode.HTML, reply_markup=ui.resume_kb(True))
    elif q.data == "r:delete":
        storage.delete_user(user["id"])
        db.update_user(user["id"], resume=None)
        reevaluate_user(user["id"])
        await q.answer("Resume deleted")
        await q.edit_message_text("🗑 Resume deleted. Jobs will still come for your keywords, without a match %.")


# =============== filters ===============

@active_only
async def cmd_filters(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    f, p = load(user["id"])
    await reply(update, ctx, ui.filters_text(f, p.years if p else None), reply_markup=ui.filters_kb(f))


async def _edit(q, text: str, kb) -> None:
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as err:   # "message is not modified"
        if "not modified" not in str(err):
            raise


def merge_keywords(f: Filters, new: list[str]) -> list[str]:
    """Append new titles (case-insensitive dedupe, up to MAX_KEYWORDS). Returns the ones added."""
    added = []
    for k in new:
        if len(f.keywords) >= MAX_KEYWORDS:
            break
        if k.lower() not in {x.lower() for x in f.keywords}:
            f.keywords.append(k)
            added.append(k)
    return added


async def _show_menu(q, f: Filters, p) -> None:
    try:
        await q.edit_message_text(ui.filters_text(f, p.years if p else None), parse_mode=ParseMode.HTML,
                                  reply_markup=ui.filters_kb(f))
    except BadRequest as err:   # "message is not modified"
        if "not modified" not in str(err):
            raise


@active_only
async def on_filter_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    uid = user["id"]
    f, p = load(uid)
    parts = q.data.split(":")
    key, arg = parts[1], (parts[2] if len(parts) > 2 else None)
    ctx.user_data.pop("awaiting", None)

    if key == "menu":
        await q.answer()
        return await _show_menu(q, f, p)
    if key in ("kw", "kwrm", "kwsug"):
        note = ""
        if key == "kwrm" and arg is not None and arg.isdigit() and int(arg) < len(f.keywords):
            removed = f.keywords.pop(int(arg))
            save_filters(uid, f)
            note = f"Removed {removed}"
        elif key == "kwsug" and p:
            added = merge_keywords(f, p.suggested_keywords)
            save_filters(uid, f)
            note = f"Added {len(added)}" + (" (list is full)" if len(f.keywords) >= MAX_KEYWORDS else "")
        await q.answer(note)
        return await _edit(q, ui.keywords_text(f, p), ui.keywords_kb(f, p))
    if key == "kwset":
        key = "kw"
    if key in ui.MULTI:
        attr, table, title = ui.MULTI[key]
        sel = list(getattr(f, attr))
        if arg == "any":
            sel = []
        elif arg in table:
            sel = [c for c in sel if c != arg] if arg in sel else sel + [arg]
        if arg is not None:
            setattr(f, attr, sel)
            save_filters(uid, f)
        await q.answer()
        return await q.edit_message_text(f"<b>{title}</b>\nTap to toggle. None selected = any.",
                                         parse_mode=ParseMode.HTML, reply_markup=ui.multi_kb(key, table, sel))
    if key == "posted":
        if arg in POSTED:
            f.posted = arg
            save_filters(uid, f)
            await q.answer(f"Date posted: {POSTED[arg]}")
            return await _show_menu(q, f, p)
        await q.answer()
        return await q.edit_message_text("<b>🕒 Date posted</b>", parse_mode=ParseMode.HTML,
                                         reply_markup=ui.posted_kb(f.posted))
    if key == "thr":
        if arg == "custom":
            ctx.user_data["awaiting"] = "thr"
            await q.answer()
            return await q.edit_message_text(ui.PROMPTS["thr"], parse_mode=ParseMode.HTML,
                                             reply_markup=ui.CANCEL_KB)
        if arg is not None and arg.isdigit():
            f.min_score = int(arg)
            n = save_filters(uid, f)
            await q.answer(f"Threshold {f.min_score}% · {n} jobs waiting to send")
            return await _show_menu(q, f, p)
        await q.answer()
        return await q.edit_message_text("<b>🎯 Match threshold</b>\nOnly jobs with match ≥ this are sent.",
                                         parse_mode=ParseMode.HTML, reply_markup=ui.threshold_kb(f.min_score))
    if key == "maxy":
        if arg is not None:
            f.max_years = None if arg == "auto" else (-1 if arg == "any" else float(arg))
            save_filters(uid, f)
            await q.answer("Saved")
            return await _show_menu(q, f, p)
        await q.answer()
        return await q.edit_message_text(
            "<b>🧮 Max years a job may ask for</b>\nAuto = your experience + 2.", parse_mode=ParseMode.HTML,
            reply_markup=ui.maxyears_kb(f.max_years))
    if key == "ea":
        f.easy_apply = not f.easy_apply
        save_filters(uid, f)
        await q.answer(f"Easy Apply only: {'ON' if f.easy_apply else 'off'}")
        return await _show_menu(q, f, p)
    if key == "reset":
        keep = f.keywords
        f = Filters(keywords=keep)
        save_filters(uid, f)
        await q.answer("Filters reset (keywords kept)")
        return await _show_menu(q, f, p)
    if key == "done":
        await q.answer("Saved ✅")
        n = len(db.deliveries(uid, ("pending",)))
        return await q.edit_message_text(
            "✅ <b>Filters saved.</b>\n\n" + e(ui.summary(f, p.years if p else None)) +
            (f"\n\n📬 {n} matching jobs are on their way." if n else
             "\n\nNew searches run in the next cycle (every few minutes)."),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔍 Find jobs now", callback_data="s:scan")]]))
    if key in ui.PROMPTS:
        ctx.user_data["awaiting"] = key
        await q.answer()
        return await q.edit_message_text(ui.PROMPTS[key], parse_mode=ParseMode.HTML, reply_markup=ui.CANCEL_KB)
    await q.answer()


def apply_text_setting(f: Filters, key: str, text: str) -> str:
    """Update f from what the user typed. Returns a confirmation. Raises ValueError with a friendly message."""
    t = text.strip()
    if key == "kw":
        kws = parse_list(t, MAX_KEYWORDS)
        if not kws:
            raise ValueError("Send at least one job title.")
        f.keywords = kws
        return f"🔎 Job titles: {', '.join(kws)}"
    if key == "kwadd":
        new = parse_list(t, 20)
        if not new:
            raise ValueError("Send at least one job title, e.g. ML Engineer")
        if len(f.keywords) >= MAX_KEYWORDS:
            raise ValueError(f"You already have {MAX_KEYWORDS} titles (the max). Remove one first with /titles.")
        added = merge_keywords(f, new)
        skipped = [k for k in new if k not in added]
        msg = f"➕ Added: {', '.join(added) or 'nothing new'}"
        return msg + (f" · skipped (already there or list full): {', '.join(skipped)}" if skipped else "")
    if key == "loc":
        locs = list(dict.fromkeys(normalize_location(x) for x in parse_list(t, MAX_LOCATIONS)))
        if not locs:
            raise ValueError("Send at least one location.")
        f.locations = locs
        return f"📍 Locations: {', '.join(location_label(x) for x in locs)}"
    if key == "excl":
        f.exclude_words = [] if t.lower() == "none" else [w.lower() for w in parse_list(t, 30, 30)]
        return f"🚫 Skipping titles with: {', '.join(f.exclude_words) or 'nothing'}"
    if key == "comp":
        f.exclude_companies = [] if t.lower() == "none" else parse_list(t, 50, 60)
        return f"🏢 Skipping companies: {', '.join(f.exclude_companies) or 'none'}"
    if key == "apps":
        f.max_applicants = parse_applicants(t)
        return f"👥 Max applicants: {f.max_applicants or 'any'}"
    if key == "quiet":
        f.quiet_start, f.quiet_end = parse_quiet_hours(t)
        return "🌙 Quiet hours off" if f.quiet_start is None else f"🌙 Quiet {f.quiet_start}:00–{f.quiet_end}:00"
    if key == "myy":
        f.my_years = None if t.lower() == "auto" else parse_years(t)
        return f"👤 Experience: {'from resume' if f.my_years is None else f'{f.my_years:g} yrs'}"
    if key == "thr":
        f.min_score = parse_threshold(t)
        return f"🎯 Threshold: {f.min_score}%"
    raise ValueError("Unknown setting")


@active_only
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    key = ctx.user_data.get("awaiting")
    if not key:
        return await reply(update, ctx, "Use the menu below 👇, or send me your resume as a file.",
                           reply_markup=ui.MAIN_KB)
    f, p = load(user["id"])
    try:
        msg = apply_text_setting(f, key, update.message.text)
    except ValueError as err:
        return await reply(update, ctx, f"⚠️ {e(str(err))}", reply_markup=ui.CANCEL_KB)
    ctx.user_data.pop("awaiting", None)
    save_filters(user["id"], f)
    if key in ("kw", "kwadd"):
        return await reply(update, ctx, f"✅ {e(msg)}\n\n" + ui.keywords_text(f, p), reply_markup=ui.keywords_kb(f, p))
    await reply(update, ctx, f"✅ {e(msg)}\n\n" + ui.filters_text(f, p.years if p else None),
                reply_markup=ui.filters_kb(f))


def _quick_setting(key: str, usage: str):
    @active_only
    async def handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
        text = " ".join(ctx.args or [])
        if not text:
            ctx.user_data["awaiting"] = key
            return await reply(update, ctx, ui.PROMPTS[key], reply_markup=ui.CANCEL_KB)
        f, _ = load(user["id"])
        try:
            msg = apply_text_setting(f, key, text)
        except ValueError as err:
            return await reply(update, ctx, f"⚠️ {e(str(err))}\nUsage: {usage}")
        n = save_filters(user["id"], f)
        await reply(update, ctx, f"✅ {e(msg)}" + (f"\n📬 {n} matching jobs waiting to send." if n else ""))
    return handler


# =============== jobs: buttons, saved, insights, status ===============

@active_only
async def on_job_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    _, status, job_id = q.data.split(":", 2)
    if status not in ui.JOB_STATES or not db.set_status(user["id"], job_id, status):
        return await q.answer()
    row = db.get_delivery(user["id"], job_id)
    await q.answer({"applied": "Marked applied ✅", "saved": "Saved ⭐", "hidden": "Hidden",
                    "sent": "Undone"}[status])
    state = ui.JOB_STATES[status]
    await q.edit_message_text(ui.job_text(row, state), parse_mode=ParseMode.HTML, disable_web_page_preview=True,
                              reply_markup=ui.job_kb(row, state))


@active_only
async def on_referral_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    job_id = q.data.split(":", 1)[1]
    row = db.get_delivery(user["id"], job_id)
    if not row:
        return await q.answer("That job is no longer available.", show_alert=True)
    await q.answer("Writing your referral messages…")
    f, p = load(user["id"])
    name = (p.name if p and p.name else "") or _name_from_saved_resume(user["id"]) or (user["first_name"] or "")
    matched = json.loads(row["details"] or "{}").get("match", {}).get("matched", [])
    ref = referral.build(row["title"], row["company"], job_id, row["url"], matched, p, name,
                         f.years(p.years if p else None) if (p or f.my_years is not None) else None,
                         fallback_role=f.keywords[0] if f.keywords else "")
    await reply(update, ctx, ui.referral_text(ref, row["title"], row["company"]),
                reply_markup=ui.referral_kb(ref, referral.clean_company(row["company"])))


def _name_from_saved_resume(user_id: int) -> str:
    """Resumes uploaded before names were parsed: read it from the saved text."""
    path = storage.user_dir(user_id) / "resume.txt.extracted"
    try:
        return find_name(path.read_text()[:2000]) if path.exists() else ""
    except OSError:
        return ""


@active_only
async def cmd_saved(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    out = []
    for status, title in (("saved", "⭐ Saved"), ("applied", "✅ Applied")):
        rows = db.deliveries(user["id"], (status,), limit=25)
        if rows:
            out.append(f"<b>{title}</b>")
            out += [f"• <a href=\"{e(r['url'])}\">{e(r['title'])}</a> — {e(r['company'])}"
                    + (f" ({r['score']}%)" if r["score"] is not None else "") for r in rows]
    await reply(update, ctx, "\n".join(out) or "Nothing saved yet. Tap ⭐ Save or ✅ Applied on a job.")


@active_only
async def cmd_insights(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    rows = db.deliveries(user["id"], ("pending",) + db.SHOWN, since_hours=24 * 30)
    await reply(update, ctx, ui.insights_text(rows))


@active_only
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    f, p = load(user["id"])
    s = db.user_stats(user["id"])
    nxt = next(iter(ctx.application.job_queue.get_jobs_by_name(SCAN_JOB)), None)
    cooldown = in_cooldown()
    lines = [
        f"<b>📊 Status</b> — {'⏸ paused' if user['paused'] else '▶️ active'}",
        f"🔁 Scans every {settings.scan_every_minutes:g} min"
        + (f" · next at {nxt.next_t.astimezone(ZoneInfo(settings.timezone)):%H:%M}" if nxt and nxt.next_t else ""),
        f"🕘 Last scan: {e(db.get_meta('last_cycle_at', 'never'))} — {e(db.get_meta('last_cycle', '-'))}",
    ]
    if cooldown:
        lines.append(f"🐢 LinkedIn rate limit: waiting until {cooldown} UTC")
    lines += [
        "",
        f"📬 Sent today: {s.get('sent_today', 0)} · total {sum(s.get(k, 0) for k in db.SHOWN)}",
        f"✅ Applied {s.get('applied', 0)} · ⭐ Saved {s.get('saved', 0)} · 🙈 Hidden {s.get('hidden', 0)}",
        f"⏳ Queued {s.get('pending', 0)} · 🔄 Being checked {s.get('waiting', 0)} · "
        f"🚫 Filtered out {s.get('rejected', 0)}",
        f"📄 Resume: {'✅ ' + e(p.file_name) if p else '— (send it as a file)'}",
        f"🔎 {len(f.keywords)} keywords × {len(f.locations)} locations",
    ]
    await reply(update, ctx, "\n".join(lines))


@active_only
async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    paused = 0 if user["paused"] else 1
    db.update_user(user["id"], paused=paused)
    await reply(update, ctx, "⏸ Alerts paused. Tap ⏯ again to resume." if paused else
                "▶️ Alerts resumed — new jobs will arrive within a few minutes.")


@active_only
async def cmd_deleteme(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    if ctx.args and ctx.args[0].lower() == "confirm":
        await clear_chat(ctx.bot, update.effective_chat.id, keep_jobs=False)
        storage.delete_user(user["id"])
        db.delete_user(user["id"])
        return await ctx.bot.send_message(update.effective_chat.id,
                                          "🗑 All your data (resume, filters, job history) is deleted. "
                                          "Send /start to begin again.")
    await reply(update, ctx, "⚠️ This deletes your resume, filters and job history.\n"
                             "Send <code>/deleteme confirm</code> to continue.")


# =============== clear chat ===============

async def clear_chat(bot, chat_id: int, keep_jobs: bool) -> int:
    ids = db.tracked_messages(chat_id, keep_kinds=("job",) if keep_jobs else ())
    deleted = 0
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await bot.delete_messages(chat_id, chunk)
            deleted += len(chunk)
        except TelegramError as err:
            log.info("delete_messages: %s", err)
        db.forget_messages(chat_id, chunk)
    return deleted


@active_only
async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    await reply(update, ctx, "🧹 <b>Clear this chat?</b>\nTelegram lets bots delete messages from the last 48 h.",
                reply_markup=ui.CLEAR_KB)


@active_only
async def on_clear_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    q = update.callback_query
    await q.answer()
    if q.data == "c:cancel":
        db.forget_messages(q.message.chat_id, [q.message.message_id])
        return await q.message.delete()
    n = await clear_chat(ctx.bot, q.message.chat_id, keep_jobs=q.data == "c:keepjobs")
    await send(ctx.bot, q.message.chat_id, f"✨ Chat cleared ({n} messages).", reply_markup=ui.MAIN_KB)


# =============== scanning & delivery ===============

def _lock(app: Application) -> asyncio.Lock:
    return app.bot_data.setdefault("scan_lock", asyncio.Lock())


async def run_cycle(app: Application, only_user: int | None = None):
    async with _lock(app):
        res = await Scanner().run(only_user=only_user)
        if only_user is None:
            db.set_meta("last_cycle_at", datetime.now(ZoneInfo(settings.timezone)).strftime("%d %b %H:%M"))
            db.set_meta("last_cycle", res.summary())
    await deliver_all(app)
    return res


async def scheduled_cycle(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _lock(ctx.application).locked():
        return
    try:
        await run_cycle(ctx.application)
    except Exception:
        log.exception("scan cycle crashed")


@active_only
async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    if update.callback_query:
        await update.callback_query.answer()
    f, _ = load(user["id"])
    if not f.keywords:
        return await reply(update, ctx, "Set some keywords first: send your resume or tap ⚙️ Filters → 🔎 Keywords.")
    last = user["last_manual_scan"]
    if last and (db.now() - datetime.fromisoformat(last + "+00:00")).total_seconds() < settings.manual_scan_cooldown_s:
        return await reply(update, ctx, "⏱ You just scanned. Automatic scans run every "
                                        f"{settings.scan_every_minutes:g} min, so new jobs will still come.")
    if _lock(ctx.application).locked():
        return await reply(update, ctx, "🔄 A scan is already running — new jobs will arrive in a moment.")
    db.update_user(user["id"], last_manual_scan=db.iso())
    msg = await reply(update, ctx, f"🔍 Searching LinkedIn for {len(f.keywords) * len(f.locations)} searches… "
                                   "(requests are spaced out to avoid being blocked, ~1–3 min)")
    res = await run_cycle(ctx.application, only_user=user["id"])
    s = db.user_stats(user["id"])
    await msg.edit_text(f"✅ Done: {e(res.summary())}\n⏳ {s.get('pending', 0)} still queued", parse_mode=ParseMode.HTML)


def _send_lock(app: Application) -> asyncio.Lock:
    return app.bot_data.setdefault("send_lock", asyncio.Lock())


async def deliver_all(app: Application) -> int:
    """Send pending jobs. Runs every minute, even while a scan is still going, so matches arrive as soon as
    they're scored. Its own lock stops two deliveries sending the same job twice."""
    async with _send_lock(app):
        return await _deliver(app)


async def _deliver(app: Application) -> int:
    total = 0
    hour = datetime.now(ZoneInfo(settings.timezone)).hour
    for u in db.users("active"):
        if u["paused"]:
            continue
        f, _ = user_context(u)
        if f.in_quiet_hours(hour):
            continue
        for row in db.deliveries(u["id"], ("pending",), limit=settings.max_per_cycle):
            try:
                msg = await send(app.bot, u["chat_id"], ui.job_text(row), kind="job", reply_markup=ui.job_kb(row))
            except Forbidden:
                log.info("user %s blocked the bot: pausing", u["id"])
                db.update_user(u["id"], paused=1)
                break
            except TelegramError as err:
                log.warning("send to %s failed: %s", u["id"], err)
                break
            db.mark_sent(u["id"], row["job_id"], msg.message_id)
            total += 1
            await asyncio.sleep(0.4)
    return total


async def deliver_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _send_lock(ctx.application).locked():
        await deliver_all(ctx.application)


async def heartbeat(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.touch()


async def prune_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    db.prune()


def clean_old_logs(logs_dir=None, hours: float | None = None) -> list[str]:
    """Delete rotated log files older than LOG_RETENTION_HOURS (the live bot.log is rotated hourly)."""
    logs_dir = logs_dir or settings.data_dir / "logs"
    hours = settings.log_retention_hours if hours is None else hours
    if not logs_dir.exists():
        return []
    cutoff = time.time() - hours * 3600
    removed = []
    for f in logs_dir.iterdir():
        if f.is_file() and f.name != "bot.log" and f.stat().st_mtime < cutoff:
            f.unlink()
            removed.append(f.name)
    return removed


async def log_cleanup_job(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    removed = clean_old_logs()
    if removed:
        log.info("removed %d old log files", len(removed))


async def ack_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()   # stale button from an old message


@active_only
async def cmd_titles(update: Update, ctx: ContextTypes.DEFAULT_TYPE, user) -> None:
    f, p = load(user["id"])
    await reply(update, ctx, ui.keywords_text(f, p), reply_markup=ui.keywords_kb(f, p))


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, (NetworkError, TimedOut)):   # internet blip: one line, no traceback
        log.warning("network problem talking to Telegram: %s", ctx.error)
        return
    log.error("handler error", exc_info=ctx.error)


# =============== wiring ===============

COMMANDS = [
    ("scan", "🔍 Search LinkedIn now"), ("filters", "⚙️ All filters"), ("resume", "📄 Your resume"),
    ("titles", "🔎 Add / remove job titles"), ("addtitle", "➕ Add job titles"), ("location", "📍 Set locations"), ("threshold", "🎯 Min match %"),
    ("years", "👤 Your years of experience"), ("saved", "⭐ Saved & applied jobs"),
    ("insights", "💡 Skills you're missing"), ("status", "📊 Stats"), ("pause", "⏯ Pause / resume alerts"),
    ("clear", "🧹 Clear chat"), ("intro", "📖 Guided tour of all features"), ("help", "❓ Commands"),
    ("deleteme", "🗑 Delete all my data"),
]


async def post_init(app: Application) -> None:
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in COMMANDS])
        await app.bot.set_my_short_description("New LinkedIn jobs matched to your resume, the moment they're posted.")
        await app.bot.set_my_description(
            "🎯 Job Hunter watches LinkedIn for new jobs and sends the ones that fit your resume, with a match %.\n\n"
            "Send your resume, set keywords, location, experience and a match threshold — that's it.")
    except TelegramError as err:
        log.warning("couldn't set bot profile: %s", err)
    jq = app.job_queue
    jq.run_repeating(scheduled_cycle, interval=timedelta(minutes=settings.scan_every_minutes), first=15,
                     name=SCAN_JOB, job_kwargs={"misfire_grace_time": 60, "coalesce": True, "max_instances": 1})
    jq.run_repeating(deliver_job, interval=60, first=30, name=DELIVER_JOB)
    jq.run_repeating(heartbeat, interval=30, first=0, name="heartbeat")
    jq.run_repeating(prune_job, interval=timedelta(hours=6), first=600, name="prune")
    jq.run_repeating(log_cleanup_job, interval=timedelta(hours=1), first=60, name="log-cleanup")
    log.info("Job Hunter started: scanning every %s min", settings.scan_every_minutes)


def build() -> Application:
    app = Application.builder().token(settings.bot_token).post_init(post_init).concurrent_updates(True).build()
    private = filters.ChatType.PRIVATE
    app.add_handler(TypeHandler(Update, track_incoming), group=-1)
    app.add_handler(CommandHandler("start", cmd_start, filters=private))
    app.add_handler(CommandHandler("help", cmd_help, filters=private))
    app.add_handler(CommandHandler(["intro", "tour", "guide"], cmd_intro, filters=private))
    app.add_handler(CommandHandler("scan", cmd_scan, filters=private, block=False))
    app.add_handler(CommandHandler("filters", cmd_filters, filters=private))
    app.add_handler(CommandHandler(["resume", "cv"], cmd_resume, filters=private))
    app.add_handler(CommandHandler("keywords", _quick_setting("kw", "/keywords AI Engineer, GenAI Engineer"),
                                   filters=private))
    app.add_handler(CommandHandler(["titles", "keywords_menu"], cmd_titles, filters=private))
    app.add_handler(CommandHandler(["addtitle", "addtitles", "add"],
                                   _quick_setting("kwadd", "/addtitle ML Engineer, Python Developer"), filters=private))
    app.add_handler(CommandHandler("location", _quick_setting("loc", "/location India, Remote"), filters=private))
    app.add_handler(CommandHandler("threshold", _quick_setting("thr", "/threshold 20"), filters=private))
    app.add_handler(CommandHandler("years", _quick_setting("myy", "/years 2.5"), filters=private))
    app.add_handler(CommandHandler(["saved", "applied"], cmd_saved, filters=private))
    app.add_handler(CommandHandler("insights", cmd_insights, filters=private))
    app.add_handler(CommandHandler("status", cmd_status, filters=private))
    app.add_handler(CommandHandler("pause", cmd_pause, filters=private))
    app.add_handler(CommandHandler("clear", cmd_clear, filters=private))
    app.add_handler(CommandHandler("deleteme", cmd_deleteme, filters=private))
    app.add_handler(CommandHandler("users", cmd_users, filters=private))
    app.add_handler(CommandHandler("approve", cmd_approve, filters=private))
    app.add_handler(CommandHandler("block", cmd_block, filters=private))

    buttons = {ui.BTN_SCAN: cmd_scan, ui.BTN_FILTERS: cmd_filters, ui.BTN_RESUME: cmd_resume,
               ui.BTN_STATUS: cmd_status, ui.BTN_SAVED: cmd_saved, ui.BTN_INSIGHTS: cmd_insights,
               ui.BTN_PAUSE: cmd_pause, ui.BTN_CLEAR: cmd_clear, ui.BTN_HELP: cmd_help}
    for label, fn in buttons.items():
        app.add_handler(MessageHandler(private & filters.Text([label]), fn, block=label != ui.BTN_SCAN))

    app.add_handler(MessageHandler(private & filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_filter_button, pattern=r"^f:"))
    app.add_handler(CallbackQueryHandler(on_job_button, pattern=r"^j:"))
    app.add_handler(CallbackQueryHandler(on_referral_button, pattern=r"^ref:"))
    app.add_handler(CallbackQueryHandler(on_resume_button, pattern=r"^r:"))
    app.add_handler(CallbackQueryHandler(on_clear_button, pattern=r"^c:"))
    app.add_handler(CallbackQueryHandler(on_admin_button, pattern=r"^a:"))
    app.add_handler(CallbackQueryHandler(on_intro_button, pattern=r"^i:"))
    app.add_handler(CallbackQueryHandler(cmd_scan, pattern=r"^s:scan$", block=False))
    app.add_handler(CallbackQueryHandler(ack_unknown))
    app.add_error_handler(on_error)
    return app
