"""Fake LinkedIn client and fake Telegram objects for tests (no network)."""
from __future__ import annotations

import itertools
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.linkedin import Job, RateLimited


class FakeLinkedIn:
    """listings: keyword -> list[(id, title, company)]; descriptions: id -> text."""
    instances: list["FakeLinkedIn"] = []

    def __init__(self, listings=None, descriptions=None, rate_limit_after=None, posted="5 minutes ago"):
        self.posted = posted
        self.listings = listings or {}
        self.descriptions = descriptions or {}
        self.rate_limit_after = rate_limit_after
        self.searches, self.detail_calls, self.closed = [], [], False
        FakeLinkedIn.instances.append(self)

    def _tick(self):
        if self.rate_limit_after is not None and len(self.searches) + len(self.detail_calls) >= self.rate_limit_after:
            raise RateLimited("HTTP 429")

    async def search(self, query, start=0):
        self._tick()
        self.searches.append((query.keyword, query.location, start))
        rows = self.listings.get(query.keyword, [])[start:start + 10]
        return [Job(id=i, title=t, company=c, location="India", posted=self.posted, search_keyword=query.keyword)
                for i, t, c in rows]

    async def details(self, job):
        self._tick()
        self.detail_calls.append(job.id)
        job.description = self.descriptions.get(job.id, "")
        job.applicants = "20 applicants"
        job.seniority, job.employment_type = "Entry level", "Full-time"
        return job

    async def close(self):
        self.closed = True


def factory(**kw):
    def make():
        return FakeLinkedIn(**kw)
    return make


class FakeBot:
    def __init__(self):
        self.sent: list[SimpleNamespace] = []
        self.deleted: list[tuple[int, list[int]]] = []
        self._ids = itertools.count(1000)
        self.fail_with: Exception | None = None

    async def send_message(self, chat_id, text, **kw):
        if self.fail_with:
            raise self.fail_with
        msg = SimpleNamespace(chat_id=chat_id, message_id=next(self._ids), text=text, kw=kw,
                              edit_text=AsyncMock(), delete=AsyncMock())
        self.sent.append(msg)
        return msg

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))
        return True


def make_ctx(bot=None, args=None):
    bot = bot or FakeBot()
    app = SimpleNamespace(bot=bot, bot_data={}, job_queue=SimpleNamespace(get_jobs_by_name=lambda name: []))
    return SimpleNamespace(bot=bot, user_data={}, args=args or [], application=app)


def make_update(uid=1, text=None, data=None, document=None, first_name="Asha", username="asha"):
    user = SimpleNamespace(id=uid, first_name=first_name, username=username)
    chat = SimpleNamespace(id=uid * 10, type="private")
    message = SimpleNamespace(text=text, document=document, message_id=1, chat_id=chat.id)
    cq = None
    if data is not None:
        cq = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(),
                             message=SimpleNamespace(chat_id=chat.id, message_id=555, delete=AsyncMock()))
    return SimpleNamespace(effective_user=user, effective_chat=chat, effective_message=message, message=message,
                           callback_query=cq)
