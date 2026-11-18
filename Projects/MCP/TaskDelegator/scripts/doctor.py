"""Health check of this clone's task-delegator server, talking to it over stdio like Claude Code does.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe scripts\\doctor.py [--json]

Calls the server's delegator_doctor tool and prints a short summary (or the full JSON with --json).
Free: it starts no Codex or worker job. Exit code 0 when the server answered, 1 when it did not.
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


def summary(d: dict) -> list[str]:
    codex = d.get("codex") or {}
    usage = d.get("codex_usage") or {}
    worker = d.get("worker_b") or {}
    auth = worker.get("auth") or {}
    lines = [f"server        OK  task-delegator {d.get('delegator_version')}, Python {d.get('python')}"]
    if codex.get("exe"):
        lines.append(f"codex.exe     OK  {codex['exe']} ({codex.get('version') or '?'})")
        lines.append(f"codex login   {'OK ' if 'logged in' in str(codex.get('login')).lower() else '!! '} "
                     f"{codex.get('login')}")
    else:
        lines.append(f"codex.exe     !!  not found: {codex.get('found_via')}")
    if usage.get("exists"):
        lines.append(f"codex limits  OK  {usage.get('script')}")
    else:
        lines.append(f"codex limits  !!  no usage script at {usage.get('script')}: limit checks will say 'unknown' "
                     "(set TASK_DELEGATOR_CODEX_USAGE_SCRIPT)")
    if worker.get("exe"):
        ok = auth.get("loggedIn") is True
        lines.append(f"worker B      {'OK ' if ok else '!! '} {worker['exe']}; config {worker.get('config_dir')}; "
                     + ("logged in" if ok else "NOT logged in (run scripts\\worker_login.ps1)"))
    else:
        lines.append(f"worker B      !!  claude.exe not found: {worker.get('found_via')}")
    lines.append(f"job history   --  {d.get('state_dir')} (kept {(d.get('limits') or {}).get('keep_days')} days)")
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
