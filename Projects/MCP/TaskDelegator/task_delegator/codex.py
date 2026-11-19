"""Codex CLI provider: find the executable, build `codex exec` argv, read its JSONL events.

Agents: "codex" is the default account (~/.codex or the server's CODEX_HOME); "codex:<name>" is
another account whose home is %USERPROFILE%\\.codex-accounts\\<name> (passed as CODEX_HOME).
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import agents, jobs, limits, procs

PROVIDER = "codex"
EXE_NAME = "codex.exe" if os.name == "nt" else "codex"

# Our mode names -> Codex sandbox policies.
SANDBOX = {"read-only": "read-only", "write": "workspace-write"}

# Env vars that belong to Claude / Anthropic and must not leak into Codex's shell.
_SCRUB_PREFIXES = ("CLAUDE", "ANTHROPIC", "MCP_")
_SCRUB_EXACT = {"CLAUDECODE"}

FINAL_MESSAGE_LIMIT = 60_000


# --------------------------------------------------------------------------- locate


def _version_key(exe: str) -> tuple:
    try:
        return (os.path.getmtime(exe),)
    except OSError:
        return (0,)


def find_codex() -> tuple[str | None, str]:
    """Return (path, how_found). The desktop app keeps the CLI under a hashed folder
    that changes on every update, so we glob instead of hardcoding it."""
    override = os.environ.get("TASK_DELEGATOR_CODEX_EXE")
    if override:
        return (override if os.path.isfile(override) else None), "TASK_DELEGATOR_CODEX_EXE"

    local = os.environ.get("LOCALAPPDATA")
    if local:
        found = glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", EXE_NAME))
        if found:
            return max(found, key=_version_key), "Codex desktop app bin"

    on_path = shutil.which("codex")
    if on_path:
        if not procs.IS_WINDOWS or on_path.lower().endswith(".exe"):
            return on_path, "PATH"
        # npm installs put a codex.cmd shim on PATH. Launching it would route our argv
        # through cmd.exe (breaks on & ^ %) and we would track cmd.exe instead of codex.
        # Use the native binary the npm package vendors instead.
        vendored = glob.glob(os.path.join(
            os.path.dirname(on_path), "node_modules", "@openai", "codex", "vendor", "*", "codex", EXE_NAME
        ))
        if vendored:
            return max(vendored, key=_version_key), "npm package (vendored exe)"
        return None, f"only a script shim on PATH ({on_path}); set TASK_DELEGATOR_CODEX_EXE to the real codex.exe"

    return None, "not found"


@lru_cache(maxsize=8)
def _version_of(exe: str, mtime: float) -> str:
    try:
        r = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0,
        )
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"


def codex_version(exe: str) -> str:
    return _version_of(exe, _version_key(exe)[0])


def home_env(home: str | None) -> dict[str, str] | None:
    """The server's environment with CODEX_HOME pointing at an account (None = inherit as is)."""
    return {**os.environ, "CODEX_HOME": home} if home else None


def login_status(exe: str, home: str | None = None) -> str:
    try:
        r = subprocess.run(
            [exe, "login", "status"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
            stdin=subprocess.DEVNULL, env=home_env(home),
            creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0,
        )
        return (r.stdout or r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"error: {e}"


# --------------------------------------------------------------------------- models


def models_cache_path(home: str | None = None) -> Path:
    home = home or os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return Path(home) / "models_cache.json"


def available_models(home: str | None = None) -> list[dict] | None:
    """Models this Codex account can use, from Codex's own cache (None if unreadable).

    Each entry: slug, name, description, efforts (supported reasoning levels), default_effort,
    listed (shown in Codex's model picker).
    """
    data = jobs.read_json(models_cache_path(home))
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    out = []
    for m in data["models"]:
        if not isinstance(m, dict) or not m.get("slug"):
            continue
        out.append({
            "slug": m["slug"],
            "name": m.get("display_name"),
            "description": m.get("description"),
            "efforts": [lvl.get("effort") for lvl in m.get("supported_reasoning_levels") or []
                        if isinstance(lvl, dict) and lvl.get("effort")],
            "default_effort": m.get("default_reasoning_level"),
            "listed": m.get("visibility") == "list",
        })
    return out


def check_model_effort(model: str | None, effort: str | None, home: str | None = None) -> str | None:
    """Return an error message if the model/effort pair is known to be invalid, else None.

    Fails open when Codex's model cache is missing or the model is not given.
    """
    models = available_models(home)
    if not models:
        return None
    by_slug = {m["slug"]: m for m in models}
    if model is not None and model not in by_slug:
        listed = ", ".join(m["slug"] for m in models if m["listed"])
        return f"unknown Codex model {model!r}; available: {listed}"
    if effort is not None and model is not None:
        efforts = by_slug[model]["efforts"]
        if efforts and effort not in efforts:
            return f"model {model!r} does not support effort {effort!r}; supported: {', '.join(efforts)}"
    if effort is not None and model is None:
        known = {e for m in models for e in m["efforts"]}
        if known and effort not in known:
            return f"unknown effort {effort!r}; known levels: {', '.join(sorted(known))}"
    return None


# --------------------------------------------------------------------------- argv / env


def child_env(home: str | None = None) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items()
        if k.upper() not in _SCRUB_EXACT and not k.upper().startswith(_SCRUB_PREFIXES)
    }
    if home:
        env["CODEX_HOME"] = home
    env.setdefault("NO_COLOR", "1")
    return env


def _common_flags(sandbox: str, last_msg: Path, model: str | None, effort: str | None,
                  opts: dict | None = None) -> list[str]:
    opts = opts or {}
    flags = [
        "--json",
        "--skip-git-repo-check",
        "-o", str(last_msg),
        # Config overrides work for both `exec` and `exec resume` (resume has no -s).
        "-c", f'sandbox_mode="{sandbox}"',
        # Non-interactive: never block on an approval prompt nobody can answer.
        "-c", 'approval_policy="never"',
    ]
    if model:
        flags += ["-m", model]
    if effort:
        flags += ["-c", f'model_reasoning_effort="{effort}"']
    if opts.get("web_search"):
        # Live web search runs on OpenAI's side (a model tool), so it also works in read-only mode.
        flags += ["-c", 'web_search="live"']
    if opts.get("network") and sandbox == "workspace-write":
        # Network for the agent's own shell commands (curl, pip, git fetch). Write mode only.
        flags += ["-c", "sandbox_workspace_write.network_access=true"]
    return flags


def build_start_argv(exe: str, cwd: str, mode: str, last_msg: Path, model: str | None,
                     effort: str | None, add_dirs: list[str] | None, opts: dict | None = None) -> list[str]:
    argv = [exe, "exec", "-C", cwd, "-s", SANDBOX[mode]]
    argv += _common_flags(SANDBOX[mode], last_msg, model, effort, opts)
    for d in add_dirs or []:
        argv += ["--add-dir", d]
    argv.append("-")  # read the prompt from stdin (no quoting / length issues on Windows)
    return argv


def _toml_str_array(items: list[str]) -> str:
    # JSON string escapes (\\, \", \uXXXX) are valid TOML basic-string escapes.
    return "[" + ", ".join(json.dumps(s) for s in items) + "]"


def build_resume_argv(exe: str, thread_id: str, mode: str, last_msg: Path, model: str | None,
                      effort: str | None, add_dirs: list[str] | None = None,
                      opts: dict | None = None) -> list[str]:
    argv = [exe, "exec", "resume"]
    argv += _common_flags(SANDBOX[mode], last_msg, model, effort, opts)
    if mode == "write" and add_dirs:
        # `exec resume` has no --add-dir; the config key is the equivalent.
        argv += ["-c", f"sandbox_workspace_write.writable_roots={_toml_str_array(add_dirs)}"]
    argv += [thread_id, "-"]
    return argv


# --------------------------------------------------------------------------- events

_PS_WRAPPER = re.compile(r'^"?[^"]*?powershell(?:\.exe)?"?\s+-Command\s+', re.IGNORECASE)


def _short_cmd(cmd: Any, limit: int = 200) -> str:
    if isinstance(cmd, list):
        cmd = " ".join(str(c) for c in cmd)
    s = _PS_WRAPPER.sub("", str(cmd or "")).strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        s = s[1:-1]
    s = s.replace("\\\\", "\\").replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def parse_events(path: Path) -> dict:
    """Fold Codex `exec --json` events into a compact summary."""
    out: dict[str, Any] = {
        "thread_id": None,
        "turn_completed": False,
        "turn_failed": None,
        "errors": [],
        "usage": None,
        "messages": [],
        "commands": [],
        "file_changes": [],
        "failed_file_changes": [],
        "web_searches": [],
        "todo": None,
        "item_counts": {},
        "events": 0,
    }
    try:
        raw = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return out

    commands: dict[str, dict] = {}
    files: dict[str, str] = {}
    failed_files: dict[str, str] = {}
    # Split on "\n" only: str.splitlines() would also break inside JSON strings that
    # contain raw U+2028 / U+2029 / U+0085 and silently drop those events.
    for line in raw.split("\n"):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        out["events"] += 1
        et = ev.get("type")
        if et == "thread.started":
            out["thread_id"] = ev.get("thread_id")
        elif et == "turn.completed":
            out["turn_completed"] = True
            out["usage"] = ev.get("usage")
        elif et == "turn.failed":
            err = ev.get("error") or {}
            out["turn_failed"] = err.get("message") if isinstance(err, dict) else str(err)
        elif et == "error":
            out["errors"].append(ev.get("message") or json.dumps(ev, ensure_ascii=False)[:500])
        elif et in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item") or {}
            it = item.get("type", "?")
            iid = item.get("id", "")
            if et == "item.completed":
                out["item_counts"][it] = out["item_counts"].get(it, 0) + 1
            if it == "agent_message" and et == "item.completed":
                out["messages"].append(item.get("text", ""))
            elif it == "command_execution":
                commands[iid] = {
                    "command": _short_cmd(item.get("command")),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                }
            elif it == "file_change" and et == "item.completed":
                # Only a completed patch changed the disk; a failed one did not.
                target = files if item.get("status", "completed") == "completed" else failed_files
                for ch in item.get("changes") or []:
                    p = ch.get("path")
                    if p:
                        target[p] = ch.get("kind") or "update"
            elif it == "web_search" and et == "item.completed":
                q = item.get("query") or (item.get("action") or {}).get("query")
                out["web_searches"].append(q if isinstance(q, str) else json.dumps(item, ensure_ascii=False)[:200])
            elif it == "todo_list":
                out["todo"] = [
                    {"text": t.get("text"), "done": bool(t.get("completed"))}
                    for t in item.get("items") or []
                ]
            elif it == "error" and et == "item.completed":
                out["errors"].append(item.get("message", ""))
    out["commands"] = list(commands.values())
    out["file_changes"] = [{"path": p, "kind": k} for p, k in files.items()]
    out["failed_file_changes"] = [{"path": p, "kind": k} for p, k in failed_files.items()]
    return out


# --------------------------------------------------------------------------- job view


def git_status(cwd: str) -> str | None:
    if not cwd or not shutil.which("git"):
        return None
    try:
        r = subprocess.run(
            ["git", "-C", cwd, "status", "--porcelain=v1", "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, stdin=subprocess.DEVNULL,
            creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None  # not a git repo
    return r.stdout.strip()[:8000]


TERMINAL = {"completed", "failed", "cancelled", "lost"}


# An "after" reading taken this long after the job ended would mostly measure other work.
LATE_READING_SEC = 300


def _take_after_reading(meta: dict, d: Path) -> dict:
    usage = limits.read(meta.get("exe") or meta.get("codex_exe"), env=home_env(meta.get("home")))
    rec = {"snapshot": limits.snapshot(usage), "summary": limits.evaluate(usage)["summary"],
           "measured_at": jobs.now_iso()}
    jobs.write_json(d / "limits_after.json", rec)
    return rec


# One "after" reading per job: the exit hook (watcher thread) and the first look at a just-finished
# job (task_wait in another thread) race for it. Whoever comes second uses the first one's file.
_AFTER_READING_LOCK = threading.Lock()


def capture_limits_after(job_id: str) -> None:
    """jobs.launch on_exit hook: record the limits right when Codex exits."""
    d = jobs.job_dir(job_id)
    meta = jobs.load_meta(job_id)
    if not meta.get("limits_before"):
        return
    with _AFTER_READING_LOCK:
        if (d / "limits_after.json").exists():
            return
        try:
            _take_after_reading(meta, d)
        except limits.LimitsUnavailable:
            pass  # limits_cost() retries on the first look, if that is soon enough


def limits_cost(meta: dict, d: Path) -> dict | None:
    """What a finished job cost in Codex limits (percentage points per window).

    The "after" reading is normally taken by capture_limits_after() the moment the job ends.
    If it is missing (failed, or the server was not running then) it is taken on the first look,
    but only if that look comes soon after the end; later readings would include other work.
    Successful readings are cached in limits_after.json; failures are not.
    """
    before = meta.get("limits_before")
    if not before:
        return None  # started without a limits reading (or before limits existed)
    rec = jobs.read_json(d / "limits_after.json")
    if rec is None:
        finished = (jobs.read_json(d / "exit.json") or {}).get("finished_at")
        try:
            age = (datetime.now().astimezone() - datetime.fromisoformat(finished)).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is None or age > LATE_READING_SEC:
            return {"cost": "unknown",
                    "note": "no limits reading was taken when the job ended; a reading now would "
                            "include later Codex use"}
        with _AFTER_READING_LOCK:
            rec = jobs.read_json(d / "limits_after.json")  # the exit hook may have just taken it
            if rec is None:
                try:
                    rec = _take_after_reading(meta, d)
                except limits.LimitsUnavailable as e:
                    return {"cost": "unknown", "error": str(e)}
    return {
        "cost": limits.cost(before, rec["snapshot"]),
        "after": rec["summary"],
        "note": "approximate: includes any other Codex use while the job ran",
    }


def job_status(job_id: str) -> tuple[str, dict, dict]:
    """Return (status, meta, events_summary)."""
    meta = jobs.load_meta(job_id)
    d = jobs.job_dir(job_id)
    running = jobs.is_running(job_id, meta)
    exit_info = None
    if not running:
        exit_info = jobs.read_json(d / "exit.json")
        if exit_info is None and meta.get("pid"):
            # Ended while no server was watching (e.g. Claude restarted): record it once.
            exit_info = jobs.finalize_unobserved(job_id)
    # Parse after the liveness check so a just-finished job's last events are included.
    ev = parse_events(d / "stdout.log")

    if meta.get("cancel_requested"):
        status = "cancelling" if running else "cancelled"
    elif running:
        status = "running"
    elif exit_info is None:
        status = "failed"  # never launched
    elif exit_info.get("unobserved"):
        # Exit code unknown: judge by what Codex itself reported.
        status = "completed" if ev["turn_completed"] and not ev["turn_failed"] else (
            "failed" if ev["turn_failed"] else "lost")
    else:
        ok = exit_info.get("exit_code") == 0 and ev["turn_completed"] and not ev["turn_failed"]
        status = "completed" if ok else "failed"
    return status, meta, ev


def _elapsed(meta: dict, d: Path) -> int | None:
    from datetime import datetime

    try:
        start = datetime.fromisoformat(meta["started_at"])
    except (KeyError, ValueError):
        return None
    end_info = jobs.read_json(d / "exit.json") or {}
    try:
        end = datetime.fromisoformat(end_info["finished_at"])
    except (KeyError, ValueError):
        end = datetime.now().astimezone()
    return int((end - start).total_seconds())


def job_view(job_id: str, detail: str = "summary") -> dict:
    """detail: 'brief' (one line for listings), 'summary' (progress), 'full' (with final answer)."""
    status, meta, ev = job_status(job_id)
    d = jobs.job_dir(job_id)
    view: dict[str, Any] = {
        "job_id": job_id,
        "agent": meta.get("agent") or PROVIDER,
        "status": status,
        "mode": meta.get("mode"),
        "cwd": meta.get("cwd"),
        "elapsed_sec": _elapsed(meta, d),
        "task": (meta.get("task_preview") or "")[:200],
    }
    if meta.get("parent_job"):
        view["continues_job"] = meta["parent_job"]
    if detail == "brief":
        return view

    view["session_id"] = ev["thread_id"]
    view["activity"] = {
        "events": ev["events"],
        "items": ev["item_counts"],
        "commands_run": len(ev["commands"]),
        "last_commands": ev["commands"][-5:],
    }
    if ev["web_searches"]:
        view["activity"]["web_searches"] = ev["web_searches"][-10:]
    if ev["todo"]:
        view["plan"] = ev["todo"]
    if ev["file_changes"]:
        view["files_changed"] = ev["file_changes"]
    if ev["failed_file_changes"]:
        view["failed_file_changes"] = ev["failed_file_changes"]
    if ev["errors"] or ev["turn_failed"]:
        view["errors"] = ([ev["turn_failed"]] if ev["turn_failed"] else []) + ev["errors"][-5:]

    if status not in TERMINAL:
        if ev["messages"]:
            view["latest_message"] = ev["messages"][-1][-1500:]
        return view

    # Terminal: attach the answer and accounting.
    view["usage"] = ev["usage"]
    final = ""
    try:
        final = (d / "last_message.txt").read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        final = ev["messages"][-1] if ev["messages"] else ""
    if len(final) > FINAL_MESSAGE_LIMIT:
        view["final_message_truncated"] = True
        final = final[:FINAL_MESSAGE_LIMIT]
    view["final_message"] = final
    if meta.get("mode") == "write":
        gs = git_status(meta.get("cwd", ""))
        if gs is not None:
            view["git_status_after"] = gs or "(clean)"
    if status in ("failed", "lost"):
        view["stderr_tail"] = jobs.tail_text(d / "stderr.log", 2000)
    lc = limits_cost(meta, d)
    if lc is not None:
        view["limits_cost"] = lc
    view["files"] = {
        "events": str(d / "stdout.log"),
        "final_message": str(d / "last_message.txt"),
        "stderr": str(d / "stderr.log"),
    }
    return view


# --------------------------------------------------------------------------- adapter


class _Adapter:
    kind = PROVIDER
    TERMINAL = TERMINAL
    DEFAULT_MAX_JOBS = 3
    HAS_DEFAULT_ACCOUNT = True
    TRACKS_COST = True  # a reading before and after a job gives limits_cost
    OPTIONS: dict[str, tuple[Any, str]] = {
        "web_search": (False, "let Codex search the live web (docs, release notes, error messages); "
                              "works in read-only mode too. Its shell has no network either way."),
    }

    @staticmethod
    def find_exe() -> tuple[str | None, str]:
        return find_codex()

    @staticmethod
    def doctor(agent: agents.Agent, exe: str) -> dict:
        models = available_models(agent.home)
        return {
            "version": codex_version(exe),
            "login": login_status(exe, agent.home),
            "models": ([{k: m[k] for k in ("slug", "description", "efforts")} for m in models if m["listed"]]
                       if models else f"unavailable (no {models_cache_path(agent.home)})"),
        }

    @staticmethod
    def validate(agent: agents.Agent, model: str | None, effort: str | None) -> str | None:
        return check_model_effort(model, effort, agent.home)

    @staticmethod
    def check_options(options: dict) -> str | None:
        if not isinstance(options.get("web_search"), bool):
            return f"web_search must be true or false, got {options.get('web_search')!r}"
        return None

    @staticmethod
    def read_usage(agent: agents.Agent, exe: str, timeout: float | None = None) -> tuple[dict, str]:
        """The usage script already speaks schema 1. Raises limits.LimitsUnavailable."""
        kw = {} if timeout is None else {"script_timeout": timeout}
        return limits.read(exe, env=home_env(agent.home), **kw), "Codex usage script"

    @staticmethod
    def build(agent: agents.Agent, exe: str, d: Path, meta: dict) -> tuple[list[str], dict, Any]:
        last_msg = d / "last_message.txt"
        # Shell network ("network") is deliberately not an option: on Windows the sandbox needs
        # Codex's experimental network proxy, and the plain network_access switch has no effect.
        opts = {"web_search": bool(meta["options"].get("web_search"))}
        if meta.get("resume_session"):
            argv = build_resume_argv(exe, meta["resume_session"], meta["mode"], last_msg, meta.get("model"),
                                     meta.get("effort"), meta.get("add_dirs"), opts)
        else:
            argv = build_start_argv(exe, meta["cwd"], meta["mode"], last_msg, meta.get("model"),
                                    meta.get("effort"), meta.get("add_dirs"), opts)
        # Take the "after" limits reading the moment Codex exits, not whenever someone looks.
        on_exit = capture_limits_after if meta.get("limits_before") else None
        return argv, child_env(agent.home), on_exit

    @staticmethod
    def job_status(job_id: str) -> tuple[str, dict, dict]:
        return job_status(job_id)

    @staticmethod
    def job_view(job_id: str, detail: str = "full") -> dict:
        return job_view(job_id, detail)

    @staticmethod
    def session_of(meta: dict, ev: dict) -> str | None:
        return ev.get("thread_id") or meta.get("resume_session") or meta.get("resume_thread")


ADAPTER = _Adapter()
agents.register(ADAPTER)
