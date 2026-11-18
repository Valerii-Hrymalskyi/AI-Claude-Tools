"""Worker B: a second Claude Code account, run headless (`claude -p`) like Codex.

The second account lives in its own config dir (CLAUDE_CONFIG_DIR): its own login, settings,
history and user-level CLAUDE.md. It runs under the user's Windows account, so it has the same
file rights as the lead Claude session. task-delegator is not registered in that config, so B cannot
delegate further.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from . import jobs, procs

PROVIDER = "worker"
EXE_NAME = "claude.exe" if os.name == "nt" else "claude"
FINAL_MESSAGE_LIMIT = 60_000

# The user's preference for development work: Opus 5.5, extra-high effort, Ultracode on.
DEFAULT_MODEL = os.environ.get("TASK_DELEGATOR_WORKER_MODEL", "claude-opus-5-5")
DEFAULT_EFFORT = os.environ.get("TASK_DELEGATOR_WORKER_EFFORT", "xhigh")
DEFAULT_ULTRACODE = os.environ.get("TASK_DELEGATOR_WORKER_ULTRACODE", "1") not in ("0", "false", "no")

# Tools B may use. In read-only mode nothing that can change files or run commands.
READ_TOOLS = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "TodoWrite"]
EDIT_EXTRA = ["Edit", "Write", "MultiEdit", "NotebookEdit"]
WRITE_EXTRA = [*EDIT_EXTRA, "Bash"]
SHELL_TOOLS = ["Bash", "PowerShell"]
READ_ONLY_DENY = [*EDIT_EXTRA, *SHELL_TOOLS]
EDIT_TOOLS = set(EDIT_EXTRA)

# Permission rules denied to write jobs that do have a shell (semicolon-separated, "" = none).
# A guard against accidents, not a sandbox: B still runs with the user's Windows rights.
WRITE_DENY = [r.strip() for r in os.environ.get("TASK_DELEGATOR_WORKER_DENY", "Bash(git push:*)").split(";")
              if r.strip()]

# Inherited env vars that must not reach B: they belong to the lead session or its account.
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC", "MCP_")
_KEEP = {"CLAUDE_CODE_GIT_BASH_PATH"}


def config_dir() -> Path:
    return Path(os.environ.get("TASK_DELEGATOR_WORKER_CONFIG_DIR")
                or Path.home() / ".claude-worker-b")


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


# Ultracode needs workflows, which are off by default on a Pro plan. Passed per run with
# --settings so the second account's own settings.json stays untouched.
WORKFLOW_SETTINGS = Path(__file__).resolve().parent / "worker_settings.json"


def child_env(ultracode: bool = False) -> dict[str, str]:
    # Scrubbing CLAUDE* also drops CLAUDE_CODE_EFFORT_LEVEL, which would override --effort.
    env = {k: v for k, v in os.environ.items()
           if k.upper() in _KEEP or not k.upper().startswith(_SCRUB_PREFIXES)}
    env["CLAUDE_CONFIG_DIR"] = str(config_dir())
    if ultracode:
        # Caps concurrent workflow agents only (not their total): keeps a Pro plan's window alive.
        env["CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"] = os.environ.get("TASK_DELEGATOR_WORKER_WORKFLOW_AGENTS", "3")
    env.setdefault("NO_COLOR", "1")
    return env


def auth_status(exe: str) -> dict:
    """`claude auth status` for the worker account. Never includes the email in the result."""
    try:
        r = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, stdin=subprocess.DEVNULL, env=child_env(),
                           creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0)
        data = json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return {"loggedIn": None, "error": str(e)[:200]}
    return {k: data.get(k) for k in ("loggedIn", "authMethod", "subscriptionType", "loginExpiresAt")
            if k in data}


def build_argv(exe: str, mode: str, model: str | None, effort: str | None,
               add_dirs: list[str] | None, max_turns: int | None, resume_session: str | None,
               ultracode: bool = False, shell: bool = True) -> list[str]:
    """ultracode (write mode only): `--effort ultracode` turns it on for the run and implies xhigh.
    The "ultracode" keyword inside a -p prompt does nothing: it only triggers on human-typed prompts.
    shell (write mode only): False leaves B file edits only, no Bash/PowerShell."""
    ultracode = ultracode and mode == "write"
    argv = [exe, "-p", "--output-format", "stream-json", "--verbose"]
    if mode == "write":
        # acceptEdits: edits are auto-approved only inside the working dirs (cwd + --add-dir);
        # anything else that would need a prompt is denied in headless mode.
        tools = [*READ_TOOLS, *(WRITE_EXTRA if shell else EDIT_EXTRA)]
        argv += ["--permission-mode", "acceptEdits", "--allowedTools", *tools]
        deny = WRITE_DENY if shell else SHELL_TOOLS  # deny rules win over allow rules
        if deny:
            argv += ["--disallowedTools", *deny]
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


TERMINAL = {"completed", "failed", "cancelled", "lost"}


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


def job_view(job_id: str, detail: str = "full") -> dict:
    status, meta, ev = job_status(job_id)
    d = jobs.job_dir(job_id)
    view: dict[str, Any] = {
        "job_id": job_id, "status": status, "mode": meta.get("mode"), "cwd": meta.get("cwd"),
        "model": meta.get("model") or ev["model"], "elapsed_sec": _elapsed(meta, d),
        "task": (meta.get("task_preview") or "")[:200],
    }
    if meta.get("parent_job"):
        view["continues_job"] = meta["parent_job"]
    if meta.get("ultracode"):
        view["ultracode"] = True
    if detail == "brief":
        return view
    view["session_id"] = ev["session_id"]
    if meta.get("ultracode"):
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
