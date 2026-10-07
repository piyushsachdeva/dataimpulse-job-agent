"""See the same page as a local in several countries, and show what changes."""

import asyncio
import difflib
import re

from .proxy import client
from .sources import strip_html

IP_URL = "http://ip-api.com/json/?fields=query,country,city"
CURRENCIES = {"£": "GBP", "€": "EUR", "₹": "INR", "$": "USD/other $", "CHF": "CHF", "zł": "PLN"}


async def exit_ip(country: str | None = None, sticky: bool = False) -> dict:
    async with client(country, timeout=20, sticky=sticky) as http:
        r = await http.get(IP_URL)
    return r.json() | {"elapsed": round(r.elapsed.total_seconds(), 2)}


async def view(url: str, country: str) -> dict:
    try:
        async with client(country) as http:
            r = await http.get(url)
    except Exception as e:  # show failures in the comparison rather than aborting it
        return {"country": country, "error": f"{type(e).__name__}: {e}"}
    body = r.text
    title = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    text = strip_html(body) if "html" in r.headers.get("content-type", "") else body
    return {
        "country": country,
        "status": r.status_code,
        "final_url": str(r.url),
        "title": strip_html(title.group(1))[:80] if title else "",
        "chars": len(text),
        "currencies": sorted({v for k, v in CURRENCIES.items() if k in text}),
        "text": text,
    }


async def compare(url: str, countries: list[str]) -> list[dict]:
    views = await asyncio.gather(*(view(url, c) for c in countries))
    base = next((v for v in views if "text" in v), None)
    for v in views:
        if "text" in v and base:
            v["similarity"] = round(difflib.SequenceMatcher(None, base["text"][:20000], v["text"][:20000]).ratio(), 3)
            only = set(re.split(r"(?<=[.!?])\s+", v["text"])) - set(re.split(r"(?<=[.!?])\s+", base["text"]))
            v["unique_sample"] = [s[:120] for s in list(only)[:3]]
    return views
