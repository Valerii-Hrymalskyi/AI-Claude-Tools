"""Health check of this clone's task-delegator server, talking to it over stdio like Claude Code does.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe scripts\\doctor.py [--json]

Calls the server's delegator_doctor tool and prints a short summary (or the full JSON with --json).
Free: it starts no job on any agent. Exit code 0 when the server answered, 1 when it did not.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


async def doctor() -> dict:
    # The full environment, so TASK_DELEGATOR_* overrides reach the server as they would from Claude.
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=dict(os.environ))
    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        res = await s.call_tool("delegator_doctor", {}, allow_input_required=False)
    if res.is_error:
        raise RuntimeError(" ".join(getattr(c, "text", "") for c in res.content))
    sc = res.structured_content
    if sc is not None:
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(res.content[0].text)


def _row(label: str, ok: bool | None, text: str) -> str:
    mark = "OK " if ok else ("-- " if ok is None else "!! ")
    return f"{label:<24}{mark} {text}"


def summary(d: dict) -> list[str]:
    lines = [_row("server", True, f"task-delegator {d.get('delegator_version')}, Python {d.get('python')}")]
    for kind, k in (d.get("kinds") or {}).items():
        lines.append(_row(f"{kind} exe", bool(k.get("exe")), k.get("exe") or f"not found: {k.get('found_via')}"))
    usage = d.get("codex_usage") or {}
    lines.append(_row("codex limits", bool(usage.get("exists")), usage.get("script") if usage.get("exists") else
                      f"no usage script at {usage.get('script')}: Codex limit checks will say 'unknown' "
                      "(set TASK_DELEGATOR_CODEX_USAGE_SCRIPT)"))
    found = d.get("agents") or {}
    for aid, a in found.items():
        if "login" in a:  # codex
            ok = "logged in" in str(a["login"]).lower()
            lines.append(_row(f"agent {aid}", ok, f"{a.get('version') or '?'}; {a['login']}; home {a.get('home')}"))
        elif "auth" in a:  # claude
            auth = a["auth"] or {}
            ok = auth.get("loggedIn") is True
            lines.append(_row(f"agent {aid}", ok, f"home {a.get('home')}; " + (
                f"logged in ({auth.get('subscriptionType') or '?'})" if ok
                else f"NOT logged in (scripts\\claude_login.ps1 -Account {aid.partition(':')[2]})")))
        else:
            lines.append(_row(f"agent {aid}", False, "its CLI was not found"))
    if not any(a.startswith("claude:") for a in found):
        lines.append(_row("claude agents", None, f"none; add one: scripts\\claude_login.ps1 -Account <name> "
                                                 f"(folders in {(d.get('accounts_dirs') or {}).get('claude')})"))
    af = d.get("agents_file") or {}
    lines.append(_row("agents.json", None, f"{af.get('path')} ({'present' if af.get('exists') else 'absent: defaults only'})"))
    for p in d.get("problems") or []:
        lines.append(_row("problem", False, p))
    lines.append(_row("job history", None, f"{d.get('state_dir')} (kept {d.get('keep_days')} days)"))
    return lines


def main() -> int:
    try:
        d = asyncio.run(doctor())
    except Exception as e:  # any failure to start or talk to the server is the answer here
        print(f"server        !!  did not answer: {type(e).__name__}: {e}")
        return 1
    print(json.dumps(d, ensure_ascii=False, indent=2) if "--json" in sys.argv else "\n".join(summary(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
