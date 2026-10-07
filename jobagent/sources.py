"""Fetchers for public applicant-tracking-system job board APIs.

These are the official public endpoints companies publish for their careers pages,
so they return clean JSON and don't require scraping.
"""

import asyncio
import html
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from .proxy import client

COMPANIES_FILE = Path(__file__).resolve().parent.parent / "companies.toml"

SPONSOR_YES = re.compile(
    r"\b(we|can|will|able to|happy to|do)\s+(can\s+)?(offer\s+|provide\s+)?(visa\s+)?sponsor(ship)?\s*(for\s+)?(your\s+)?(work\s+)?visas?"
    r"|visa sponsorship (is |may be |can be )?(available|provided|offered|possible)"
    r"|sponsorship (is )?(available|provided|offered)|h-?1b (sponsorship|transfer)"
    r"|(support|help)( you)? with (your )?(visa|work permit)|relocation and visa",
    re.I,
)
SPONSOR_NO = re.compile(
    r"(not|unable to|cannot|can't|won't|will not|do not|does not)\s+(currently\s+)?(provide\s+|offer\s+)?(visa\s+)?sponsor"
    r"|(visa\s+)?sponsorship (is |will )?(not|n't) (be )?(available|provided|offered|possible)"
    r"|no (visa )?sponsorship"
    r"|without (the need for )?(current or future )?(visa )?sponsorship"
    r"|must be (legally )?authori[sz]ed to work .{0,40}without",
    re.I,
)


@dataclass
class Job:
    id: str
    source: str
    company: str
    title: str
    location: str
    url: str
    description: str
    sponsorship: str  # "yes" | "no" | "unknown"
    country: str = ""


def strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def sponsorship_signal(text: str) -> str:
    if SPONSOR_NO.search(text):
        return "no"
    if SPONSOR_YES.search(text):
        return "yes"
    return "unknown"


def _job(source, company, jid, title, location, url, raw_desc, iso3=None) -> Job:
    from .countries import country_of
    desc = strip_html(html.unescape(raw_desc or ""))
    return Job(f"{source}:{company}:{jid}", source, company, title, location or "", url, desc,
               sponsorship_signal(desc), country_of(location or "", iso3))


# Public board APIs: (url, params, parser) per applicant-tracking system.
BOARDS = {
    "greenhouse": (
        "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", {"content": "true"},
        lambda slug, d: [_job("greenhouse", slug, j["id"], j["title"], (j.get("location") or {}).get("name"),
                              j["absolute_url"], j.get("content")) for j in d.get("jobs", [])]),
    "lever": (
        "https://api.lever.co/v0/postings/{slug}", {"mode": "json"},
        lambda slug, d: [_job("lever", slug, j["id"], j["text"], (j.get("categories") or {}).get("location"),
                              j["hostedUrl"], j.get("descriptionPlain") or j.get("description")) for j in d]),
    "ashby": (
        "https://api.ashbyhq.com/posting-api/job-board/{slug}", {},
        lambda slug, d: [_job("ashby", slug, j["id"], j["title"], j.get("location"), j["jobUrl"],
                              j.get("descriptionPlain") or j.get("descriptionHtml")) for j in d.get("jobs", [])]),
}


def board_request(src: str, slug: str) -> tuple[str, dict]:
    url, params, _ = BOARDS[src]
    return url.format(slug=slug), params


def parse_board(src: str, slug: str, data) -> list[Job]:
    return BOARDS[src][2](slug, data)


def _fetcher(src: str):
    async def fetch(http: httpx.AsyncClient, slug: str) -> list[Job]:
        url, params = board_request(src, slug)
        r = await http.get(url, params=params)
        r.raise_for_status()
        return parse_board(src, slug, r.json())
    return fetch


greenhouse, lever, ashby = _fetcher("greenhouse"), _fetcher("lever"), _fetcher("ashby")


BIGSITE = "https://www.amazon.jobs/en/search.json"


def bigsite_jobs(data: dict) -> list[Job]:
    return [
        _job("bigsite", j.get("company_name") or "bigsite", j["id_icims"], j["title"],
             j.get("normalized_location") or j.get("location"), "https://www.amazon.jobs" + j["job_path"],
             (j.get("description_short") or "") + " " + (j.get("basic_qualifications") or ""), j.get("country_code"))
        for j in data.get("jobs", [])
    ]


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def load_companies() -> dict[str, list[str]]:
    with open(COMPANIES_FILE, "rb") as f:
        return {k: v for k, v in tomllib.load(f).items() if k in FETCHERS}


async def fetch_all(companies: dict[str, list[str]] | None = None,
                    country: str | None = None) -> tuple[list[Job], list[str]]:
    """Fetch every configured board concurrently. Returns (jobs, errors)."""
    companies = companies or load_companies()
    targets = [(src, c) for src, slugs in companies.items() for c in slugs]
    sem = asyncio.Semaphore(5)

    async with client(country) as http:
        async def one(src, company):
            async with sem:
                return await FETCHERS[src](http, company)

        results = await asyncio.gather(*(one(s, c) for s, c in targets), return_exceptions=True)

    jobs, errors = [], []
    for (src, company), res in zip(targets, results):
        if isinstance(res, Exception):
            errors.append(f"{src}/{company}: {type(res).__name__}: {res}")
        else:
            jobs.extend(res)
    return jobs, errors


def matches(job: Job, keywords: list[str], location: str | None, sponsorship_only: bool) -> bool:
    title = job.title.lower()
    if keywords and not any(k.lower() in title for k in keywords):
        return False
    if location and location.lower() not in job.location.lower():
        return False
    if sponsorship_only and job.sponsorship == "no":
        return False
    return True


def summary(job: Job, desc_chars: int = 0) -> dict:
    d = asdict(job)
    d["description"] = job.description[:desc_chars] if desc_chars else ""
    return d
