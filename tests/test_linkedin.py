import asyncio

import httpx
import pytest

from app.filters import Query
from app.linkedin import DETAIL_URL, Job, LinkedInClient, RateLimited, applicants_count, parse_detail, parse_search
from tests.conftest import FIXTURES

Q = Query("Backend Developer", "India", "r86400", ("2", "3"))


def test_parse_search_fixture():
    jobs = parse_search((FIXTURES / "list.html").read_text(), "backend")
    assert len(jobs) == 10
    for j in jobs:
        assert j.id.isdigit() and j.title and j.company and j.location
        assert j.url == f"https://www.linkedin.com/jobs/view/{j.id}/"
    assert jobs[0].posted_date.count("-") == 2


def test_parse_detail_fixture():
    j = parse_detail((FIXTURES / "detail.html").read_text(), Job(id="1", title="t", company="c", location="l"))
    assert len(j.description) > 200
    assert j.seniority and j.employment_type and "applicants" in j.applicants.lower()


def test_parse_empty():
    assert parse_search("", "x") == []


@pytest.mark.parametrize("text,n", [("Over 200 applicants", 200), ("35 applicants", 35),
                                    ("Be among the first 25 applicants", 25), ("1,234 applicants", 1234), ("", None)])
def test_applicants_count(text, n):
    assert applicants_count(text) == n


def run(coro):
    return asyncio.run(coro)


def client_with(handler):
    return LinkedInClient(transport=httpx.MockTransport(handler), backoff_s=0)


def test_search_sends_filters_and_parses():
    seen = {}

    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, text=(FIXTURES / "list.html").read_text())

    async def go():
        c = client_with(handler)
        jobs = await c.search(Q, start=10)
        await c.close()
        return jobs
    assert len(run(go())) == 10
    assert seen["keywords"] == "Backend Developer" and seen["f_TPR"] == "r86400" and seen["f_E"] == "2,3"
    assert seen["start"] == "10" and seen["sortBy"] == "DD"


def test_backs_off_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(429) if calls["n"] < 3 else httpx.Response(200, text="")

    async def go():
        c = client_with(handler)
        r = await c.search(Q)
        await c.close()
        return r
    assert run(go()) == [] and calls["n"] == 3


def test_gives_up_after_three_429s():
    async def go():
        c = client_with(lambda req: httpx.Response(429))
        try:
            await c.details(Job(id="9", title="", company="", location=""))
        finally:
            await c.close()
    with pytest.raises(RateLimited):
        run(go())


def test_400_past_last_page_is_empty():
    async def go():
        c = client_with(lambda req: httpx.Response(400))
        r = await c.search(Q, start=990)
        await c.close()
        return r
    assert run(go()) == []


def test_detail_url():
    assert DETAIL_URL.format(id="123").endswith("/jobPosting/123")
