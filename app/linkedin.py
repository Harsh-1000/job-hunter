"""LinkedIn job search through the public (logged-out) jobs pages.

These are the same endpoints linkedin.com/jobs uses for visitors who are not signed in, so no
account or cookie is needed and nothing is tied to a profile. Requests are spaced out and the
scan stops on HTTP 429 (rate limited) instead of retrying hard.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .filters import Query

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL_URL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
VIEW_URL = "https://www.linkedin.com/jobs/view/{id}/"
PAGE_SIZE = 10

USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
]


class RateLimited(Exception):
    """LinkedIn answered 429 / 999: stop this scan and try again next time."""


@dataclass
class Job:
    id: str
    title: str
    company: str
    location: str
    posted: str = ""            # e.g. "3 hours ago"
    posted_date: str = ""       # ISO date from the card
    url: str = ""
    description: str = ""
    seniority: str = ""
    employment_type: str = ""
    job_function: str = ""
    industries: str = ""
    applicants: str = ""
    search_keyword: str = ""
    easy_apply: bool | None = None   # True: LinkedIn Easy Apply · False: applies on the company site
    criteria: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.url:
            self.url = VIEW_URL.format(id=self.id)


def _text(node) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node else ""


AGE_UNITS = {"second": 1 / 60, "minute": 1, "hour": 60, "day": 1440, "week": 10080, "month": 43200, "year": 525600}


def posted_age_minutes(text: str) -> float | None:
    """'9 minutes ago' -> 9, 'Reposted 2 hours ago' -> 120, 'Just now' -> 0; None if unknown."""
    t = (text or "").lower()
    if "just now" in t or "moments ago" in t:
        return 0.0
    m = re.search(r"(\d+)\s*(second|minute|hour|day|week|month|year)s?\s+ago", t)
    if not m:
        return None
    return int(m.group(1)) * AGE_UNITS[m.group(2)]


def applicants_count(text: str) -> int | None:
    """'Over 200 applicants' -> 200, 'Be among the first 25 applicants' -> 25, '35 applicants' -> 35."""
    m = re.search(r"(\d[\d,]*)\s*(?:\+\s*)?applicants?", text or "", re.I)
    return int(m.group(1).replace(",", "")) if m else None


def detect_easy_apply(html: str) -> bool | None:
    """The public job page tags its apply button: '...apply-link-simple' = Easy Apply, '...-offsite' = company site."""
    if "public_jobs_apply-link-offsite" in html or "offsite-apply" in html:
        return False
    if "public_jobs_apply-link-simple" in html:
        return True
    return None


def parse_search(html: str, keyword: str = "") -> list[Job]:
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[Job] = []
    for card in soup.select("div.base-card[data-entity-urn], div.job-search-card"):
        urn = card.get("data-entity-urn", "")
        m = re.search(r"(\d+)$", urn)
        if not m:
            link = card.select_one("a.base-card__full-link")
            m = re.search(r"-(\d+)\?", link.get("href", "")) if link else None
        if not m:
            continue
        t = card.select_one("time")
        jobs.append(Job(
            id=m.group(1),
            title=_text(card.select_one(".base-search-card__title")),
            company=_text(card.select_one(".base-search-card__subtitle")),
            location=_text(card.select_one(".job-search-card__location")),
            posted=_text(t),
            posted_date=t.get("datetime", "") if t else "",
            search_keyword=keyword,
        ))
    return jobs


def parse_detail(html: str, job: Job) -> Job:
    soup = BeautifulSoup(html, "html.parser")
    desc = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    if desc:
        for br in desc.find_all(["br", "li", "p"]):
            br.insert_before("\n")
        job.description = re.sub(r"\n\s*\n+", "\n", desc.get_text(" ")).strip()
    for item in soup.select(".description__job-criteria-item"):
        k = _text(item.select_one(".description__job-criteria-subheader"))
        v = _text(item.select_one(".description__job-criteria-text"))
        if k:
            job.criteria[k] = v
    job.seniority = job.criteria.get("Seniority level", "")
    job.employment_type = job.criteria.get("Employment type", "")
    job.job_function = job.criteria.get("Job function", "")
    job.industries = job.criteria.get("Industries", "")
    job.applicants = _text(soup.select_one(".num-applicants__caption"))
    job.easy_apply = detect_easy_apply(html)
    if not job.posted:
        job.posted = _text(soup.select_one(".posted-time-ago__text"))
    return job


class LinkedInClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, backoff_s: float = 30) -> None:
        self.backoff_s = backoff_s
        self.requests = 0
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=20,
            follow_redirects=True,
            headers={"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,*/*"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None) -> str:
        for attempt in range(3):
            await asyncio.sleep(random.uniform(settings.delay_min, settings.delay_max))
            self.requests += 1
            r = await self._client.get(url, params=params, headers={"User-Agent": random.choice(USER_AGENTS)})
            if r.status_code not in (429, 999):
                break
            if attempt == 2:
                raise RateLimited(f"HTTP {r.status_code}")
            wait = self.backoff_s * (attempt + 1) + random.uniform(0, self.backoff_s / 2)
            log.info("LinkedIn %s, backing off %.0fs", r.status_code, wait)
            await asyncio.sleep(wait)
        if r.status_code == 400 and "start" in (params or {}):
            return ""  # past the last page
        r.raise_for_status()
        return r.text

    async def search(self, query: Query, start: int = 0) -> list[Job]:
        html = await self._get(SEARCH_URL, query.params(start))
        return parse_search(html, query.keyword) if html.strip() else []

    async def details(self, job: Job) -> Job:
        html = await self._get(DETAIL_URL.format(id=job.id))
        return parse_detail(html, job)
