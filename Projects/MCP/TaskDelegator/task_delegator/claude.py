"""Another Claude: Claude Code on another Claude account, run headless (`claude -p`) like Codex.

Each account lives in its own config dir (CLAUDE_CONFIG_DIR), by default
%USERPROFILE%\\.claude-accounts\\<name> (agent id "claude:<name>"): its own login, settings,
history and user-level CLAUDE.md. It runs under the user's Windows account, so it has the same
file rights as the lead Claude session. task-delegator is not registered in those configs, so
another Claude cannot delegate further. The lead's own ~/.claude is never used.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import agents, jobs, limits, procs

# <clone>/Projects/MCP/TaskDelegator/task_delegator/claude.py -> parents[4] is the clone root.
_CLONE_ROOT = Path(__file__).resolve().parents[4]
GUARD_TIMEOUT_SEC = 10

PROVIDER = "claude"
EXE_NAME = "claude.exe" if os.name == "nt" else "claude"
FINAL_MESSAGE_LIMIT = 60_000

# Tools the agent may use. In read-only mode nothing that can change files or run commands.
READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"]
EDIT_EXTRA = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
WRITE_EXTRA = [*EDIT_EXTRA, "Bash"]
SHELL_TOOLS = ["Bash", "PowerShell"]
READ_ONLY_DENY = [*EDIT_EXTRA, *SHELL_TOOLS]
EDIT_TOOLS = set(EDIT_EXTRA)

# Permission rules denied to write jobs that do have a shell; per agent: "deny" in agents.json.
# A guard against accidents, not a sandbox: the agent still runs with the user's Windows rights.
DEFAULT_DENY = ["Bash(git push:*)"]
DEFAULT_WORKFLOW_AGENTS = 3

# Inherited env vars that must not reach the agent: they belong to the lead session or its account.
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC", "MCP_")
_KEEP = {"CLAUDE_CODE_GIT_BASH_PATH"}

# Ultracode needs workflows, which are off by default on a Pro plan. Passed per run with
# --settings so the account's own settings.json stays untouched.
WORKFLOW_SETTINGS = Path(__file__).resolve().parent / "worker_settings.json"

TERMINAL = {"completed", "failed", "cancelled", "lost"}


def find_claude() -> tuple[str | None, str]:
    override = os.environ.get("TASK_DELEGATOR_CLAUDE_EXE")
    if override:
        return (override if os.path.isfile(override) else None), "TASK_DELEGATOR_CLAUDE_EXE"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found = glob.glob(os.path.join(local, "Claude-Data", "claude_*", "claude-code", "*", "*", EXE_NAME))
        if found:
            return max(found, key=os.path.getmtime), "Claude desktop app bundle"
    native = Path.home() / ".local" / "bin" / EXE_NAME
    if native.is_file():
        return str(native), "~/.local/bin"
    on_path = shutil.which("claude")
    if on_path and (not procs.IS_WINDOWS or on_path.lower().endswith(".exe")):
        return on_path, "PATH"
    return None, "not found (set TASK_DELEGATOR_CLAUDE_EXE)"


def child_env(home: str, ultracode: bool = False, workflow_agents: int = DEFAULT_WORKFLOW_AGENTS) -> dict[str, str]:
    # Scrubbing CLAUDE* also drops CLAUDE_CODE_EFFORT_LEVEL, which would override --effort.
    env = {k: v for k, v in os.environ.items()
           if k.upper() in _KEEP or not k.upper().startswith(_SCRUB_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(home)
    if ultracode:
        # Caps concurrent workflow agents only (not their total): keeps a Pro plan's window alive.
        env["CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"] = str(workflow_agents)
    env.setdefault("NO_COLOR", "1")
    return env


def auth_status(exe: str, home: str) -> dict:
    """`claude auth status` for that account. Never includes the email in the result."""
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, stdin=subprocess.DEVNULL, env=child_env(home),
                           creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0)
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"loggedIn": None, "error": str(e)[:200]}
    return {k: data.get(k) for k in ("loggedIn", "authMethod", "subscriptionType", "loginExpiresAt")
            if k in data}


def build_argv(exe: str, mode: str, model: str | None, effort: str | None,
               add_dirs: list[str] | None, max_turns: int | None, resume_session: str | None,
               ultracode: bool = False, shell: bool = True, deny: list[str] | None = None) -> list[str]:
    """ultracode (write mode only): `--effort ultracode` turns it on for the run and implies xhigh.
    The "ultracode" keyword inside a -p prompt does nothing: it only triggers on human-typed prompts.
    shell (write mode only): False leaves the agent file edits only, no Bash/PowerShell.
    deny: permission rules denied in write mode with a shell (default DEFAULT_DENY)."""
    ultracode = ultracode and mode == "write"
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
    if mode == "write":
        # acceptEdits: edits are auto-approved only inside the working dirs (cwd + --add-dir);
        # anything else that would need a prompt is denied in headless mode.
        tools = [*READ_TOOLS, *(WRITE_EXTRA if shell else EDIT_EXTRA)]
        argv += ["--permission-mode", "acceptEdits", "--allowedTools", *tools]
        rules = (DEFAULT_DENY if deny is None else deny) if shell else SHELL_TOOLS  # deny wins over allow
        if rules:
            argv += ["--disallowedTools", *rules]
    else:
        # dontAsk: whatever is not explicitly allowed is denied instead of prompting.
        argv += ["--permission-mode", "dontAsk", "--allowedTools", *READ_TOOLS,
                 "--disallowedTools", *READ_ONLY_DENY]
    if model:
        argv += ["--model", model]
    if ultracode:
        argv += ["--effort", "ultracode", "--settings", str(WORKFLOW_SETTINGS)]
    elif effort:
        argv += ["--effort", effort]
    for d in add_dirs or []:
        argv += ["--add-dir", d]
    if max_turns:
        argv += ["--max-turns", str(max_turns)]
    if resume_session:
        argv += ["--resume", resume_session]
    # The prompt goes in on stdin (no Windows quoting or length limits).
    return argv


# --------------------------------------------------------------------------- events


def _short(value: Any, limit: int = 160) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def parse_events(path: Path) -> dict:
    """Fold `claude -p --output-format stream-json` events into a compact summary."""
    out: dict[str, Any] = {
        "session_id": None, "model": None, "result": None, "is_error": None, "subtype": None,
        "turns": None, "cost_usd": None, "usage": None, "permission_denials": [],
        "tool_uses": [], "files_edited": [], "texts": [], "events": 0, "rate_limits": None,
        "workflow_available": None,
    }
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out
    files: dict[str, None] = {}
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        out["events"] += 1
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            out["session_id"] = ev.get("session_id") or out["session_id"]
            out["model"] = ev.get("model")
            tools = ev.get("tools")
            if isinstance(tools, list):
                out["workflow_available"] = "Workflow" in tools
        elif t == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    out["texts"].append(block["text"])
                elif block.get("type") == "tool_use":
                    name, inp = block.get("name"), block.get("input") or {}
                    detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") \
                        or inp.get("query") or inp.get("url") or inp
                    out["tool_uses"].append({"tool": name, "input": _short(detail)})
                    if name in EDIT_TOOLS and isinstance(inp.get("file_path") or inp.get("notebook_path"), str):
                        files[inp.get("file_path") or inp.get("notebook_path")] = None
        elif t == "rate_limit_event":
            out["rate_limits"] = ev.get("rate_limit_info") or ev
        elif t == "result":
            out["session_id"] = ev.get("session_id") or out["session_id"]
            out["result"] = ev.get("result")
            out["is_error"] = ev.get("is_error")
            out["subtype"] = ev.get("subtype")
            out["turns"] = ev.get("num_turns")
            out["cost_usd"] = ev.get("total_cost_usd")
            u = ev.get("usage") or {}
            out["usage"] = {k: u.get(k) for k in ("input_tokens", "cache_read_input_tokens",
                                                   "cache_creation_input_tokens", "output_tokens") if k in u}
            out["permission_denials"] = [
                {"tool": d.get("tool_name"), "input": _short(d.get("tool_input"))}
                for d in ev.get("permission_denials") or [] if isinstance(d, dict)]
    out["files_edited"] = list(files)
    return out


# --------------------------------------------------------------------------- limits
#
# Two sources, converted to schema 1 (agents.read_limits): UsageGuard, which asks the server for the
# account when its folder holds a usage-limits-token (measured; one tiny request, cached 20-90 s), and,
# without it, the newest rate_limit_event of the account's own jobs (free, as fresh as its last job).


def usage_guard_script() -> Path:
    return Path(os.environ.get("TASK_DELEGATOR_USAGE_GUARD")
                or _CLONE_ROOT / "Projects" / "UsageGuard" / "usage_guard.py")


def _local_ts(value: Any) -> float | None:
    """UsageGuard's "YYYY-mm-dd HH:MM:SS" local time as a unix time."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def usage_from_guard(d: dict, now: float | None = None) -> dict:
    """UsageGuard's `-mode json` for one account, in schema 1."""
    now = time.time() if now is None else now
    windows, reached = [], None
    for pct_key, reset_key, status_key, kind, label in (
            ("live_pct", "reset_at", "probe_status_5h", "primary", "5h"),
            ("seven_day_pct", "weekly_reset_at", "probe_status_7d", "secondary", "7d")):
        ts = _local_ts(d.get(reset_key))
        used = d.get(pct_key)
        windows.append({
            "kind": kind, "label": label,
            "used_percent": float(used) if isinstance(used, (int, float)) else None,
            "resets_at_unix": ts, "resets_in_sec": int(ts - now) if ts else None,
            "resets_at": datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None,
        })
        if d.get(status_key) == "rejected":
            reached = f"{label} window rejected the last request"
    bucket: dict[str, Any] = {"id": "claude", "name": "claude", "windows": windows}
    if reached:
        bucket["limit_reached"] = reached
    # A probe answered from UsageGuard's cache is as old as that cache, not "now".
    age = d.get("probe_age_sec") if d.get("source") == "server" else None
    taken = now - float(age) if isinstance(age, (int, float)) and age > 0 else now
    measured = datetime.fromtimestamp(taken).astimezone().isoformat(timespec="seconds")
    return {"schema": 1, "measured_at": measured, "buckets": [bucket]}


def merge_usage(a: dict, b: dict) -> dict:
    """The worse of two schema-1 readings of one account, window by window.

    The sources can disagree for a while - a cached probe against a job that has just been
    refused - and for a budget gate the pessimistic one is the safe one. A window that has reset
    since a reading already reads 0 % in it (usage_from_rate_limits), so an old figure cannot block
    a new window.
    """
    def windows(u: dict) -> dict[str, dict]:
        return {w.get("kind"): w for bk in u.get("buckets") or [] for w in bk.get("windows") or []}

    wa, wb = windows(a), windows(b)
    merged = []
    for kind in dict.fromkeys([*wa, *wb]):
        x, y = wa.get(kind), wb.get(kind)
        if x is None or y is None:
            merged.append(x or y)
            continue
        ux, uy = x.get("used_percent"), y.get("used_percent")
        worse_y = isinstance(uy, (int, float)) and (not isinstance(ux, (int, float)) or uy > ux)
        merged.append(y if worse_y else x)
    bucket: dict[str, Any] = {"id": "claude", "name": "claude", "windows": merged}
    reached = [bk["limit_reached"] for u in (a, b) for bk in u.get("buckets") or [] if bk.get("limit_reached")]
    if reached:
        bucket["limit_reached"] = reached[0]
    measured = max(a.get("measured_at") or "", b.get("measured_at") or "") or None
    return {"schema": 1, "measured_at": measured, "buckets": [bucket]}


def read_usage(agent: agents.Agent, timeout: float | None = None) -> tuple[dict, str]:
    """The account's limits in schema 1 and where they came from. Raises limits.LimitsUnavailable.

    UsageGuard (measured, or its estimate) and the newest rate_limit_event of the account's own
    jobs are both read; when both exist, the worse figure of each window wins (merge_usage).
    """
    problem = guard_usage = guard_source = None
    guard = usage_guard_script()
    if guard.is_file():
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(agent.home)}
        try:
            r = procs.run_capture([sys.executable, str(guard), "-mode", "json"], timeout or GUARD_TIMEOUT_SEC,
                                  env=env, text=True)
            d = json.loads(r.stdout)
            if isinstance(d, dict) and d.get("ok"):
                guard_usage = usage_from_guard(d)
                guard_source = "measured" if d.get("source") == "server" else "UsageGuard estimate"
            else:
                problem = "UsageGuard has no data for this account"
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            problem = f"UsageGuard: {type(e).__name__}"
    rl, measured, jid = last_rate_limits(agent.id)
    job_usage = usage_from_rate_limits(rl, measured) if rl is not None else None
    job_source = f"its last job ({jid}, {(measured or '?')[11:16]})"
    if guard_usage is None and job_usage is None:
        raise limits.LimitsUnavailable("no recent job of this account to read its limits from"
                                       + (f"; {problem}" if problem else ""))
    if guard_usage is None:
        return job_usage, job_source
    if job_usage is None:
        return guard_usage, guard_source
    merged = merge_usage(guard_usage, job_usage)
    if merged["buckets"][0] == guard_usage["buckets"][0]:
        return merged, guard_source
    return merged, f"{guard_source}, worse in {job_source}"


# How many of this account's most recently written logs are parsed for a reading.
_LIMITS_LOOKBACK_JOBS = 10


def last_rate_limits(agent_id: str) -> tuple[dict | None, str | None, str | None]:
    """The account's newest rate_limit_event: (info, measured_at, job_id).

    "Newest" by when the job's log was last written, not by when the job started: a long job that
    is still running reports fresher numbers than a short one started after it. Only this account's
    jobs are considered. Free: Claude Code reports the account's windows in every run.
    """
    candidates = []
    for jid in jobs.list_job_ids(PROVIDER, limit=1_000_000):
        try:
            meta = jobs.load_meta(jid)
        except (KeyError, ValueError):
            continue
        if meta.get("agent") != agent_id:
            continue
        log = jobs.job_dir(jid) / "stdout.log"
        try:
            candidates.append((log.stat().st_mtime, jid, log))
        except OSError:
            continue
    candidates.sort(reverse=True)
    for mtime, jid, log in candidates[:_LIMITS_LOOKBACK_JOBS]:
        rl = parse_events(log)["rate_limits"]
        if isinstance(rl, dict):
            return rl, datetime.fromtimestamp(mtime).astimezone().isoformat(timespec="seconds"), jid
    return None, None, None


def usage_from_rate_limits(rl: dict, measured_at: str | None, now: float | None = None) -> dict:
    """Turn a rate_limit_event into the schema-1 shape limits.evaluate() understands.

    A window whose reset time has passed since the reading has started over: its old figure says
    nothing any more, so it is reported as 0 % rather than blocking on stale data.
    """
    now = time.time() if now is None else now
    windows, reached = [], None
    unified = rl.get("unifiedWindows") if isinstance(rl.get("unifiedWindows"), dict) else {}
    top_status, top_reset, top_type = rl.get("status"), rl.get("resetsAt"), rl.get("rateLimitType")
    top_live = isinstance(top_reset, (int, float)) and top_reset > now
    if not unified and top_type in ("five_hour", "seven_day"):
        # An event without per-window data still names the window it is about.
        unified = {top_type: {"status": top_status, "resetsAt": top_reset}}
    for key, kind, label in (("five_hour", "primary", "5h"), ("seven_day", "secondary", "7d")):
        w = unified.get(key)
        if not isinstance(w, dict):
            continue
        resets = w.get("resetsAt")
        util = w.get("utilization")
        expired = isinstance(resets, (int, float)) and resets <= now
        used = 0.0 if expired else (round(float(util) * 100, 1) if isinstance(util, (int, float)) else None)
        windows.append({
            "kind": kind, "label": label, "used_percent": used,
            "resets_at_unix": resets if isinstance(resets, (int, float)) and not expired else None,
            "resets_in_sec": (int(resets - now) if isinstance(resets, (int, float)) and not expired else None),
            "resets_at": (datetime.fromtimestamp(resets).astimezone().isoformat(timespec="seconds")
                          if isinstance(resets, (int, float)) and not expired else None),
        })
        if w.get("status") == "rejected" and not expired:
            reached = f"{label} window rejected the last request"
    if top_status == "rejected" and top_live and not reached:
        # Rejected, but not attributable to a known window (or the window data is missing).
        reached = f"the last request was rejected ({top_type or 'limit'})"
        if not any(w.get("resets_at_unix") for w in windows):
            windows.append({"kind": "other", "label": str(top_type or "limit"), "used_percent": None,
                            "resets_at_unix": top_reset, "resets_in_sec": int(top_reset - now),
                            "resets_at": datetime.fromtimestamp(top_reset).astimezone().isoformat(timespec="seconds")})
    bucket: dict[str, Any] = {"id": "claude", "name": "claude", "windows": windows}
    if reached:
        bucket["limit_reached"] = reached
    return {"schema": 1, "measured_at": measured_at, "buckets": [bucket]}


# --------------------------------------------------------------------------- jobs


def job_status(job_id: str) -> tuple[str, dict, dict]:
    meta = jobs.load_meta(job_id)
    d = jobs.job_dir(job_id)
    running = jobs.is_running(job_id, meta)
    exit_info = None
    if not running:
        exit_info = jobs.read_json(d / "exit.json")
        if exit_info is None and meta.get("pid"):
            exit_info = jobs.finalize_unobserved(job_id)
    ev = parse_events(d / "stdout.log")
    done_ok = ev["subtype"] == "success" and ev["is_error"] is False
    if meta.get("cancel_requested"):
        status = "cancelling" if running else "cancelled"
    elif running:
        status = "running"
    elif exit_info is None:
        status = "failed"
    elif exit_info.get("unobserved"):
        status = "completed" if done_ok else ("failed" if ev["subtype"] else "lost")
    else:
        status = "completed" if exit_info.get("exit_code") == 0 and done_ok else "failed"
    return status, meta, ev


def _elapsed(meta: dict, d: Path) -> int | None:
    try:
        start = datetime.fromisoformat(meta["started_at"])
    except (KeyError, ValueError):
        return None
    try:
        end = datetime.fromisoformat((jobs.read_json(d / "exit.json") or {})["finished_at"])
    except (KeyError, ValueError):
        end = datetime.now().astimezone()
    return int((end - start).total_seconds())


def agent_of(meta: dict) -> str:
    return meta.get("agent") or "claude (legacy worker job)"


def job_view(job_id: str, detail: str = "full") -> dict:
    status, meta, ev = job_status(job_id)
    d = jobs.job_dir(job_id)
    options = meta.get("options") or {}
    view: dict[str, Any] = {
        "job_id": job_id, "agent": agent_of(meta), "status": status, "mode": meta.get("mode"),
        "cwd": meta.get("cwd"), "model": meta.get("model") or ev["model"], "elapsed_sec": _elapsed(meta, d),
        "task": (meta.get("task_preview") or "")[:200],
    }
    if meta.get("parent_job"):
        view["continues_job"] = meta["parent_job"]
    ultracode = options.get("ultracode", meta.get("ultracode")) and meta.get("mode") == "write"
    if ultracode:
        view["ultracode"] = True
    if detail == "brief":
        return view
    view["session_id"] = ev["session_id"]
    if ultracode:
        view["workflow_available"] = ev["workflow_available"]
    view["activity"] = {"events": ev["events"], "tool_uses": len(ev["tool_uses"]),
                        "last_tools": ev["tool_uses"][-6:]}
    if ev["files_edited"]:
        view["files_edited"] = ev["files_edited"]
    if ev["permission_denials"]:
        view["permission_denials"] = ev["permission_denials"][-10:]
    if ev["rate_limits"]:
        view["rate_limits"] = ev["rate_limits"]
    if status not in TERMINAL:
        if ev["texts"]:
            view["latest_message"] = ev["texts"][-1][-1500:]
        return view
    final = ev["result"] if isinstance(ev["result"], str) else (ev["texts"][-1] if ev["texts"] else "")
    if len(final) > FINAL_MESSAGE_LIMIT:
        view["final_message_truncated"] = True
        final = final[:FINAL_MESSAGE_LIMIT]
    view["final_message"] = final
    view["result"] = {"subtype": ev["subtype"], "is_error": ev["is_error"], "turns": ev["turns"],
                      "cost_usd_list_price": ev["cost_usd"], "usage": ev["usage"]}
    if status in ("failed", "lost"):
        view["stderr_tail"] = jobs.tail_text(d / "stderr.log", 2000)
    view["files"] = {"events": str(d / "stdout.log"), "stderr": str(d / "stderr.log")}
    return view


# --------------------------------------------------------------------------- adapter


class _Adapter:
    kind = PROVIDER
    TERMINAL = TERMINAL
    DEFAULT_MAX_JOBS = 2
    HAS_DEFAULT_ACCOUNT = False  # ~/.claude is the lead's own account
    TRACKS_COST = False  # its result carries the account's rate_limits instead
    OPTIONS: dict[str, tuple[Any, str]] = {
        "shell": (True, "write mode: allow shell commands (Bash, with network, as the Windows user); "
                        "false = file edits only"),
        "ultracode": (False, "write mode: multi-agent workflows (--effort ultracode, implies xhigh); costly"),
        "max_turns": (None, "optional cap on agent turns (1-500)"),
    }

    @staticmethod
    def find_exe() -> tuple[str | None, str]:
        return find_claude()

    @staticmethod
    def doctor(agent: agents.Agent, exe: str) -> dict:
        return {"auth": auth_status(exe, agent.home)}

    @staticmethod
    def validate(agent: agents.Agent, model: str | None, effort: str | None) -> str | None:
        return None  # model ids and aliases are checked by Claude Code itself

    @staticmethod
    def normalize_options(mode: str, options: dict) -> dict:
        # Ultracode only for write jobs: workflow subagents may get edit rights, which would
        # undermine a read-only job. Runs after check_options, so the value is a real bool.
        return {**options, "ultracode": options.get("ultracode") is True and mode == "write"}

    @staticmethod
    def check_options(options: dict) -> str | None:
        mt = options.get("max_turns")
        if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool) or not 1 <= mt <= 500):
            return f"max_turns must be an integer 1-500, got {mt!r}"
        for k in ("shell", "ultracode"):
            if not isinstance(options.get(k), bool):
                return f"{k} must be true or false, got {options.get(k)!r}"
        return None

    @staticmethod
    def read_usage(agent: agents.Agent, exe: str, timeout: float | None = None) -> tuple[dict, str]:
        return read_usage(agent, timeout)

    @staticmethod
    def build(agent: agents.Agent, exe: str, d: Path, meta: dict) -> tuple[list[str], dict, Any]:
        o = meta["options"]
        ultracode = bool(o.get("ultracode")) and meta["mode"] == "write"
        argv = build_argv(exe, meta["mode"], meta.get("model"), meta.get("effort"), meta.get("add_dirs"),
                          o.get("max_turns"), meta.get("resume_session"), ultracode, bool(o.get("shell", True)),
                          agent.setting("deny", DEFAULT_DENY))
        env = child_env(agent.home, ultracode, int(agent.setting("workflow_agents", DEFAULT_WORKFLOW_AGENTS)))
        return argv, env, None

    @staticmethod
    def job_status(job_id: str) -> tuple[str, dict, dict]:
        return job_status(job_id)

    @staticmethod
    def job_view(job_id: str, detail: str = "full") -> dict:
        return job_view(job_id, detail)

    @staticmethod
    def session_of(meta: dict, ev: dict) -> str | None:
        return ev.get("session_id") or meta.get("resume_session")


ADAPTER = _Adapter()
agents.register(ADAPTER)
