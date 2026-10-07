"""MCP server: DataImpulse residential web access + job search tools for AI agents.

Every outbound request goes through the DataImpulse proxy (see proxy.py).
The AI client itself (and its own traffic) is not proxied.
"""

import asyncio
import functools
import inspect
import json
import logging
import sys
import time

from mcp.server.mcpserver import MCPServer

from . import fetcher, geo, sheet, sources, store, sweep
from .proxy import client, meter

logging.getLogger("httpx").setLevel(logging.WARNING)

INSTRUCTIONS = """\
DataImpulse residential web access.

Use `proxy_fetch` instead of your built-in web fetch when:
- a normal fetch fails with 403, 429, 503, "Server Busy", or times out;
- you need many requests to the same site (pagination, monitoring, bulk lookups);
- the page should be seen as a local visitor in a specific country (pass `country`, ISO-2).
Use `sticky=true` for multi-step browsing that should keep one IP (up to the session
duration set in the DataImpulse dashboard).

Do not use it for pages behind a login, to get past CAPTCHAs, or on sites whose terms
forbid automated access. Every call uses paid traffic ($1/GB); `usage_receipt` shows the total.

Job search: `search_jobs`, `get_job`, `track_job`, `list_tracked` (results also go to
the spreadsheet jobs.xlsx). See AGENTS.md in the project for the job-search workflow.
"""

mcp = MCPServer("dataimpulse", instructions=INSTRUCTIONS, log_level="WARNING")


def _log(msg: str):
    print(f"\033[2m{time.strftime('%H:%M:%S')}\033[0m  {msg}", file=sys.stderr, flush=True)


def tool(fn):
    """Register an MCP tool and log each call (name, arguments, time, result size) to stderr."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        shown = ", ".join(f"{k}={v!r}" for k, v in kwargs.items() if v not in (None, False))
        _log(f"\033[36m▶ {fn.__name__}\033[0m({shown[:110]})")
        t0 = time.time()
        try:
            out = fn(*args, **kwargs)
            if inspect.isawaitable(out):
                out = await out
        except Exception as e:
            _log(f"\033[31m✖ {fn.__name__} failed: {type(e).__name__}: {e}\033[0m")
            raise
        _log(f"\033[32m◀ {fn.__name__}\033[0m · {time.time() - t0:.1f}s · {len(str(out)) / 1000:.1f} KB result")
        return out
    return mcp.tool()(wrapper)


@tool
async def search_jobs(
    keywords: list[str] | None = None,
    location: str | None = None,
    sponsorship_only: bool = False,
    new_only: bool = False,
    country: str | None = None,
    limit: int = 50,
) -> str:
    """Search the job boards of every company in companies.toml.

    keywords: match any of these in the job title (case-insensitive), e.g. ["backend", "platform engineer"].
    location: substring match on the location, e.g. "London" or "Remote".
    sponsorship_only: drop jobs whose description says they will NOT sponsor visas.
    new_only: only return jobs not returned by a previous search.
    country: proxy exit country (ISO-2) for this search, overriding DATAIMPULSE_COUNTRY.
    """
    jobs, errors = await sources.fetch_all(country=country)
    fresh = store.mark_seen(jobs)
    hits = [
        j for j in jobs
        if sources.matches(j, keywords or [], location, sponsorship_only) and (not new_only or j.id in fresh)
    ]
    store.mark_matched(j.id for j in hits)
    sheet_status = sheet.export()
    return json.dumps({
        "total_scanned": len(jobs),
        "spreadsheet": sheet_status,
        "matched": len(hits),
        "returned": min(len(hits), limit),
        "jobs": [sources.summary(j) | {"new": j.id in fresh} for j in hits[:limit]],
        "errors": errors,
        "receipt": meter.receipt(),
    }, indent=1)


@tool
async def get_job(job_id: str) -> str:
    """Full description of one job, by the id from sweep_jobs / list_tracked / search_jobs.

    Reads just that posting through DataImpulse (not the whole board). Use this when
    proxy_fetch on a job URL is blocked or returns almost no text.
    """
    src, company, jid = job_id.split(":", 2)
    async with client(timeout=30) as http:
        if src == "greenhouse":
            r = await http.get(f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{jid}")
            r.raise_for_status()
            jobs = sources.parse_board("greenhouse", company, {"jobs": [r.json()]})
        elif src == "lever":
            r = await http.get(f"https://api.lever.co/v0/postings/{company}/{jid}")
            r.raise_for_status()
            jobs = sources.parse_board("lever", company, [r.json()])
        elif src == "ashby":
            jobs = [j for j in await sources.FETCHERS["ashby"](http, company) if j.id == job_id]
        else:  # big careers site: read the posting page itself
            row = next((r for r in store.list_jobs() if r["id"] == job_id), None)
            if not row:
                return f"Job {job_id} not found in the tracker."
            res = await fetcher.fetch(row["url"], max_chars=15000)
            return fetcher.as_text(res)
    if not jobs:
        return f"Job {job_id} not found (it may have been closed)."
    return json.dumps(sources.summary(jobs[0], desc_chars=15000), indent=1)


@tool
async def proxy_fetch(url: str, country: str | None = None, sticky: bool = False,
                      max_chars: int = 30000, raw: bool = False) -> str:
    """Fetch a URL through a DataImpulse residential IP and return its readable text.

    Use when your normal web fetch is blocked or rate-limited (403/429/503/timeouts), when
    you need many requests to one site, or to see a page as a local in `country` (ISO-2,
    e.g. "gb"). Refused requests are retried on a fresh IP with backoff. sticky=true keeps
    one IP across calls. raw=true returns HTML instead of extracted text.
    First line of the result is JSON metadata (status, attempts, final_url, truncated).
    """
    return fetcher.as_text(await fetcher.fetch(url, country, sticky, max_chars, raw))


@tool
async def fetch_page(url: str, country: str | None = None, max_chars: int = 20000) -> str:
    """Alias of proxy_fetch kept for the job-search workflow."""
    return fetcher.as_text(await fetcher.fetch(url, country, max_chars=max_chars))


@tool
async def proxy_status(country: str | None = None) -> str:
    """Check that the DataImpulse proxy works and show the exit IP and location."""
    async with client(country, timeout=20) as http:
        r = await http.get("http://ip-api.com/json/?fields=query,country,city,isp")
    return r.text


@tool
async def compare_countries(url: str, countries: list[str] | None = None) -> str:
    """Fetch the same page as a local visitor in several countries (ISO-2 codes) and report
    what differs: status, redirects, title, currencies shown, similarity to the first country."""
    views = await geo.compare(url, countries or ["in", "gb", "de", "us"])
    for v in views:
        v.pop("text", None)
    return json.dumps({"views": views, "receipt": meter.receipt()}, indent=1)


@tool
def usage_receipt() -> str:
    """Proxy traffic used so far in this session and its estimated cost at $1/GB."""
    return meter.receipt()


@tool
def track_job(job_id: str, status: str | None = None, score: int | None = None, notes: str | None = None) -> str:
    """Record a decision about a job. status: shortlisted | applied | rejected | skip. score: 0-100 fit."""
    if not store.update(job_id, status, score, notes):
        return f"unknown job id {job_id}"
    return "updated; " + sheet.export()


@tool
def list_tracked(status: str | None = None, country: str | None = None, title: str | None = None,
                 unscored_only: bool = False, limit: int = 50, max_per_company: int = 2) -> str:
    """Jobs in the tracker (matching jobs from the latest sweeps), newest first.

    Filter by status ("new", "shortlisted", "applied", "skip"), country ("Canada", "Germany"…),
    a title substring ("backend"), or unscored_only=true for jobs you haven't assessed yet.
    max_per_company (default 2) keeps one big employer from filling the list; 0 = no cap.
    """
    rows, per = [], {}
    for r in store.list_jobs(status, country, title, matched_only=True, unscored_only=unscored_only):
        # the big careers site lists jobs under many legal-entity names: count it as one employer
        key = "bigsite" if r["id"].startswith("bigsite:") else (r.get("company") or "").lower()
        if max_per_company and per.get(key, 0) >= max_per_company:
            continue
        per[key] = per.get(key, 0) + 1
        rows.append(r)
        if len(rows) >= limit:
            break
    keep = ("id", "company", "title", "location", "country", "sponsorship", "status", "score", "notes", "url")
    return json.dumps([{k: r.get(k) for k in keep} for r in rows], indent=1)


@tool
async def sweep_jobs(keywords: list[str] | None = None, pages: int = 1000, country: str | None = None) -> str:
    """Sweep job listings worldwide through DataImpulse residential IPs (takes 2–4 minutes).

    Scans every employer board in companies.toml plus `pages` pages (10 listings each) of a
    large careers site, using a new household IP per request so the site doesn't refuse it.
    All jobs go into the tracker and jobs.xlsx. Returns a summary: pages retrieved/refused,
    failure reasons, jobs collected, new jobs, matches by country, log file, traffic cost.
    Then use list_tracked(unscored_only=true, ...) to review the new matches.
    """
    return json.dumps(await sweep.run(keywords or ["engineer"], pages=pages, country=country), indent=1)


def main():
    """Start the server.

    jobsearch-mcp            stdio: Claude Code starts and stops it for you
    jobsearch-mcp --http     you start it; Claude connects to http://127.0.0.1:8765/mcp
    """
    import argparse
    import os

    ap = argparse.ArgumentParser(prog="jobsearch-mcp")
    ap.add_argument("--http", action="store_true", help="run as a local HTTP server you start yourself")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()

    if not args.http:
        mcp.run()
        return

    from mcp.server.transport_security import TransportSecuritySettings

    url = f"http://127.0.0.1:{args.port}/mcp"
    gateway = f"{os.getenv('DATAIMPULSE_HOST', 'gw.dataimpulse.com')}:{os.getenv('DATAIMPULSE_PORT', '823')}"
    print(f"""
\033[1mDataImpulse MCP server is running\033[0m
  URL        {url}
  proxy      {gateway} (residential, rotating)
  tools      {len(asyncio.run(mcp.list_tools()))} (sweep_jobs, list_tracked, get_job, track_job, proxy_fetch, …)

  Connect Claude Code (once, in another terminal):
    claude mcp add -s user --transport http dataimpulse {url}

  Every tool call Claude makes appears below. Ctrl+C to stop.
""", file=sys.stderr, flush=True)
    mcp.run(
        "streamable-http",
        port=args.port,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(
            allowed_hosts=[f"127.0.0.1:{args.port}", f"localhost:{args.port}"]),
    )


if __name__ == "__main__":
    main()
