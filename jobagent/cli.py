"""Plain command line for AIs without tool support (ChatGPT / Gemini web chat, etc.).

    uv run jobagent search -k backend platform -l London --sponsorship
    uv run jobagent prompt -k backend -l London > prompt.md   # paste into any chat
"""

import argparse
import asyncio
from pathlib import Path

import sys

from . import geo, sheet, sources, store
from .proxy import LEDGER, Meter, meter

ROOT = Path(__file__).resolve().parent.parent


def table(jobs) -> str:
    rows = ["| # | Company | Title | Location | Sponsorship | URL |", "|---|---|---|---|---|---|"]
    rows += [f"| {i} | {j.company} | {j.title} | {j.location} | {j.sponsorship} | {j.url} |"
             for i, j in enumerate(jobs, 1)]
    return "\n".join(rows)


def run(args) -> list:
    jobs, errors = asyncio.run(sources.fetch_all(country=args.country))
    fresh = store.mark_seen(jobs)
    hits = [j for j in jobs if sources.matches(j, args.keywords, args.location, args.sponsorship)
            and (not args.new or j.id in fresh)]
    hits.sort(key=lambda j: {"yes": 0, "unknown": 1, "no": 2}[j.sponsorship])
    store.mark_matched(j.id for j in hits)
    print(f"<!-- {sheet.export()} -->", file=sys.stderr)
    for e in errors:
        print(f"<!-- error: {e} -->")
    return hits[: args.limit]


def cmd_ip(args):
    """Exit IP per country, then rotating vs sticky behaviour."""
    for cc in args.countries:
        info = asyncio.run(geo.exit_ip(cc))
        print(f"  {cc.upper():3} -> {info.get('query'):16} {info.get('city')}, {info.get('country')}  ({info['elapsed']}s)")
    for label, sticky in [("rotating", False), ("sticky", True)]:
        ips = [asyncio.run(geo.exit_ip(args.countries[0], sticky))["query"] for _ in range(3)]
        print(f"  {label:8}: {' , '.join(ips)}  ({len(set(ips))} distinct)")


def cmd_compare(args):
    for url in args.urls:
        print(f"\n## {url}\n")
        print("| Country | HTTP | Final URL | Title | Chars | Currencies | Similarity to first |")
        print("|---|---|---|---|---|---|---|")
        views = asyncio.run(geo.compare(url, args.countries))
        for v in views:
            if "error" in v:
                print(f"| {v['country'].upper()} | error | {v['error'][:60]} | | | | |")
                continue
            print(f"| {v['country'].upper()} | {v['status']} | {v['final_url'][:60]} | {v['title']} | "
                  f"{v['chars']} | {', '.join(v['currencies'])} | {v.get('similarity', '')} |")
        for v in views[1:]:
            if v.get("unique_sample"):
                print(f"\nOnly in {v['country'].upper()}: " + " / ".join(v["unique_sample"]))


def main():
    ap = argparse.ArgumentParser(prog="jobagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("receipt", help="total proxy usage and cost since the last --reset")
    p.add_argument("--reset", action="store_true")
    p = sub.add_parser("ip", help="show exit IPs per country and rotating vs sticky sessions")
    p.add_argument("countries", nargs="*", default=["gb", "de", "us", "in"])
    p = sub.add_parser("compare", help="fetch the same page as a local in several countries")
    p.add_argument("urls", nargs="+")
    p.add_argument("-c", "--countries", nargs="+", default=["in", "gb", "de", "us"])
    for name, help_ in [("search", "print matching jobs as a Markdown table"),
                        ("prompt", "print a ready-to-paste prompt: profile + jobs + instructions")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("-k", "--keywords", nargs="*", default=[])
        p.add_argument("-l", "--location")
        p.add_argument("--sponsorship", action="store_true", help="drop jobs that say they won't sponsor")
        p.add_argument("--new", action="store_true", help="only jobs not seen in a previous run")
        p.add_argument("--country", help="proxy exit country, ISO-2")
        p.add_argument("--limit", type=int, default=40)
        p.add_argument("--desc-chars", type=int, default=600, help="description excerpt per job (prompt only)")
    args = ap.parse_args()

    if args.cmd == "receipt":
        if args.reset:
            LEDGER.unlink(missing_ok=True)
            print("Usage ledger reset.")
        else:
            print(f"Total since reset: {Meter.from_ledger().receipt()}")
        return

    if args.cmd in ("ip", "compare"):
        (cmd_ip if args.cmd == "ip" else cmd_compare)(args)
        sys.stdout.flush(); print(f"\nReceipt: {meter.receipt()}", file=sys.stderr)
        return

    hits = run(args)
    if args.cmd == "search":
        print(table(hits))
        sys.stdout.flush(); print(f"\nReceipt: {meter.receipt()}", file=sys.stderr)
        return

    profile = (ROOT / "profile.md").read_text()
    jobs = "\n\n".join(
        f"### {i}. {j.title} — {j.company} ({j.location})\nSponsorship signal: {j.sponsorship}\n{j.url}\n\n"
        f"{j.description[: args.desc_chars]}"
        for i, j in enumerate(hits, 1)
    )
    print(f"""You are helping me with a job search. Here is my profile:

{profile}

Below are {len(hits)} open roles. Score each 0-100 for fit against my profile (skills, seniority,
location, and visa sponsorship if I need it). Return a table ranked by score: #, company, title,
score, one-line reason, URL. Then list the top 3 with what I should emphasise when applying.

{jobs}
""")
    sys.stdout.flush(); print(f"Receipt: {meter.receipt()}", file=sys.stderr)


if __name__ == "__main__":
    main()
