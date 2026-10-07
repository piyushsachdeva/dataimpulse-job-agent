"""General-purpose fetch through DataImpulse residential IPs, with polite pacing.

Retries a refused request (403/429/503/timeout) on a fresh residential IP with backoff,
honours Retry-After, and limits concurrency per site. It does not solve CAPTCHAs, spoof
browser fingerprints, or log in anywhere.
"""

import asyncio
import collections
import json
import time
from urllib.parse import urlparse

import httpx

from .proxy import client
from .sources import strip_html

RETRY_CODES = {403, 429, 503}
PER_HOST = collections.defaultdict(lambda: asyncio.Semaphore(4))
LAST_HIT: dict[str, float] = {}
MIN_GAP = 0.25  # seconds between requests to the same host


async def _polite(host: str):
    wait = LAST_HIT.get(host, 0) + MIN_GAP - time.monotonic()
    if wait > 0:
        await asyncio.sleep(wait)
    LAST_HIT[host] = time.monotonic()


async def fetch(url: str, country: str | None = None, sticky: bool = False,
                max_chars: int = 30000, raw: bool = False, attempts: int = 3) -> dict:
    host = urlparse(url).netloc
    history = []
    async with PER_HOST[host]:
        for attempt in range(1, attempts + 1):
            await _polite(host)
            try:
                async with client(country, timeout=30, sticky=sticky, new_ip_per_request=not sticky) as http:
                    r = await http.get(url)
                history.append(r.status_code)
            except httpx.ProxyError as e:
                history.append(f"ProxyError: {e}")
                r = None
                if "BLOCKED" in str(e).upper():  # provider blocklist: retrying cannot help
                    return {"ok": False, "attempts": history,
                            "error": f"DataImpulse does not allow this site ({e}). It is on the provider's "
                                     f"blocklist (see 'Blocked Websites' in the dashboard). For job postings "
                                     f"use get_job(job_id) instead."}
            except httpx.HTTPError as e:
                history.append(type(e).__name__)
                r = None
            if r is not None and r.status_code not in RETRY_CODES:
                break
            if attempt < attempts:
                retry_after = r.headers.get("retry-after", "") if r is not None else ""
                delay = min(float(retry_after), 10) if retry_after.isdigit() else 2 ** (attempt - 1)
                await asyncio.sleep(delay)

    if r is None:
        return {"ok": False, "attempts": history, "error": "no response after retries"}
    ctype = r.headers.get("content-type", "")
    if raw or "json" in ctype:
        body = r.text
    elif "html" in ctype:
        body = strip_html(r.text)
    else:
        body = r.text
    return {
        "ok": r.status_code < 400,
        "status": r.status_code,
        "final_url": str(r.url),
        "attempts": history,
        "content_type": ctype,
        "chars": len(body),
        "text": body[:max_chars],
        "truncated": len(body) > max_chars,
        **({"hint": "very little text: the page is probably built by JavaScript. For job postings use "
                    "get_job(job_id)."} if len(body) < 300 and r.status_code < 400 else {}),
    }


def as_text(result: dict) -> str:
    head = {k: v for k, v in result.items() if k != "text"}
    return json.dumps(head) + "\n\n" + result.get("text", "")
