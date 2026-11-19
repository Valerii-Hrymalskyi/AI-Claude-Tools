"""Limits of the lead's subordinates (TaskDelegator agents): by hand, as JSON, or as a Stop hook.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe scripts\\team_limits.py              report
    ...                                                  scripts\\team_limits.py --json       JSON
    ...                                                  scripts\\team_limits.py --hook claude-stop
    ...                                                  scripts\\team_limits.py --install-hook | --uninstall-hook

--install-hook adds the Stop hook to the settings.json of the account it runs for (CLAUDE_CONFIG_DIR,
default ~/.claude) - the lead's account, not an agent's. After every turn the user then sees the
team's limits next to the lead's own (UsageGuard's Stop hook).

Nothing here knows the kinds of agents: every agent's limits come from agents.read_limits(), which
asks the agent's adapter for them in one shape (schema 1) - see the adapters for the sources
(codex: Codex's usage script; claude: UsageGuard for that account, else its last job's
rate_limit_event). A new kind of agent shows up here as soon as its adapter can read its usage.
Agents are read in parallel with a short timeout; the hook never breaks a turn - a problem becomes a
line of its own. The report is cached for CACHE_SEC so that quick turns do not read it again.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]          # <clone>/Projects/MCP/TaskDelegator
sys.path.insert(0, str(ROOT))

from task_delegator import agents, limits  # noqa: E402

READ_TIMEOUT_SEC = 8          # per agent; agents are read in parallel
CACHE_SEC = 30
HOOK_MARK = "team_limits.py"  # how --install-hook / --uninstall-hook recognise their entry

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass


def cache_file() -> Path:
    return Path(os.environ.get("TASK_DELEGATOR_TEAM_LIMITS_CACHE")
                or Path(tempfile.gettempdir()) / "task-delegator-team-limits.json")


# --------------------------------------------------------------------------- reading


def _fmt_time(dt: datetime | None) -> str:
    """Local reset time as UsageGuard prints it, "YYYY-mm-dd HH:MM:SS", rounded to the minute
    (a server's reset time jitters by a second or two: 19:12:59 vs 19:13:00)."""
    if dt is None:
        return "?"
    dt = (dt.astimezone() if dt.tzinfo else dt) + timedelta(seconds=30)
    return dt.strftime("%Y-%m-%d %H:%M:00")


def _parse_time(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    for parse in (datetime.fromisoformat, lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M:%S")):
        try:
            return parse(value)
        except ValueError:
            continue
    return None


def _windows_of(verdict: dict) -> list[dict]:
    return [{"label": w.get("window"), "used": w.get("used_percent"), "reset": _fmt_time(_parse_time(w.get("resets_at")))}
            for w in verdict.get("windows") or []]


def read_agent(agent: agents.Agent) -> dict:
    """One agent's row, whatever its kind."""
    try:
        verdict, _snapshot = agents.read_limits(agent, timeout=READ_TIMEOUT_SEC - 2)
    except limits.LimitsUnavailable as e:
        return {"state": "unknown", "error": str(e)[:160]}
    return {"state": verdict["state"], "windows": _windows_of(verdict), "source": verdict.get("source")}


def read_team(found: dict[str, agents.Agent] | None = None) -> list[dict]:
    if found is None:
        found, _problems = agents.discover()
    rows: list[dict] = [{"agent": a.id, "name": a.setting("name") or a.id} for a in found.values()]
    results: list[dict | None] = [None] * len(rows)

    def work(i: int, agent: agents.Agent) -> None:
        try:
            results[i] = read_agent(agent)
        except Exception as e:  # noqa: BLE001 - one agent's failure must not hide the others
            results[i] = {"state": "unknown", "error": f"{type(e).__name__}: {str(e)[:120]}"}

    threads = [threading.Thread(target=work, args=(i, a), daemon=True) for i, a in enumerate(found.values())]
    for t in threads:
        t.start()
    deadline = time.monotonic() + READ_TIMEOUT_SEC + 4
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    for row, res in zip(rows, results):
        row.update(res or {"state": "unknown", "error": f"no answer within {READ_TIMEOUT_SEC + 4} s"})
    return rows


def _team_key(found: dict[str, agents.Agent]) -> str:
    """What the cached rows are about: the agents, their homes and names, and the environment that
    picks a default account (CODEX_HOME for "codex", any other *_HOME / *_CONFIG_DIR a kind may
    use). Another set - another agents.json, accounts root, account folder or default home - must not
    be served this one's numbers."""
    ident = sorted((a.id, str(a.home), str(a.setting("name") or "")) for a in found.values())
    homes = sorted((k.upper(), v) for k, v in os.environ.items()
                   if k.upper().endswith(("_HOME", "_CONFIG_DIR")))
    return hashlib.sha1(json.dumps([ident, homes]).encode("utf-8")).hexdigest()


def team(fresh: bool = False) -> list[dict]:
    """The rows, from the cache when it is younger than CACHE_SEC and about the same agents."""
    found, _problems = agents.discover()
    key = _team_key(found)
    path = cache_file()
    if not fresh:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("key") == key and time.time() - float(cached["t"]) < CACHE_SEC:
                return cached["rows"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
    rows = read_team(found)
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps({"t": time.time(), "key": key, "rows": rows}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # another session wrote it at the same moment; the next turn reads one or the other
    return rows


# --------------------------------------------------------------------------- output


def _pct(value: Any) -> str:
    return f"{float(value):.1f}%" if isinstance(value, (int, float)) else "?"


def format_rows(rows: list[dict]) -> str:
    """The same layout as UsageGuard's own "Limits:" block, one block per agent:

        Team limits:
        Developer (Claude Pro) — PAUSE:
        5h: 99.0%, R: 2026-11-19 05:00:00
        7d: 36.0%, R: 2026-11-22 05:00:00

    Every window line starts at the same place whatever the font: Claude Code shows hook messages
    in a proportional font, where padding with spaces cannot line columns up.
    """
    lines = ["Team limits:"]
    for r in rows:
        flag = f" — {r['state'].upper()}" if r.get("state") in ("warn", "pause") else ""
        if r.get("windows"):
            lines.append(f"{r['name']}{flag}:")
            lines += [f"{w['label']}: {_pct(w['used'])}, R: {w['reset']}" for w in r["windows"]]
        else:
            lines.append(f"{r['name']}: ? ({r.get('error') or 'no data'})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- hook install


def settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def _hook_entry() -> dict:
    return {"hooks": [{"type": "command", "command": sys.executable.replace("\\", "/"),
                       "args": [str(Path(__file__).resolve()).replace("\\", "/"), "--hook", "claude-stop"],
                       "timeout": 20}]}


def _load_settings(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}


def _save_settings(path: Path, data: dict) -> None:
    """Back up the current file (the copy is the state right before this change), then replace it
    atomically: an interrupted write must not leave the account with a broken settings.json."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-team-limits"))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_hook() -> int:
    path = settings_path()
    data = _load_settings(path)
    stop = data.setdefault("hooks", {}).setdefault("Stop", [])
    if any(HOOK_MARK in json.dumps(group) for group in stop):
        print(f"already installed: {path}")
        return 0
    stop.append(_hook_entry())
    _save_settings(path, data)
    print(f"Stop hook added: {path}\nIt takes effect in Claude sessions started from now on.")
    return 0


def uninstall_hook() -> int:
    path = settings_path()
    data = _load_settings(path)
    stop = (data.get("hooks") or {}).get("Stop") or []
    kept = []
    for group in stop:
        hooks = [h for h in group.get("hooks") or [] if HOOK_MARK not in json.dumps(h)]
        if hooks:
            kept.append({**group, "hooks": hooks})
    if len(kept) == len(stop) and all(HOOK_MARK not in json.dumps(g) for g in stop):
        print(f"not installed: {path}")
        return 0
    if kept:
        data["hooks"]["Stop"] = kept
    else:
        data["hooks"].pop("Stop", None)
        if not data["hooks"]:
            data.pop("hooks")
    _save_settings(path, data)
    print(f"Stop hook removed: {path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="print the rows as JSON")
    ap.add_argument("--hook", choices=["claude-stop"], help="print a Claude Code Stop-hook systemMessage")
    ap.add_argument("--fresh", action="store_true", help="ignore the cache")
    ap.add_argument("--install-hook", action="store_true")
    ap.add_argument("--uninstall-hook", action="store_true")
    args = ap.parse_args()

    if args.install_hook:
        return install_hook()
    if args.uninstall_hook:
        return uninstall_hook()
    if args.hook:
        # The hook's input (on stdin) is not needed and is deliberately not read: waiting for its
        # end could hold the turn up if the pipe stayed open.
        try:
            rows = team(args.fresh)
            if rows:
                print(json.dumps({"systemMessage": format_rows(rows)}, ensure_ascii=False))
        except Exception as e:  # noqa: BLE001 - a hook must never break the turn
            print(json.dumps({"systemMessage": f"Team limits: unavailable ({type(e).__name__}: {e})"},
                             ensure_ascii=False))
        return 0
    rows = team(args.fresh)
    print(json.dumps(rows, ensure_ascii=False, indent=2) if args.json else format_rows(rows) if rows
          else "No agents found (see delegator_doctor).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
