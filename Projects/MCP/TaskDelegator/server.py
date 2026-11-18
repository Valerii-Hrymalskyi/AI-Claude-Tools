"""TaskDelegator MCP server (stdio), registered in Claude Code as "task-delegator".

Exposes other AI agents as tools for Claude: Codex CLI and worker B (a second Claude Code account).
Run with the project's portable Python:

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe server.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import Field  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402  (message reaches the model)

from task_delegator import __version__, claude_worker, codex, jobs, limits  # noqa: E402


def _status(job_id: str) -> tuple[str, dict, dict]:
    try:
        return codex.job_status(job_id)
    except (KeyError, ValueError) as e:
        raise ToolError(f"unknown job id {job_id!r}; call codex_status() to list jobs") from e


def _view(job_id: str, detail: str = "full") -> dict:
    _status(job_id)  # validates the id with a readable error
    return codex.job_view(job_id, detail)

MAX_RUNNING = int(os.environ.get("TASK_DELEGATOR_CODEX_MAX_JOBS", "3"))
KEEP_DAYS = int(os.environ.get("TASK_DELEGATOR_KEEP_DAYS", "14"))
WAIT_MAX_SEC = 600
POLL_SEC = 2.0

INSTRUCTIONS = """\
TaskDelegator lets you (Claude) delegate work to other AI agents running on this PC: OpenAI Codex
(codex_* tools) and worker B, headless Claude Code on a second Claude account (worker_* tools).

Treat Codex as a capable subordinate, not an oracle:
- Give it a complete, self-contained brief: goal, relevant paths, constraints, what "done" looks like,
  and what to return. It does not see this conversation.
- Default to mode="read-only" (analysis, review, second opinion, research in the codebase).
  Use mode="write" only when you deliberately hand it an implementation task in `cwd`,
  and avoid editing the same files yourself while it runs.
- Its answer is a claim, not a fact: check its diff / key statements before reporting them as done.
- Jobs run in the background: codex_start returns a job_id immediately; do other work, then
  codex_wait (blocks up to timeout_sec) or codex_status. Use codex_reply to continue the same
  Codex session with follow-up questions or corrections.

Codex has rate limits (a 5-hour and a weekly window). Plan around them:
- Before handing out a large or multi-step job, call codex_limits. If a window is in "warn",
  prefer smaller, well-scoped jobs and keep the most important work for Codex; if "pause",
  do the work yourself or tell the user when the window resets.
- codex_start / codex_reply refuse to start in "pause" and add a warning in "warn". Pass
  ignore_limits=true only when the user explicitly wants to spend the remaining quota.
- A finished job reports limits_cost (approximate percentage points per window); use it to
  estimate what similar jobs will cost.

Model and effort: always pass both explicitly. Otherwise Codex falls back to the model and effort
of the user's Codex config, which may be slow or expensive. Pick the cheapest pair that fits:
- simple question / mechanical edit with exact instructions: a small fast model, low
- ordinary well-scoped task (a test, small feature, known bug, small review): a general model, low-medium
- multi-file change, unclear bug, large review, architecture: a general model, high
- hardest, high-stakes work: the strongest model, high-xhigh
- max / ultra only when the user asks; in "warn" stay at medium or below and avoid the strongest model.
delegator_doctor lists the models with their descriptions and the efforts each supports. If the user
has a delegation policy (e.g. in their CLAUDE.md), it takes precedence over these defaults.

Two write jobs in overlapping folders can overwrite each other's edits: a start result with a
"warning" names the other running write job. Split the files between them or wait for it.
"""

mcp = MCPServer(name="task-delegator", version=__version__, instructions=INSTRUCTIONS)

Mode = Literal["read-only", "write"]


def _running_count() -> int:
    n = 0
    for jid in jobs.list_job_ids(codex.PROVIDER, limit=50):
        try:
            if codex.job_status(jid)[0] in ("running", "cancelling"):
                n += 1
        except (KeyError, ValueError):
            pass
    return n


def _check_cwd(cwd: str, mode: str) -> str:
    p = Path(cwd).expanduser()
    if not p.is_absolute():
        raise ToolError(f"cwd must be an absolute path, got {cwd!r}")
    if not p.is_dir():
        raise ToolError(f"cwd does not exist or is not a directory: {cwd}")
    p = p.resolve()
    if mode == "write" and (p == Path(p.anchor) or p == Path.home().resolve()):
        raise ToolError(f"refusing write mode on a drive root or home folder: {p}")
    return str(p)


_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


def _check_token(name: str, value: str | None) -> None:
    # model / effort end up inside `-c key="value"` TOML overrides.
    if value is not None and not _SAFE_TOKEN.match(value):
        raise ToolError(f"invalid {name}: {value!r}")


def _limits_gate(exe: str, ignore_limits: bool) -> tuple[dict, dict | None]:
    """Check Codex limits before a start. Returns (info for the caller, snapshot for the job).

    Fails open: if the usage script is unavailable the job still starts, and the caller is told.
    """
    try:
        usage = limits.read(exe)
    except limits.LimitsUnavailable as e:
        return {"state": "unknown", "note": f"limits unavailable, started without a check: {e}"}, None
    verdict = limits.evaluate(usage)
    info = {"state": verdict["state"], "summary": verdict["summary"]}
    if verdict["reasons"]:
        info["reasons"] = verdict["reasons"]
    if verdict["state"] == "pause":
        if not ignore_limits:
            when = verdict.get("resume_after")
            raise ToolError(
                "Codex limits are exhausted: " + "; ".join(verdict["reasons"])
                + (f". The window resets at {when}." if when else ".")
                + " Do the work yourself, wait for the reset, or pass ignore_limits=true"
                  " if the user explicitly wants to spend what is left."
            )
        info["note"] = "started despite the pause threshold (ignore_limits=true)"
        info["overridden"] = True
    elif verdict["state"] == "warn":
        info["note"] = "close to the limit: keep jobs small and important"
    return info, limits.snapshot(usage)


def _prepare(model: str | None, effort: str | None) -> str:
    """Cheap validation shared by every start path. Returns the codex.exe path."""
    _check_token("model", model)
    _check_token("effort", effort)
    problem = codex.check_model_effort(model, effort)
    if problem:
        raise ToolError(problem + " (see delegator_doctor for the model list)")
    exe, how = codex.find_codex()
    if not exe:
        raise ToolError(f"Codex CLI not available: {how}")
    return exe


def _check_capacity() -> None:
    running = _running_count()
    if running >= MAX_RUNNING:
        raise ToolError(
            f"{running} Codex jobs already running (limit {MAX_RUNNING}); wait for or cancel one first"
        )


def _write_conflicts(roots: list[str]) -> list[str]:
    """Running write jobs (Codex or worker B) whose cwd/add_dirs overlap these folders.

    Only a warning: parallel jobs in one repository are fine as long as they edit different files,
    which the server cannot know in advance.
    """
    def norm(p: str) -> Path:
        # add_dirs are stored as given: fold "..", "." and separators before comparing.
        return Path(os.path.abspath(p))

    mine = [norm(r) for r in roots if r]
    found = []
    for provider in (codex.PROVIDER, claude_worker.PROVIDER):
        for jid in jobs.list_job_ids(provider, limit=50):
            try:
                meta = jobs.load_meta(jid)
            except (KeyError, ValueError, OSError):
                continue
            if meta.get("mode") != "write" or meta.get("cancel_requested") or not jobs.is_running(jid, meta):
                continue
            theirs = [norm(p) for p in [meta.get("cwd"), *(meta.get("add_dirs") or [])] if p]
            # WindowsPath comparisons are case-insensitive; a parent and its subfolder overlap too.
            if any(a == b or a.is_relative_to(b) or b.is_relative_to(a) for a in mine for b in theirs):
                found.append(jid)
    return found


def _conflict_warning(mode: str, cwd: str, add_dirs: list[str] | None) -> str | None:
    if mode != "write":
        return None
    others = _write_conflicts([cwd, *(add_dirs or [])])
    if not others:
        return None
    return (f"other write job(s) running in an overlapping folder: {', '.join(others)}. "
            "Make sure they do not edit the same files (and do not edit them yourself meanwhile).")


def _check_session_free(thread: str | None) -> None:
    busy = _job_running_on_thread(thread) if thread else None
    if busy:
        raise ToolError(f"Codex session {thread} is already busy in job {busy}; wait for it first")


def _launch(prompt: str, cwd: str, mode: str, model: str | None, effort: str | None,
            add_dirs: list[str] | None, resume_thread: str | None, parent_job: str | None,
            ignore_limits: bool = False, opts: dict | None = None) -> dict:
    """Synchronous start path, for scripts (scripts/codex_job.py). The MCP tools use _launch_async."""
    exe = _prepare(model, effort)
    _check_capacity()
    limits_info, limits_before = _limits_gate(exe, ignore_limits)
    _check_capacity()
    _check_session_free(resume_thread)
    return _create_and_launch(prompt, cwd, mode, model, effort, add_dirs, resume_thread, parent_job,
                              exe, limits_info, limits_before, opts)


async def _launch_async(prompt: str, cwd: str, mode: str, model: str | None, effort: str | None,
                        add_dirs: list[str] | None, resume_thread: str | None, parent_job: str | None,
                        ignore_limits: bool, opts: dict | None = None) -> dict:
    exe = _prepare(model, effort)
    _check_capacity()  # fail fast, before spending a second on the limits read
    # Only the slow subprocess runs off the loop. If the call is cancelled here, it raises at
    # this await and nothing has been created or launched yet.
    limits_info, limits_before = await asyncio.to_thread(_limits_gate, exe, ignore_limits)
    # Back on the event loop: no await from here until the job exists, so concurrent tool calls
    # cannot slip between these checks and the job becoming visible to them.
    _check_capacity()
    _check_session_free(resume_thread)
    return _create_and_launch(prompt, cwd, mode, model, effort, add_dirs, resume_thread, parent_job,
                              exe, limits_info, limits_before, opts)


def _create_and_launch(prompt: str, cwd: str, mode: str, model: str | None, effort: str | None,
                       add_dirs: list[str] | None, resume_thread: str | None, parent_job: str | None,
                       exe: str, limits_info: dict, limits_before: dict | None,
                       opts: dict | None = None) -> dict:
    opts = {"web_search": bool((opts or {}).get("web_search")), "network": bool((opts or {}).get("network"))}
    warning = _conflict_warning(mode, cwd, add_dirs)  # before create_job: the new job must not see itself
    job_id, d = jobs.create_job(codex.PROVIDER, prompt, {
        "mode": mode, "cwd": cwd, "model": model, "effort": effort, "add_dirs": add_dirs or [],
        "parent_job": parent_job, "resume_thread": resume_thread,
        "task_preview": " ".join(prompt.split())[:300], "codex_exe": exe,
        "limits_before": limits_before, "limits_overridden": bool(limits_info.get("overridden")),
        **opts,
    })
    last_msg = d / "last_message.txt"
    if resume_thread:
        argv = codex.build_resume_argv(exe, resume_thread, mode, last_msg, model, effort, add_dirs, opts)
    else:
        argv = codex.build_start_argv(exe, cwd, mode, last_msg, model, effort, add_dirs, opts)
    try:
        # Take the "after" limits reading the moment Codex exits, not whenever someone looks.
        jobs.launch(job_id, argv, cwd, codex.child_env(),
                    on_exit=codex.capture_limits_after if limits_before else None)
    except OSError as e:
        jobs.write_json(d / "exit.json", {"exit_code": -1, "finished_at": jobs.now_iso(), "error": str(e)})
        raise ToolError(f"failed to start Codex: {e}") from e
    result = {"job_id": job_id, "status": "running", "mode": mode, "cwd": cwd, "limits": limits_info,
              "hint": "call codex_wait(job_id) when you are ready for the result"}
    if opts["web_search"]:
        result["web_search"] = True
    if warning:
        result["warning"] = warning
    return result


@mcp.tool()
async def codex_start(
    task: Annotated[str, Field(description="Self-contained brief for Codex: goal, paths, constraints, "
                                           "definition of done, what to return. It does not see this chat.")],
    cwd: Annotated[str, Field(description="Absolute path of the folder Codex works in (its workspace root).")],
    mode: Annotated[Mode, Field(description="read-only: may read and run read-only commands. "
                                            "write: may also edit files inside cwd (and add_dirs).")] = "read-only",
    model: Annotated[str | None, Field(description="Codex model, e.g. gpt-6-luna | gpt-6.1-sol | gpt-6-astra "
                                                   "(see delegator_doctor). Pass it explicitly.")] = None,
    effort: Annotated[str | None, Field(description="Reasoning effort: low | medium | high | xhigh "
                                                    "(max/ultra only on the user's request). Pass it explicitly.")] = None,
    add_dirs: Annotated[list[str] | None, Field(description="Extra absolute folders Codex may write to (write mode).")] = None,
    web_search: Annotated[bool, Field(description="Let Codex search the live web (docs, release notes, "
                                                  "error messages). Works in read-only mode too. Codex's "
                                                  "shell has no network on this machine either way.")] = False,
    ignore_limits: Annotated[bool, Field(description="Start even if Codex limits are past the pause "
                                                     "threshold. Only when the user explicitly asks.")] = False,
) -> dict:
    """Start a Codex job in the background and return its job_id immediately (checks Codex limits first)."""
    cwd = _check_cwd(cwd, mode)
    for extra in add_dirs or []:
        _check_cwd(extra, mode)
    # Shell network ("network" opt) is deliberately not exposed: on Windows the sandbox needs
    # Codex's experimental network proxy, and the plain network_access switch has no effect.
    return await _launch_async(task, cwd, mode, model, effort, add_dirs, None, None, ignore_limits,
                               {"web_search": web_search})


@mcp.tool()
async def codex_reply(
    job_id: Annotated[str, Field(description="A finished Codex job whose session should be continued.")],
    message: Annotated[str, Field(description="Follow-up instruction or question for the same Codex session.")],
    mode: Annotated[Mode | None, Field(description="Sandbox for this turn; defaults to the original job's mode.")] = None,
    web_search: Annotated[bool | None, Field(description="Live web search for this turn; "
                                                         "defaults to the original job's setting.")] = None,
    ignore_limits: Annotated[bool, Field(description="Start even if Codex limits are past the pause "
                                                     "threshold. Only when the user explicitly asks.")] = False,
) -> dict:
    """Continue an earlier Codex session (it keeps its full context). Starts a new background job."""
    status, meta, ev = _status(job_id)
    if status in ("running", "cancelling"):
        raise ToolError(f"job {job_id} is still {status}; wait for it first")
    thread = ev["thread_id"] or meta.get("resume_thread")
    if not thread:
        raise ToolError(f"job {job_id} has no Codex session id to resume")
    _check_session_free(thread)  # fail fast; re-checked right before the launch
    mode = mode or meta.get("mode") or "read-only"
    cwd = _check_cwd(meta["cwd"], mode)
    add_dirs = meta.get("add_dirs") or []
    for extra in add_dirs:
        _check_cwd(extra, mode)
    opts = {"web_search": meta.get("web_search", False) if web_search is None else web_search}
    return await _launch_async(message, cwd, mode, meta.get("model"), meta.get("effort"),
                               add_dirs, thread, job_id, ignore_limits, opts)


@mcp.tool()
async def codex_limits() -> dict:
    """Current Codex rate-limit usage (5-hour and weekly windows) with an ok/warn/pause verdict.

    Read-only and free: it does not spend any quota. Use it to plan before delegating.
    """
    exe, _how = codex.find_codex()
    try:
        usage = await asyncio.to_thread(limits.read, exe)
    except limits.LimitsUnavailable as e:
        raise ToolError(f"Codex limits unavailable: {e}") from e
    verdict = limits.evaluate(usage)
    verdict["thresholds"] = {
        "5h (primary)": limits.thresholds()["primary"],
        "7d (secondary)": limits.thresholds()["secondary"],
    }
    return verdict


def _job_running_on_thread(thread: str) -> str | None:
    """A running job that already drives this Codex session (two resumes would race)."""
    for jid in jobs.list_job_ids(codex.PROVIDER, limit=50):
        try:
            status, meta, ev = codex.job_status(jid)
        except (KeyError, ValueError):
            continue
        if status in ("running", "cancelling") and thread in (meta.get("resume_thread"), ev["thread_id"]):
            return jid
    return None


@mcp.tool()
async def codex_wait(
    job_id: Annotated[str, Field(description="Job to wait for.")],
    timeout_sec: Annotated[int, Field(ge=0, le=WAIT_MAX_SEC, description="Max seconds to block.")] = 240,
) -> dict:
    """Block until the job finishes or timeout_sec passes. Returns progress, or the final answer when done."""
    deadline = time.monotonic() + timeout_sec
    while True:
        status = _status(job_id)[0]
        if status in codex.TERMINAL or time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_SEC)
    view = await asyncio.to_thread(_view, job_id)  # may run `git status`
    if view["status"] not in codex.TERMINAL:
        view["note"] = "still running; call codex_wait again or do other work meanwhile"
    return view


@mcp.tool()
async def codex_status(
    job_id: Annotated[str | None, Field(description="Job to inspect; omit to list recent jobs.")] = None,
    limit: Annotated[int, Field(ge=1, le=50, description="How many recent jobs to list.")] = 10,
) -> dict:
    """Non-blocking: one job's progress (or final answer), or a list of recent jobs."""
    if job_id:
        return await asyncio.to_thread(_view, job_id)
    ids = jobs.list_job_ids(codex.PROVIDER, limit)
    return {"jobs": await asyncio.to_thread(lambda: [codex.job_view(j, "brief") for j in ids])}


@mcp.tool()
async def codex_cancel(job_id: Annotated[str, Field(description="Job to stop.")]) -> dict:
    """Stop a running Codex job (kills its process tree). Files it already changed stay changed."""
    status = _status(job_id)[0]
    if status in codex.TERMINAL:
        return {"job_id": job_id, "status": status, "note": "already finished"}
    ok, msg = jobs.cancel(job_id)
    await asyncio.sleep(1)
    return {"job_id": job_id, "kill_ok": ok, "kill_output": msg, "status": _status(job_id)[0]}


@mcp.tool()
async def delegator_doctor() -> dict:
    """Health check: which agents are available, versions, login state, where job files live."""
    exe, how = codex.find_codex()
    info = {
        "delegator_version": __version__,
        "python": sys.version.split()[0],
        "state_dir": str(jobs.STATE_ROOT),
        "codex": {"exe": exe, "found_via": how},
        "limits": {"max_running_codex_jobs": MAX_RUNNING, "keep_days": KEEP_DAYS},
        "codex_usage": {
            "script": limits.usage_script(),
            "exists": Path(limits.usage_script()).is_file(),
            "thresholds": limits.thresholds(),
        },
    }
    if exe:
        info["codex"]["version"] = await asyncio.to_thread(codex.codex_version, exe)
        info["codex"]["login"] = await asyncio.to_thread(codex.login_status, exe)
    wexe, whow = claude_worker.find_claude()
    info["worker_b"] = {"exe": wexe, "found_via": whow, "config_dir": str(claude_worker.config_dir()),
                        "max_running_jobs": WORKER_MAX_RUNNING}
    if wexe:
        info["worker_b"]["auth"] = await asyncio.to_thread(claude_worker.auth_status, wexe)
    models = codex.available_models()
    info["codex"]["models"] = (
        [{k: m[k] for k in ("slug", "description", "efforts")} for m in models if m["listed"]]
        if models else f"unavailable (no {codex.models_cache_path()})"
    )
    return info


# =========================================================================== worker B

WORKER_MAX_RUNNING = int(os.environ.get("TASK_DELEGATOR_WORKER_MAX_JOBS", "2"))


def _w_status(job_id: str) -> tuple[str, dict, dict]:
    if not job_id.startswith(claude_worker.PROVIDER + "-"):
        raise ToolError(f"{job_id!r} is not a worker job; use codex_* tools for Codex jobs")
    try:
        return claude_worker.job_status(job_id)
    except (KeyError, ValueError) as e:
        raise ToolError(f"unknown job id {job_id!r}; call worker_status() to list jobs") from e


def _w_launch(prompt: str, cwd: str, mode: str, model: str | None, effort: str | None,
              add_dirs: list[str] | None, max_turns: int | None, resume_session: str | None,
              parent_job: str | None, ultracode: bool | None = None, shell: bool = True) -> dict:
    model = model or claude_worker.DEFAULT_MODEL
    effort = effort or claude_worker.DEFAULT_EFFORT
    # Ultracode only for write jobs: workflow subagents may get edit rights, which would
    # undermine a read-only job.
    ultracode = (claude_worker.DEFAULT_ULTRACODE if ultracode is None else ultracode) and mode == "write"
    _check_token("model", model)
    _check_token("effort", effort)
    exe, how = claude_worker.find_claude()
    if not exe:
        raise ToolError(f"Claude Code CLI not available: {how}")
    running = 0
    for jid in jobs.list_job_ids(claude_worker.PROVIDER, limit=50):
        try:
            st = claude_worker.job_status(jid)
        except (KeyError, ValueError):
            continue
        if st[0] in ("running", "cancelling"):
            running += 1
            if resume_session and resume_session in (st[1].get("resume_session"), st[2]["session_id"]):
                raise ToolError(f"worker session {resume_session} is already busy in job {jid}")
    if running >= WORKER_MAX_RUNNING:
        raise ToolError(f"{running} worker jobs already running (limit {WORKER_MAX_RUNNING})")
    warning = _conflict_warning(mode, cwd, add_dirs)  # before create_job: the new job must not see itself
    job_id, d = jobs.create_job(claude_worker.PROVIDER, prompt, {
        "mode": mode, "cwd": cwd, "model": model, "effort": effort, "add_dirs": add_dirs or [],
        "max_turns": max_turns, "resume_session": resume_session, "parent_job": parent_job,
        "task_preview": " ".join(prompt.split())[:300], "claude_exe": exe,
        "config_dir": str(claude_worker.config_dir()), "ultracode": ultracode, "shell": shell,
    })
    argv = claude_worker.build_argv(exe, mode, model, effort, add_dirs, max_turns, resume_session, ultracode,
                                    shell)
    try:
        jobs.launch(job_id, argv, cwd, claude_worker.child_env(ultracode))
    except OSError as e:
        jobs.write_json(d / "exit.json", {"exit_code": -1, "finished_at": jobs.now_iso(), "error": str(e)})
        raise ToolError(f"failed to start worker B: {e}") from e
    result = {"job_id": job_id, "status": "running", "mode": mode, "cwd": cwd, "model": model,
              "effort": "ultracode (xhigh)" if ultracode else effort,
              "hint": "call worker_wait(job_id) when you are ready for the result"}
    if mode == "write":
        result["shell"] = shell
    if warning:
        result["warning"] = warning
    return result


@mcp.tool()
async def worker_start(
    task: Annotated[str, Field(description="Self-contained brief for worker B (a second Claude Code account): "
                                           "goal, paths, constraints, definition of done, what to return.")],
    cwd: Annotated[str, Field(description="Absolute path of the folder B works in.")],
    mode: Annotated[Mode, Field(description="read-only: read/search files and the web, no edits, no shell. "
                                            "write: may edit files in cwd/add_dirs and (unless shell=false) "
                                            "run shell commands with network, as the Windows user.")] = "read-only",
    model: Annotated[str | None, Field(description="Claude model id or alias. "
                                                   f"Default: {claude_worker.DEFAULT_MODEL}.")] = None,
    effort: Annotated[str | None, Field(description="low | medium | high | xhigh | max. "
                                                    f"Default: {claude_worker.DEFAULT_EFFORT}. "
                                                    "Ignored when ultracode is on (it implies xhigh).")] = None,
    ultracode: Annotated[bool | None, Field(description="Ultracode (multi-agent workflows, max 3 concurrent; "
                                                        "costly). Default: "
                                                        + ("on" if claude_worker.DEFAULT_ULTRACODE else "off")
                                                        + " for write jobs; never for read-only.")] = None,
    shell: Annotated[bool, Field(description="Write mode only: allow shell commands (Bash, with network). "
                                             "false = file edits only. Some commands are always denied "
                                             f"({'; '.join(claude_worker.WRITE_DENY) or 'none'}).")] = True,
    add_dirs: Annotated[list[str] | None, Field(description="Extra absolute folders B may work in.")] = None,
    max_turns: Annotated[int | None, Field(ge=1, le=500, description="Optional cap on agent turns.")] = None,
) -> dict:
    """Start a worker-B job (headless Claude Code on the second account) and return its job_id."""
    cwd = _check_cwd(cwd, mode)
    for extra in add_dirs or []:
        _check_cwd(extra, mode)
    return _w_launch(task, cwd, mode, model, effort, add_dirs, max_turns, None, None, ultracode, shell)


@mcp.tool()
async def worker_reply(
    job_id: Annotated[str, Field(description="A finished worker job whose session should be continued.")],
    message: Annotated[str, Field(description="Follow-up instruction or question for the same B session.")],
    mode: Annotated[Mode | None, Field(description="Defaults to the original job's mode.")] = None,
) -> dict:
    """Continue an earlier worker-B session with its full context. Starts a new background job."""
    status, meta, ev = _w_status(job_id)
    if status in ("running", "cancelling"):
        raise ToolError(f"job {job_id} is still {status}; wait for it first")
    session = ev["session_id"] or meta.get("resume_session")
    if not session:
        raise ToolError(f"job {job_id} has no session id to resume")
    mode = mode or meta.get("mode") or "read-only"
    cwd = _check_cwd(meta["cwd"], mode)
    return _w_launch(message, cwd, mode, meta.get("model"), meta.get("effort"), meta.get("add_dirs"),
                     meta.get("max_turns"), session, job_id, meta.get("ultracode"), meta.get("shell", True))


@mcp.tool()
async def worker_wait(
    job_id: Annotated[str, Field(description="Worker job to wait for.")],
    timeout_sec: Annotated[int, Field(ge=0, le=WAIT_MAX_SEC, description="Max seconds to block.")] = 240,
) -> dict:
    """Block until the worker job finishes or timeout_sec passes; returns its answer when done."""
    deadline = time.monotonic() + timeout_sec
    while _w_status(job_id)[0] not in claude_worker.TERMINAL and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SEC)
    view = await asyncio.to_thread(claude_worker.job_view, job_id)
    if view["status"] not in claude_worker.TERMINAL:
        view["note"] = "still running; call worker_wait again or do other work meanwhile"
    return view


@mcp.tool()
async def worker_status(
    job_id: Annotated[str | None, Field(description="Worker job to inspect; omit to list recent jobs.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Non-blocking: one worker job's progress (or answer), or a list of recent worker jobs."""
    if job_id:
        _w_status(job_id)
        return await asyncio.to_thread(claude_worker.job_view, job_id)
    ids = jobs.list_job_ids(claude_worker.PROVIDER, limit)
    return {"jobs": await asyncio.to_thread(lambda: [claude_worker.job_view(j, "brief") for j in ids])}


@mcp.tool()
async def worker_cancel(job_id: Annotated[str, Field(description="Worker job to stop.")]) -> dict:
    """Stop a running worker-B job (kills its process tree). Edits already made stay."""
    status = _w_status(job_id)[0]
    if status in claude_worker.TERMINAL:
        return {"job_id": job_id, "status": status, "note": "already finished"}
    ok, msg = jobs.cancel(job_id)
    await asyncio.sleep(1)
    return {"job_id": job_id, "kill_ok": ok, "kill_output": msg, "status": _w_status(job_id)[0]}


def main() -> None:
    try:
        jobs.cleanup_old(KEEP_DAYS)
    except OSError:
        pass
    mcp.run()


if __name__ == "__main__":
    main()
