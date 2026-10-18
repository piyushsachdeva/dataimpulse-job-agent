# Job search agent on DataImpulse residential proxies

A job scraper and AI agent tools (an MCP server for Claude Code) that send every request
through DataImpulse residential IPs.

**Everything you need is in [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md):** setup, the before/after demo,
using it from Claude Code

```bash
uv sync
cp .env.example .env                 # add your DataImpulse login + password
uv run python scripts/test_proxy.py  # every line should say PASS
DEMO_FRESH=1 ./demo/demo.sh          # the demo
uv run jobsearch-mcp --http          # the MCP server for AI agents: http://127.0.0.1:8765/mcp
```

Don't point it at sites whose terms forbid automated access; proxies don't make that allowed.
