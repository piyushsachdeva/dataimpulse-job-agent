"""Global job sweep with a live before/after panel and a full request log.

    USE_PROXY=0 uv run python -m jobagent.sweep     # BEFORE: from your own connection
    uv run python -m jobagent.sweep                 # AFTER: through DataImpulse residential IPs
    ... --mask                                      # hide site hostnames (for recording)

Same work both times: every employer board in companies.toml plus N pages of a big careers
site. Every request is printed as it happens and saved to logs/. Nothing is simulated.
"""

import argparse
import asyncio
import collections
import os
import time
import tomllib
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import httpx
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from . import sheet, sources, store
from .proxy import USER_AGENT, client, meter

IP_URL = "http://ip-api.com/json/?fields=query,country,city,isp"
TIMEOUT = 25.0
BOARD_TIMEOUT = 20.0
REFUSAL_MARKERS = {"server busy": "'Server Busy' bot page instead of job data",
                   "captcha": "CAPTCHA page instead of job data",
                   "unusual traffic": "'unusual traffic' page instead of job data",
                   "are you a robot": "'are you a robot' page instead of job data"}
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
STYLE = {"OK": "green", "REFUSED": "bold red", "TIMEOUT": "bold yellow", "ERROR": "yellow", "SKIPPED": "yellow",
         "RETRY": "dim yellow"}
ICON = {"OK": "✅", "REFUSED": "⛔", "TIMEOUT": "⏱ ", "ERROR": "⚠️ ", "SKIPPED": "⏭ ", "RETRY": "↻ "}
NETWORK_RETRIES = 2  # network errors only (dropped connections, timeouts); refusals are never retried


def classify(r: httpx.Response) -> str | None:
    """Why a careers-site response is a refusal, or None if it carries job data."""
    if r.status_code >= 400:
        return f"HTTP {r.status_code} {r.reason_phrase}"
    head = r.text[:3000].lower()
    for marker, reason in REFUSAL_MARKERS.items():
        if marker in head:
            return reason
    ctype = r.headers.get("content-type", "").split(";")[0]
    if "json" not in ctype:
        return f"got {ctype or 'unknown content'} instead of job data (JSON)"
    return None


def describe_error(e: Exception, timeout: float) -> tuple[str, str]:
    if isinstance(e, httpx.ConnectTimeout):
        return "TIMEOUT", f"could not connect within {timeout:.0f}s (ConnectTimeout)"
    if isinstance(e, httpx.ReadTimeout):
        return "TIMEOUT", f"connected, but no data within {timeout:.0f}s (ReadTimeout)"
    if isinstance(e, (httpx.PoolTimeout, httpx.WriteTimeout, asyncio.TimeoutError)):
        return "TIMEOUT", f"gave up after {timeout:.0f}s ({type(e).__name__})"
    if isinstance(e, httpx.ProxyError):
        return "ERROR", f"proxy rejected the request: {e}"
    if isinstance(e, httpx.ConnectError):
        return "ERROR", f"connection failed: {str(e)[:80] or 'ConnectError'}"
    if isinstance(e, httpx.RemoteProtocolError):
        return "ERROR", "server closed the connection mid-response (RemoteProtocolError)"
    if isinstance(e, ValueError):
        return "ERROR", "response was not valid JSON"
    return "ERROR", f"{type(e).__name__}: {str(e)[:80]}"


class State:
    def __init__(self, proxied: bool, boards: int, pages: int, mask: bool):
        self.proxied, self.boards_total, self.pages_total, self.mask = proxied, boards, pages, mask
        self.boards_done = self.pages_ok = self.pages_refused = self.pages_error = 0
        self.timeouts = 0
        self.first_refusal: int | None = None
        self.jobs: dict[str, sources.Job] = {}
        self.where = "…"
        self.ips: set[str] = set()
        self.started = time.time()
        self.finished: float | None = None
        self.reasons: collections.Counter = collections.Counter()
        self.lines: list[tuple[str, Text]] = []  # (verdict, line) for the end-of-run excerpt
        self.console: Console | None = None
        LOG_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = LOG_DIR / f"sweep-{'after' if proxied else 'before'}-{stamp}.log"
        self.log = open(self.log_path, "w")

    @property
    def pages_done(self):
        return self.pages_ok + self.pages_refused + self.pages_error

    @property
    def completeness(self):
        done = self.boards_done + self.pages_done
        got = self.boards_done + self.pages_ok
        return got / done if done else 1.0

    def target(self, url: str, mask: bool | None = None) -> str:
        u = urlparse(url)
        host = u.netloc
        if (self.mask if mask is None else mask) and "amazon" in host:
            host = "careers-site"
        return f"{host}{u.path}" + (f"?{u.query}" if u.query else "")

    def event(self, n: str, method: str, url: str, verdict: str, seconds: float, response: str, detail: str):
        """Print one fully described request line and append it to the log file."""
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        via = "proxy" if self.proxied else "direct"
        plain = (f"{ts}  {n:<14} {via:<6} {method} {self.target(url, mask=False)}  →  {response:<34} "
                 f"{seconds:5.2f}s  {verdict:<8} {detail}")
        self.log.write(plain + "\n")
        line = Text(f"{ts} ", style="dim")
        line.append(f"{n:<14}", style="cyan")
        line.append(f"{via:<7}", style="magenta" if self.proxied else "blue")
        line.append(f"{method} {self.target(url)}", style="white")
        line.append(f"\n{'':13}→ {response}  ·  {seconds:.2f}s  ·  ", style="dim")
        line.append(f"{ICON[verdict]} {verdict}", style=STYLE[verdict])
        if detail:
            line.append(f"  {detail}", style=STYLE[verdict] if verdict != "OK" else "dim")
        self.lines.append((verdict, line))
        if self.console:
            self.console.print(line)

    def panel(self) -> Panel:
        color = "green" if self.proxied else ("red" if self.pages_refused else "yellow")
        title = ("AFTER · through DataImpulse residential proxies" if self.proxied
                 else "BEFORE · from my own internet connection")
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right", style="dim", width=22)
        t.add_column()
        t.add_row("connection", self.where)
        if self.proxied and self.ips:
            t.add_row("IPs used (sampled)", f"{len(self.ips)} different household IPs")
        t.add_row("employer boards", f"{self.boards_done} / {self.boards_total}")
        t.add_row("careers site pages", f"{self.pages_ok} retrieved / {self.pages_total}")
        t.add_row("⛔ refused", Text(f"{self.pages_refused}" + (f"  (first at page #{self.first_refusal})"
                                                                 if self.first_refusal else ""),
                                      style="bold red" if self.pages_refused else "green"))
        t.add_row("⏱  timeouts / errors", Text(f"{self.timeouts} / {self.pages_error - self.timeouts}",
                                               style="yellow" if self.pages_error else "green"))
        t.add_row("✅ jobs collected", Text(f"{len(self.jobs):,}", style="bold"))
        t.add_row("data retrieved", Text(f"{self.completeness * 100:.0f}%",
                                         style="bold green" if self.completeness > 0.97 else "bold red"))
        t.add_row("time", f"{(self.finished or time.time()) - self.started:.0f}s")
        bar = ProgressBar(total=self.pages_total, completed=self.pages_done, width=60,
                          complete_style="green" if not self.pages_refused else "red")
        return Panel(Group(t, Text(), bar), title=f"[bold]{title}", border_style=color, width=100)


def header(state: State, a, pages: int) -> Panel:
    t = Table.grid(padding=(0, 2))
    t.add_column(justify="right", style="dim", width=22)
    t.add_column()
    t.add_row("mode", "AFTER · DataImpulse residential proxies" if state.proxied else "BEFORE · no proxy")
    t.add_row("connection", state.where)
    if state.proxied:
        t.add_row("proxy gateway", f"{os.getenv('DATAIMPULSE_HOST', 'gw.dataimpulse.com')}:"
                                   f"{os.getenv('DATAIMPULSE_PORT', '823')} · "
                                   f"{os.getenv('DATAIMPULSE_PROTOCOL', 'http').upper()}")
        t.add_row("session", "rotating · new household IP for every careers-site request")
        t.add_row("exit country", a.country.upper() if a.country else "worldwide (any of 195 locations)")
    t.add_row("targets", f"{state.boards_total} employer boards + {pages:,} careers-site pages "
                         f"(10 jobs per page = {pages * 10:,} listings)")
    t.add_row("timeouts", f"{TIMEOUT:.0f}s per careers-site request · {BOARD_TIMEOUT:.0f}s per employer board")
    t.add_row("retries", f"up to {NETWORK_RETRIES} on network errors only · refusals are never retried")
    t.add_row("parallel requests", f"{a.conc} at a time")
    t.add_row("search keywords", " ".join(a.keywords))
    t.add_row("full log file", str(state.log_path.relative_to(LOG_DIR.parent)))
    return Panel(t, title="[bold]Sweep settings", border_style="cyan", width=100)


def country_table(state: State, keywords: list[str]) -> Table:
    hits = [j for j in state.jobs.values() if sources.matches(j, keywords, None, False)]
    by = collections.defaultdict(lambda: [0, 0])
    for j in hits:
        by[j.country][0] += 1
        by[j.country][1] += j.sponsorship == "yes"
    t = Table(title=f"'{' / '.join(keywords) or 'all'}' jobs found, by country", width=100)
    t.add_column("Country")
    t.add_column("Jobs", justify="right")
    t.add_column("Say they sponsor visas", justify="right")
    for c, (n, yes) in sorted(by.items(), key=lambda kv: -kv[1][0])[:14]:
        t.add_row(c, f"{n:,}", str(yes) if yes else "–")
    t.add_row("[bold]Total", f"[bold]{len(hits):,}", f"[bold]{sum(v[1] for v in by.values())}")
    return t


async def boards(state: State, companies: dict, country: str | None):
    http = client(country, timeout=BOARD_TIMEOUT) if state.proxied else \
        httpx.AsyncClient(timeout=BOARD_TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    sem = asyncio.Semaphore(6)
    names = [(s, c) for s, slugs in companies.items() for c in slugs]

    async def one(k, src, slug):
        async with sem:
            url, params = sources.board_request(src, slug)
            full = url + ("?" + urlencode(params) if params else "")
            t0 = time.time()
            try:
                r = await asyncio.wait_for(http.get(url, params=params), timeout=BOARD_TIMEOUT)
                resp = f"HTTP {r.status_code} {r.reason_phrase} · {len(r.content) / 1000:,.0f} KB"
                if r.status_code >= 400:
                    state.event(f"board {k}/{len(names)}", "GET", full, "ERROR", time.time() - t0, resp,
                                f"{slug}: board returned an error")
                else:
                    jobs = sources.parse_board(src, slug, r.json())
                    for j in jobs:
                        state.jobs[j.id] = j
                    state.event(f"board {k}/{len(names)}", "GET", full, "OK", time.time() - t0, resp,
                                f"{slug}: {len(jobs)} jobs")
            except Exception as e:
                verdict, why = describe_error(e, BOARD_TIMEOUT)
                state.event(f"board {k}/{len(names)}", "GET", full, "SKIPPED" if verdict == "TIMEOUT" else verdict,
                            time.time() - t0, "no response", f"{slug}: {why}")
            state.boards_done += 1

    async with http:
        await asyncio.gather(*(one(k, s, c) for k, (s, c) in enumerate(names, 1)))


async def bigsite(state: State, query: str, conc: int, country: str | None):
    if state.proxied:
        http = client(country, timeout=TIMEOUT, new_ip_per_request=True)
    else:
        http = httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True, headers={"User-Agent": USER_AGENT})
    sem = asyncio.Semaphore(conc)

    async def page(i):
        async with sem:
            params = {"base_query": query, "offset": i * 10, "result_limit": 10}
            url = f"{sources.BIGSITE}?{urlencode(params)}"
            n = f"page {i + 1}/{state.pages_total}"
            for attempt in range(NETWORK_RETRIES + 1):
                t0 = time.time()
                try:
                    r = await http.get(sources.BIGSITE, params=params)
                except Exception as e:
                    verdict, why = describe_error(e, TIMEOUT)
                    if attempt < NETWORK_RETRIES:
                        state.event(n, "GET", url, "RETRY", time.time() - t0, "no response",
                                    f"{why} → retrying (attempt {attempt + 2}/{NETWORK_RETRIES + 1})")
                        continue
                    state.pages_error += 1
                    state.timeouts += verdict == "TIMEOUT"
                    state.reasons[why] += 1
                    state.event(n, "GET", url, verdict, time.time() - t0, "no response",
                                f"{why} (gave up after {NETWORK_RETRIES + 1} attempts)")
                    return
                ctype = r.headers.get("content-type", "?").split(";")[0]
                resp = f"HTTP {r.status_code} {r.reason_phrase} · {ctype} · {len(r.content) / 1000:.1f} KB"
                reason = classify(r)
                if reason:
                    state.pages_refused += 1
                    state.first_refusal = state.first_refusal or i + 1
                    state.reasons[reason] += 1
                    state.event(n, "GET", url, "REFUSED", time.time() - t0, resp, reason)
                else:
                    try:
                        jobs = sources.bigsite_jobs(r.json())
                    except ValueError:
                        state.pages_error += 1
                        state.reasons["response was not valid JSON"] += 1
                        state.event(n, "GET", url, "ERROR", time.time() - t0, resp, "response was not valid JSON")
                        return
                    state.pages_ok += 1
                    for j in jobs:
                        state.jobs[j.id] = j
                    note = f" (after {attempt} retr{'y' if attempt == 1 else 'ies'})" if attempt else ""
                    state.event(n, "GET", url, "OK", time.time() - t0, resp, f"{len(jobs)} jobs{note}")
                return

    async with http:
        await asyncio.gather(*(page(i) for i in range(state.pages_total)))


async def sample_ips(state: State, country: str | None, stop: asyncio.Event):
    while not stop.is_set():
        try:
            async with client(country, timeout=10) as c:
                info = (await c.get(IP_URL)).json()
            state.ips.add(info["query"])
            state.where = f"{info['query']} · {info.get('city')}, {info.get('country')} (changes every request)"
        except Exception:
            pass
        await asyncio.sleep(1.5)


def load_config(pages: int | None) -> tuple[dict, dict, int]:
    with open(sources.COMPANIES_FILE, "rb") as f:
        cfg = tomllib.load(f)
    return cfg, sources.load_companies(), pages or cfg.get("bigsite", {}).get("pages", 1000)


async def collect(state: State, cfg: dict, companies: dict, conc: int, country: str | None):
    await asyncio.gather(boards(state, companies, country),
                         bigsite(state, cfg.get("bigsite", {}).get("query", ""), conc, country))
    state.finished = time.time()



def finish(state: State, keywords: list[str]) -> dict:
    """Close the log, store results in the tracker + spreadsheet, return a summary."""
    first = f" (first at page {state.first_refusal})" if state.first_refusal else ""
    state.log.write(f"\n# summary: {state.pages_ok} pages ok, {state.pages_refused} refused{first}, "
                    f"{state.timeouts} timeouts, "
                    f"{state.pages_error - state.timeouts} other errors, {len(state.jobs)} jobs\n")
    for reason, n in state.reasons.most_common():
        state.log.write(f"#   {n:4} × {reason}\n")
    state.log.close()
    hits = [j for j in state.jobs.values() if sources.matches(j, keywords, None, False)]
    fresh = store.mark_seen(list(state.jobs.values()))
    store.mark_matched(j.id for j in hits)
    by_country = collections.Counter(j.country for j in hits)
    return {
        "mode": "DataImpulse residential proxies" if state.proxied else "own connection",
        "careers_site_pages": {"total": state.pages_total, "retrieved": state.pages_ok,
                               "refused": state.pages_refused, "failed": state.pages_error,
                               "first_refusal_at_page": state.first_refusal},
        "employer_boards": state.boards_total,
        "data_retrieved_pct": round(state.completeness * 100),
        "jobs_collected": len(state.jobs),
        "jobs_matching_keywords": len(hits),
        "new_jobs_never_seen_before": len(fresh),
        "matching_jobs_by_country": dict(by_country.most_common(15)),
        "failure_reasons": dict(state.reasons.most_common()),
        "household_ips_sampled": len(state.ips),
        "seconds": round((state.finished or time.time()) - state.started),
        "spreadsheet": sheet.export(),
        "log_file": str(state.log_path.relative_to(LOG_DIR.parent)),
        "proxy_traffic": meter.receipt() if state.proxied else None,
    }


async def run(keywords: list[str], pages: int | None = None, conc: int = 20,
              country: str | None = None) -> dict:
    """Headless sweep through DataImpulse, used by the MCP tool. Logs go to logs/, nothing to stdout."""
    cfg, companies, pages = load_config(pages)
    state = State(True, sum(len(v) for v in companies.values()), pages, mask=False)
    state.where = "DataImpulse rotating residential IPs"
    state.log.write(f"# AFTER · DataImpulse residential proxies (run by the AI agent)\n# targets: "
                    f"{state.boards_total} boards + {pages} pages · keywords: {' '.join(keywords)}\n\n")
    stop = asyncio.Event()
    sampler = asyncio.create_task(sample_ips(state, country, stop))
    await collect(state, cfg, companies, conc, country)
    stop.set()
    await sampler
    return finish(state, keywords)


def excerpt(state: State) -> list[Text]:
    """A short, representative slice of the log for screenshots: first OKs, each kind of failure, last lines."""
    pick, count = [], collections.Counter()
    for verdict, line in state.lines:
        if count[verdict] < (2 if verdict == "OK" else 3):
            count[verdict] += 1
            pick.append(line)
    return pick + [line for _, line in state.lines[-2:]]


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", "--keywords", nargs="*", default=["engineer"])
    ap.add_argument("--pages", type=int, help="careers-site pages (default from companies.toml)")
    ap.add_argument("--conc", type=int, default=20)
    ap.add_argument("--country", help="proxy exit country, ISO-2 (default: worldwide)")
    ap.add_argument("--mask", action="store_true", help="hide the careers site's hostname (for recording)")
    ap.add_argument("--svg", help="save a screenshot (settings, log excerpt, results) as SVG")
    a = ap.parse_args()

    cfg, companies, pages = load_config(a.pages)
    proxied = os.getenv("USE_PROXY", "1") != "0"
    state = State(proxied, sum(len(v) for v in companies.values()), pages, a.mask)
    console = Console()

    if not proxied:
        async with httpx.AsyncClient(timeout=10) as c:
            me = (await c.get(IP_URL)).json()
        state.where = f"{me['query']} · {me.get('city')}, {me.get('country')} · {me.get('isp')} (one IP)"
    else:
        state.where = "DataImpulse rotating residential IPs (sampling…)"
    console.print(header(state, a, pages))
    state.log.write(f"# {'AFTER · DataImpulse residential proxies' if proxied else 'BEFORE · no proxy'}\n"
                    f"# connection: {state.where}\n"
                    f"# targets: {state.boards_total} employer boards + {pages} careers-site pages\n"
                    f"# timeouts: {TIMEOUT:.0f}s per page, {BOARD_TIMEOUT:.0f}s per board · parallel: {a.conc} · "
                    f"keywords: {' '.join(a.keywords)}\n\n")

    stop = asyncio.Event()
    with Live(state.panel(), console=console, refresh_per_second=8) as live:
        state.console = live.console

        async def paint():
            while not stop.is_set():
                live.update(state.panel())
                await asyncio.sleep(0.12)

        tasks = [asyncio.create_task(paint())]
        if proxied:
            tasks.append(asyncio.create_task(sample_ips(state, a.country, stop)))
        await collect(state, cfg, companies, a.conc, a.country)
        stop.set()
        await asyncio.gather(*tasks)
        live.update(state.panel())

    summary = finish(state, a.keywords)
    results = []
    why = Table(title="What went wrong", width=100)
    why.add_column("Reason")
    why.add_column("Requests", justify="right")
    for reason, n in state.reasons.most_common():
        why.add_row(reason, str(n))
    if not state.reasons:
        why.add_row("[green]nothing: every request returned job data", "0")
    results.append(why)
    results.append(country_table(state, a.keywords))
    results.append(Text.from_markup(f"[bold]{summary['spreadsheet']}[/]  "
                                    f"({summary['new_jobs_never_seen_before']:,} never seen before)"))
    if proxied:
        results.append(Text.from_markup(f"[cyan]DataImpulse traffic: {meter.receipt()}[/]"))
    else:
        results.append(Text.from_markup(f"[red]Missed: {state.pages_refused} careers-site pages refused, "
                                        f"{state.pages_error} failed (about "
                                        f"{(state.pages_refused + state.pages_error) * 10:,} job listings never seen).[/]"))
    results.append(Text(f"Full request log: {state.log_path.relative_to(LOG_DIR.parent)}", style="dim"))
    for r in results:
        console.print(r)

    if a.svg:
        shot = Console(record=True, width=100, file=open(os.devnull, "w"))
        shot.print(header(state, a, pages))
        shot.print(Text("log excerpt (every line is in the log file)", style="dim"))
        for line in excerpt(state):
            shot.print(line)
        shot.print(state.panel())
        for r in results:
            shot.print(r)
        shot.save_svg(a.svg, title="Global job sweep")


if __name__ == "__main__":
    asyncio.run(main())
