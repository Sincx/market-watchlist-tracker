"""Equibles MCP client — thin JSON-RPC wrapper over the free-tier Equibles
MCP server (https://mcp.equibles.com/mcp). Evaluated 2026-09-10 alongside FMP
(fred-finance-system-spec.md §7/§10).

Two uses in this pipeline so far:
  - GetIndexComposition: fresher, more authoritative S&P 500 constituents
    than Wikipedia (sourced from IVV's actual daily fund-holdings basket,
    not a community-edited page) — used as universe.py's primary S&P 500
    source, Wikipedia as fallback. Equibles' free tier only covers US
    indices (sp-500/sp-400/sp-600/nasdaq-100/russell-1000/russell-2000/
    dow-jones) — no FTSE or STOXX, so those stay Wikipedia-only.
  - GetSuperInvestors / GetInstitutionPortfolio: for any tracked investor
    OTHER than Burry (per the 2026-09-10 decision — Burry's 13F data proved
    too stale, 8 positions dated over a year back, so Burry stays sourced
    from wiki trading-post prose per the original spec design). Use this
    for a second/future tracked investor instead of building a new
    prose-extraction pipeline each time.

Auth: Authorization: Bearer <key> header, configured in ~/.claude.json's
mcpServers.equibles.headers (not read from this pipeline's own .env —
this module reads the same credential the Claude Code MCP config uses,
via mcp_headers(), so there's one place the key is stored).
"""
import json
from pathlib import Path

import requests

_URL = "https://mcp.equibles.com/mcp"
_CLAUDE_JSON = Path.home() / ".claude.json"


def _auth_headers() -> dict:
    try:
        config = json.loads(_CLAUDE_JSON.read_text(encoding="utf-8"))
        return config["mcpServers"]["equibles"]["headers"]
    except Exception:
        return {}


def _post(payload: dict, session_id: str | None = None) -> tuple[dict | None, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        **_auth_headers(),
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    try:
        r = requests.post(_URL, json=payload, headers=headers, timeout=30)
    except Exception:
        return None, session_id
    new_sid = r.headers.get("Mcp-Session-Id", session_id)
    ct = r.headers.get("Content-Type", "")
    if "text/event-stream" in ct:
        for line in r.text.splitlines():
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:]), new_sid
                except Exception:
                    pass
        return None, new_sid
    try:
        return r.json(), new_sid
    except Exception:
        return None, new_sid


class Session:
    """One MCP session: initialize once, call tools as needed, discard."""

    def __init__(self):
        self.sid = None
        init, self.sid = _post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "market-watchlist-pipeline", "version": "1.0"},
            },
        })
        self.ok = bool(init and "error" not in init)
        if self.ok:
            headers = {"Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream",
                       **_auth_headers()}
            if self.sid:
                headers["Mcp-Session-Id"] = self.sid
            try:
                requests.post(_URL, json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                              headers=headers, timeout=10)
            except Exception:
                pass

    def call(self, tool: str, arguments: dict) -> str | None:
        """Call a tool, return its text content (or None on any failure)."""
        if not self.ok:
            return None
        resp, self.sid = _post({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }, self.sid)
        if not resp or "error" in resp:
            return None
        content = resp.get("result", {}).get("content", [])
        text = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        return text or None
