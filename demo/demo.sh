#!/usr/bin/env bash
# Recording-ready BEFORE → AFTER demo. Large-font terminal; press Enter to advance.
# No AI or other product branding on screen — only the agent's output and DataImpulse.
#   DEMO_FRESH=1   start with an empty job tracker/spreadsheet (recommended for takes)
#   DEMO_OPEN_SHEET=1   open jobs.xlsx at the spreadsheet step
#   DEMO_HEADLESS_AI=1  run the AI step as plain output instead of a live Claude Code session
set -euo pipefail
cd "$(dirname "$0")/.."
B=$'\e[1m'; C=$'\e[36m'; R=$'\e[0m'
beat() { echo; read -rp "${C}── $1 ── [Enter]${R}"; clear; echo "${B}$1${R}"; echo; }
quiet() { "$@" 2> >(grep -v "HTTP Request" >&2); }

[[ "${DEMO_FRESH:-0}" == 1 ]] && rm -f jobs.db jobs.xlsx jobs.csv
uv run jobagent receipt --reset >/dev/null

beat "BEFORE · 1. This is me: one home internet connection"
curl -s "http://ip-api.com/json/?fields=query,country,city,isp" | python3 -m json.tool

beat "BEFORE · 2. My job scraper sweeps the world's job listings from my connection"
USE_PROXY=0 quiet uv run python -m jobagent.sweep --mask -k ${DEMO_KEYWORDS:-engineer}

beat "SWITCH · Add DataImpulse to .env (show the dashboard now)"
grep -E '^DATAIMPULSE_(HOST|PORT)=' .env
echo "DATAIMPULSE_LOGIN=********   DATAIMPULSE_PASSWORD=********"

beat "AFTER · 3. The exact same sweep, through DataImpulse residential IPs"
quiet uv run python -m jobagent.sweep --mask -k ${DEMO_KEYWORDS:-engineer}

beat "AFTER · 4. Start the MCP server (terminal 2) and hook it into Claude Code (you type this part)"
if [[ "${DEMO_HEADLESS_AI:-0}" == 1 ]]; then   # unbranded output only, no typing
  claude -p "$(cat demo/agent_prompt.md)" --allowedTools 'mcp__dataimpulse__*' 'Read' 2>/dev/null
else
  cat demo/claude_steps.txt
  echo
  PS1=$'\e[36m(claude setup)\e[0m $ ' bash --norc -i   # type the steps; "exit" returns to the demo
fi

beat "AFTER · 5. Any country, real household IPs · rotating vs sticky"
quiet uv run jobagent ip ${DEMO_COUNTRIES:-ca us gb de in jp au br}

beat "AFTER · 6. Every job saved to my spreadsheet, by country, with AI scores"
uv run python -c "from jobagent import sheet; print(sheet.export())"
[[ "${DEMO_OPEN_SHEET:-0}" == 1 ]] && open jobs.xlsx

beat "7. What did DataImpulse cost for all of this?"
echo "${B}$(uv run jobagent receipt)${R}"
echo "Billed figure: DataImpulse dashboard → Usage Chart."
