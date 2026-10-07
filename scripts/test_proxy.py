"""End-to-end proxy check: run with `uv run python scripts/test_proxy.py`.

1. Direct vs proxied exit IP (proves traffic leaves through DataImpulse)
2. Rotation: several requests on the rotating port should show different IPs
3. Country targeting: exit IP location for a few countries
4. Real workload: job board search + fetch_page, both through the proxy
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from jobagent import server
import os

from jobagent.proxy import client, meter, proxy_url

IP_URL = "http://ip-api.com/json/?fields=query,country,city,isp"


def ok(label, detail=""):
    print(f"  PASS  {label} {detail}")


def fail(label, detail=""):
    print(f"  FAIL  {label} {detail}")
    return 1


async def main() -> int:
    failures = 0
    try:
        masked = proxy_url().split("@")[1]
    except RuntimeError as e:
        print(f"Cannot test: {e}")
        return 2
    print(f"Proxy gateway: {masked}\n")

    print("1. Direct vs proxied IP")
    async with httpx.AsyncClient(timeout=15) as http:
        direct = (await http.get(IP_URL)).json()
    async with client() as http:
        proxied = (await http.get(IP_URL)).json()
    print(f"     direct : {direct}\n     proxied: {proxied}")
    failures += 0 if proxied["query"] != direct["query"] else fail("exit IP unchanged — proxy not in use")
    if proxied["query"] != direct["query"]:
        ok("traffic exits through a different IP")

    print("\n2. Rotating vs sticky sessions (5 requests each)")
    for label, sticky in [("rotating", False), ("sticky", True)]:
        ips = []
        for _ in range(5):
            async with client(sticky=sticky) as http:
                ips.append((await http.get(IP_URL)).json()["query"])
        print(f"     {label}: {ips}")
        good = len(set(ips)) > 1 if not sticky else len(set(ips)) == 1
        if good:
            ok(label, f"{len(set(ips))} distinct IP(s)")
        else:
            failures += fail(label, f"{len(set(ips))} distinct IP(s) — check the ports in .env against the dashboard")

    print("\n2b. SOCKS5")
    os.environ["DATAIMPULSE_PROTOCOL"] = "socks5"
    try:
        async with client() as http:
            info = (await http.get(IP_URL)).json()
        ok("socks5", f"-> {info.get('query')} {info.get('country')}")
    except Exception as e:
        failures += fail("socks5", f"{type(e).__name__}: {e}")
    finally:
        os.environ["DATAIMPULSE_PROTOCOL"] = "http"

    print("\n3. Country targeting")
    for cc in ["us", "gb", "de", "in"]:
        async with client(cc) as http:
            info = (await http.get(IP_URL)).json()
        match = {"us": "United States", "gb": "United Kingdom", "de": "Germany", "in": "India"}[cc]
        if info.get("country") == match:
            ok(cc, f"-> {info['country']}, {info.get('city')}")
        else:
            failures += fail(cc, f"-> {info}")

    print("\n4. Real workload through the proxy")
    r = json.loads(await server.search_jobs(keywords=["engineer"], limit=1))
    if r["total_scanned"] and not r["errors"]:
        ok("search_jobs", f"scanned {r['total_scanned']} jobs, {r['matched']} matched")
    else:
        failures += fail("search_jobs", str(r["errors"][:3]))
    page = await server.proxy_fetch("https://jobs.lever.co/spotify", country="gb", max_chars=300)
    meta = json.loads(page.split("\n", 1)[0])
    if meta.get("ok"):
        ok("proxy_fetch careers page (gb)", f"HTTP {meta['status']} attempts={meta['attempts']}")
    else:
        failures += fail("proxy_fetch careers page (gb)", str(meta))

    print(f"\nReceipt: {meter.receipt()}")
    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
