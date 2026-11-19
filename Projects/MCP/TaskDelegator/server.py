"""TaskDelegator MCP server (stdio), registered in Claude Code as "task-delegator".

Exposes other AI agents to Claude through one set of tools. An agent is a kind plus an account:
"codex", "codex:<name>", "claude:<name>" (see task_delegator/agents.py); each kind is an adapter
module (task_delegator/codex.py, task_delegator/claude.py). Run with the project's portable Python:

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe server.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from pathlib import Path
from typing import Annotated, Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pydantic import Field  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402  (message reaches the model)

from task_delegator import __version__, agents, jobs, limits  # noqa: E402

KEEP_DAYS = int(os.environ.get("TASK_DELEGATOR_KEEP_DAYS", "14"))
WAIT_MAX_SEC = 600
POLL_SEC = 2.0

INSTRUCTIONS = """\
TaskDelegator lets you (Claude) delegate work to other AI agents running on this PC. An agent is a
kind plus an account: "codex" (OpenAI Codex), "codex:<name>", "claude:<name>" (Claude Code on another
Claude account, with its own limits). list_agents() shows who is there, their defaults, options and
current limits; task_* tools work the same for every agent.

Treat an agent as a capable subordinate, not an oracle:
- Give it a complete, self-contained brief: goal, relevant paths, constraints, what "done" looks like,
  and what to return. It does not see this conversation.
- Default to mode="read-only" (analysis, review, second opinion, research in the codebase).
  Use mode="write" only when you deliberately hand it an implementation task in `cwd`,
  and avoid editing the same files yourself while it runs. A start result with a "warning" names
  another running write job in an overlapping folder: split the files between them or wait.
- Its answer is a claim, not a fact: check its diff / key statements before reporting them as done.
- Jobs run in the background: task_start returns a job_id immediately; do other work, then
  task_wait (blocks up to timeout_sec) or task_status. task_reply continues the same session.

Kinds differ:
- codex: sandboxed (read-only, or writes only inside cwd/add_dirs); its shell has NO network.
  options: web_search (live web search, also in read-only mode).
- claude: in write mode it edits files in cwd/add_dirs and runs shell commands WITH network as the
  Windows user (not a sandbox). options: shell (false = edits only), ultracode (costly multi-agent
  workflows, write mode only), max_turns.

Every agent has limits (a 5-hour and a weekly window), checked before each start:
- If an agent is in "warn", prefer smaller, well-scoped jobs; in "pause" task_start refuses and names
  the reset time: use another agent, do the work yourself, or wait. Pass ignore_limits=true only when
  the user explicitly wants to spend the remaining quota.
- A finished Codex job reports limits_cost; a finished claude job reports the account's rate_limits.

Model and effort: pass both explicitly unless the agent has defaults (list_agents()). Otherwise the
account's own config applies, which may be slow or expensive. Pick the cheapest pair that fits:
- simple question / mechanical edit with exact instructions: a small fast model, low
- ordinary well-scoped task (a test, small feature, known bug, small review): a general model, low-medium
- multi-file change, unclear bug, large review, architecture: a general model, high
- hardest, high-stakes work: the strongest model, high-xhigh
- max / ultra only when the user asks; in "warn" stay at medium or below and avoid the strongest model.
delegator_doctor lists Codex models with descriptions and supported efforts. If the user has a
delegation policy (e.g. in their CLAUDE.md), it takes precedence over these defaults.
"""

mcp = MCPServer(name="task-delegator", version=__version__, instructions=INSTRUCTIONS)

Mode = Literal["read-only", "write"]


# =========================================================================== lookups


def _agent(agent_id: str) -> agents.Agent:
    try:
        return agents.get(agent_id)
    except KeyError as e:
        raise ToolError(str(e.args[0])) from e


def _adapter(agent: agents.Agent) -> agents.Adapter:
    return agents.load_kinds()[agent.kind]


def _provider_of(job_id: str, meta: dict) -> str:
    return meta.get("provider") or job_id.split("-", 1)[0]


def _job(job_id: str) -> tuple[agents.Adapter, str, dict, dict]:
    """(adapter, status, meta, events) of any job, whatever its agent."""
    try:
        meta = jobs.load_meta(job_id)
        ad = agents.adapter_for(_provider_of(job_id, meta))
        status, meta, ev = ad.job_status(job_id)
    except (KeyError, ValueError) as e:
        raise ToolError(f"unknown job id {job_id!r}; call task_status() to list jobs") from e
    return ad, status, meta, ev


def _agent_id_of(job_id: str, meta: dict) -> str | None:
    """The agent a job ran on. Jobs from before agents existed: Codex ones were on "codex";
    the account of an old worker job is not recorded."""
    if meta.get("agent"):
        return meta["agent"]
    return "codex" if _provider_of(job_id, meta) == "codex" else None


def _running_jobs(agent_id: str | None = None) -> list[tuple[str, dict]]:
    # All jobs, not the newest N: a long job must not drop out of capacity and session checks
    # behind many short ones. Finished jobs are skipped by their exit.json before reading job.json;
    # cleanup_old keeps the folder count bounded.
    out = []
    for jid in jobs.list_job_ids(None, limit=1_000_000):
        if (jobs.JOBS_DIR / jid / "exit.json").exists():
            continue
        try:
            meta = jobs.load_meta(jid)
        except (KeyError, ValueError):
            continue
        if agent_id is not None and _agent_id_of(jid, meta) != agent_id:
            continue
        if jobs.is_running(jid, meta):  # a job being cancelled still counts until it is gone
            out.append((jid, meta))
    return out


def _running_count(agent_id: str) -> int:
    return len(_running_jobs(agent_id))


def _max_jobs(agent: agents.Agent) -> int:
    try:
        return int(agent.setting("max_jobs", _adapter(agent).DEFAULT_MAX_JOBS))
    except (TypeError, ValueError):
        return _adapter(agent).DEFAULT_MAX_JOBS


# =========================================================================== checks


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
    # model / effort end up inside `-c key="value"` TOML overrides (Codex) and CLI args (Claude).
    if value is not None and not _SAFE_TOKEN.match(value):
        raise ToolError(f"invalid {name}: {value!r}")


def _options(agent: agents.Agent, mode: str, given: dict | None, base: dict | None = None) -> dict:
    """Effective options: the kind's defaults < the agent's (agents.json) or the parent job's < given."""
    ad = _adapter(agent)
    supported = {k: d for k, (d, _desc) in ad.OPTIONS.items()}
    unknown = set(given or {}) - set(supported)
    if unknown:
        raise ToolError(f"unknown option(s) {sorted(unknown)} for {agent.kind} agents; "
                        f"supported: {', '.join(supported) or 'none'}")
    configured = base if base is not None else agent.setting("options", {})
    if not isinstance(configured, dict):
        configured = {}  # agents.load_config already reported it
    merged = {**supported, **{k: v for k, v in configured.items() if k in supported}, **(given or {})}
    problem = ad.check_options(merged)  # types first: "false" must not count as true below
    if problem:
        raise ToolError(f"{agent.id}: {problem}")
    normalize = getattr(ad, "normalize_options", None)
    return normalize(mode, merged) if normalize else merged


def _prepare(agent: agents.Agent, model: str | None, effort: str | None) -> tuple[str, str | None, str | None]:
    """Cheap validation shared by every start path. Returns (exe, model, effort) with defaults applied."""
    model = model or agent.setting("model")
    effort = effort or agent.setting("effort")
    _check_token("model", model)
    _check_token("effort", effort)
    ad = _adapter(agent)
    problem = ad.validate(agent, model, effort)
    if problem:
        raise ToolError(problem + " (see delegator_doctor for the model list)")
    exe, how = ad.find_exe()
    if not exe:
        raise ToolError(f"{agent.kind} CLI not available: {how}")
    return exe, model, effort


def _check_capacity(agent: agents.Agent) -> None:
    running, cap = _running_count(agent.id), _max_jobs(agent)
    if running >= cap:
        raise ToolError(f"{running} jobs already running on {agent.id} (limit {cap}); "
                        "wait for or cancel one first, or use another agent")


def _session_busy(agent_id: str, session: str) -> str | None:
    """A running job that already drives this session (two resumes would race)."""
    for jid, meta in _running_jobs(agent_id):
        try:
            ad = agents.adapter_for(_provider_of(jid, meta))
            _status, meta, ev = ad.job_status(jid)
        except (KeyError, ValueError):
            continue
        if session == ad.session_of(meta, ev):
            return jid
    return None


def _check_session_free(agent_id: str, session: str | None) -> None:
    busy = _session_busy(agent_id, session) if session else None
    if busy:
        raise ToolError(f"session {session} of {agent_id} is already busy in job {busy}; wait for it first")


def _limits_gate(agent: agents.Agent, exe: str, ignore_limits: bool) -> tuple[dict, dict | None]:
    """Check the agent's limits before a start. Returns (info for the caller, snapshot for the job).

    Fails open: when the limits cannot be read the job still starts, and the caller is told.
    """
    try:
        verdict, snapshot = agents.read_limits(agent, exe)
    except limits.LimitsUnavailable as e:
        return {"state": "unknown", "note": f"limits unavailable: {e}; started without a check"}, None
    state = verdict.get("state", "unknown")
    info = {"state": state, "summary": verdict.get("summary"), "source": verdict.get("source")}
    if verdict.get("reasons"):
        info["reasons"] = verdict["reasons"]
    if state == "pause":
        if not ignore_limits:
            when = verdict.get("resume_after")
            raise ToolError(
                f"{agent.id} limits are exhausted: " + "; ".join(verdict.get("reasons") or [])
                + (f". The window resets at {when}." if when else ".")
                + " Use another agent, do the work yourself, wait for the reset, or pass"
                  " ignore_limits=true if the user explicitly wants to spend what is left."
            )
        info["note"] = "started despite the pause threshold (ignore_limits=true)"
        info["overridden"] = True
    elif state == "warn":
        info["note"] = "close to the limit: keep jobs small and important"
    return info, snapshot


def _write_conflicts(roots: list[str]) -> list[str]:
    """Running write jobs (any agent) whose cwd/add_dirs overlap these folders.

    Only a warning: parallel jobs in one repository are fine as long as they edit different files,
    which the server cannot know in advance.
    """
    def norm(p: str) -> Path:
        # Paths arrive in different spellings: add_dirs are stored as given, cwd is resolved, and
        # Windows may hand out 8.3 short names (C:\Users\RUNNER~1\...) for the same folder.
        # resolve() folds "..", "." and separators and, for an existing folder, expands short names.
        try:
            return Path(p).resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return Path(os.path.abspath(p))

    mine = [norm(r) for r in roots if r]
    found = []
    for jid, meta in _running_jobs():
        if meta.get("mode") != "write" or meta.get("cancel_requested"):
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


# =========================================================================== launch


def _launch(agent: agents.Agent, prompt: str, cwd: str, mode: str, model: str | None, effort: str | None,
            add_dirs: list[str] | None, options: dict, resume_session: str | None = None,
            parent_job: str | None = None, ignore_limits: bool = False) -> dict:
    """Synchronous start path, for scripts (scripts/codex_job.py). The MCP tools use _launch_async."""
    exe, model, effort = _prepare(agent, model, effort)
    _check_capacity(agent)
    limits_info, limits_before = _limits_gate(agent, exe, ignore_limits)
    _check_capacity(agent)
    _check_session_free(agent.id, resume_session)
    return _create_and_launch(agent, exe, prompt, cwd, mode, model, effort, add_dirs, options,
                              resume_session, parent_job, limits_info, limits_before)


async def _launch_async(agent: agents.Agent, prompt: str, cwd: str, mode: str, model: str | None,
                        effort: str | None, add_dirs: list[str] | None, options: dict,
                        resume_session: str | None, parent_job: str | None, ignore_limits: bool) -> dict:
    exe, model, effort = _prepare(agent, model, effort)
    _check_capacity(agent)  # fail fast, before spending a second on the limits read
    # Only the slow limits read runs off the loop. If the call is cancelled here, it raises at
    # this await and nothing has been created or launched yet.
    limits_info, limits_before = await asyncio.to_thread(_limits_gate, agent, exe, ignore_limits)
    # Back on the event loop: no await from here until the job exists, so concurrent tool calls
    # cannot slip between these checks and the job becoming visible to them.
    _check_capacity(agent)
    _check_session_free(agent.id, resume_session)
    return _create_and_launch(agent, exe, prompt, cwd, mode, model, effort, add_dirs, options,
                              resume_session, parent_job, limits_info, limits_before)


def _create_and_launch(agent: agents.Agent, exe: str, prompt: str, cwd: str, mode: str, model: str | None,
                       effort: str | None, add_dirs: list[str] | None, options: dict,
                       resume_session: str | None, parent_job: str | None,
                       limits_info: dict, limits_before: dict | None) -> dict:
    ad = _adapter(agent)
    warning = _conflict_warning(mode, cwd, add_dirs)  # before create_job: the new job must not see itself
    meta = {
        "agent": agent.id, "account": agent.account, "home": agent.home,
        "mode": mode, "cwd": cwd, "model": model, "effort": effort, "add_dirs": add_dirs or [],
        "options": options, "parent_job": parent_job, "resume_session": resume_session,
        "task_preview": " ".join(prompt.split())[:300], "exe": exe,
        "limits_before": limits_before, "limits_overridden": bool(limits_info.get("overridden")),
    }
    job_id, d = jobs.create_job(agent.kind, prompt, meta)
    try:
        argv, env, on_exit = ad.build(agent, exe, d, meta)
        jobs.launch(job_id, argv, cwd, env, on_exit=on_exit)
    except OSError as e:
        jobs.write_json(d / "exit.json", {"exit_code": -1, "finished_at": jobs.now_iso(), "error": str(e)})
        raise ToolError(f"failed to start {agent.id}: {e}") from e
    result = {"job_id": job_id, "agent": agent.id, "status": "running", "mode": mode, "cwd": cwd,
              "model": model, "effort": effort, "options": options, "limits": limits_info,
              "hint": "call task_wait(job_id) when you are ready for the result"}
    if warning:
        result["warning"] = warning
    return result


# =========================================================================== tools


def _agent_card(agent: agents.Agent) -> dict:
    ad = _adapter(agent)
    exe, how = ad.find_exe()
    card: dict[str, Any] = {"id": agent.id, "kind": agent.kind, "account": agent.account}
    if agent.setting("name"):
        card["name"] = agent.setting("name")
    if agent.setting("description"):
        card["description"] = agent.setting("description")
    card["available"] = bool(exe)
    if not exe:
        card["problem"] = how
    card["running_jobs"] = _running_count(agent.id)
    card["max_jobs"] = _max_jobs(agent)
    try:
        defaults_options = _options(agent, "write", None)
    except ToolError as e:
        defaults_options = f"invalid in agents.json: {e}"
    card["defaults"] = {"model": agent.setting("model") or "(the account's own default)",
                        "effort": agent.setting("effort") or "(the account's own default)",
                        "options": defaults_options}
    card["options"] = {k: desc for k, (_d, desc) in ad.OPTIONS.items()}
    if exe:
        try:
            verdict, _snapshot = agents.read_limits(agent, exe)
        except limits.LimitsUnavailable as e:
            verdict = {"state": "unknown", "note": f"limits unavailable: {e}"}
        card["limits"] = {k: verdict[k] for k in ("state", "summary", "reasons", "resume_after", "note", "source")
                          if verdict.get(k)}
    return card


@mcp.tool()
async def list_agents() -> dict:
    """Agents you can delegate to: id, kind, account, availability, defaults, options, running jobs, limits.

    Free: limits come from Codex's usage script and from the newest result of each Claude account.
    """
    found, problems = await asyncio.to_thread(agents.discover)
    cards = [await asyncio.to_thread(_agent_card, a) for a in found.values()]
    result: dict[str, Any] = {"agents": cards}
    if problems:
        result["problems"] = problems
    if not cards:
        result["note"] = "no agents found; see delegator_doctor"
    return result


@mcp.tool()
async def task_start(
    agent: Annotated[str, Field(description="Agent id from list_agents(), e.g. codex or claude:<account>.")],
    task: Annotated[str, Field(description="Self-contained brief: goal, paths, constraints, definition of done, "
                                           "what to return. The agent does not see this chat.")],
    cwd: Annotated[str, Field(description="Absolute path of the folder the agent works in (its workspace root).")],
    mode: Annotated[Mode, Field(description="read-only: may read (and, Codex, run read-only commands). "
                                            "write: may also edit files inside cwd and add_dirs; a claude "
                                            "agent also runs shell commands with network unless "
                                            "options.shell=false.")] = "read-only",
    model: Annotated[str | None, Field(description="Model for this job. Codex: e.g. gpt-6-luna | gpt-6.1-sol | "
                                                   "gpt-6-astra (see delegator_doctor). Claude: an id or alias "
                                                   "(opus, sonnet, haiku). Default: the agent's default.")] = None,
    effort: Annotated[str | None, Field(description="Reasoning effort: low | medium | high | xhigh "
                                                    "(max/ultra only on the user's request). "
                                                    "Default: the agent's default.")] = None,
    add_dirs: Annotated[list[str] | None, Field(description="Extra absolute folders the agent may write to (write mode).")] = None,
    options: Annotated[dict[str, Any] | None, Field(description="Kind-specific options (see list_agents()): "
                                                                "codex {web_search}; claude {shell, ultracode, "
                                                                "max_turns}.")] = None,
    ignore_limits: Annotated[bool, Field(description="Start even if the agent's limits are past the pause "
                                                     "threshold. Only when the user explicitly asks.")] = False,
) -> dict:
    """Start a job on an agent in the background and return its job_id immediately (checks limits first)."""
    a = _agent(agent)
    cwd = _check_cwd(cwd, mode)
    for extra in add_dirs or []:
        _check_cwd(extra, mode)
    opts = _options(a, mode, options)
    return await _launch_async(a, task, cwd, mode, model, effort, add_dirs, opts, None, None, ignore_limits)


@mcp.tool()
async def task_reply(
    job_id: Annotated[str, Field(description="A finished job whose session should be continued.")],
    message: Annotated[str, Field(description="Follow-up instruction or question for the same session.")],
    mode: Annotated[Mode | None, Field(description="Mode for this turn; defaults to the original job's mode.")] = None,
    options: Annotated[dict[str, Any] | None, Field(description="Options to change for this turn; the rest are "
                                                                "taken from the original job.")] = None,
    ignore_limits: Annotated[bool, Field(description="Start even if the agent's limits are past the pause "
                                                     "threshold. Only when the user explicitly asks.")] = False,
) -> dict:
    """Continue an earlier session on the same agent (it keeps its full context). Starts a new background job."""
    ad, status, meta, ev = _job(job_id)
    if status in ("running", "cancelling"):
        raise ToolError(f"job {job_id} is still {status}; wait for it first")
    session = ad.session_of(meta, ev)
    if not session:
        raise ToolError(f"job {job_id} has no session id to resume")
    agent_id = _agent_id_of(job_id, meta)
    if not agent_id:
        raise ToolError(f"job {job_id} was started before agents existed and its account is unknown; "
                        "start a new task instead")
    a = _agent(agent_id)
    _check_session_free(a.id, session)  # fail fast; re-checked right before the launch
    mode = mode or meta.get("mode") or "read-only"
    cwd = _check_cwd(meta["cwd"], mode)
    add_dirs = meta.get("add_dirs") or []
    for extra in add_dirs:
        _check_cwd(extra, mode)
    base = meta.get("options")
    if base is None:  # a job from before options existed
        base = {k: meta[k] for k in ("web_search", "ultracode", "shell", "max_turns") if meta.get(k) is not None}
    opts = _options(a, mode, options, base)
    return await _launch_async(a, message, cwd, mode, meta.get("model"), meta.get("effort"), add_dirs, opts,
                               session, job_id, ignore_limits)


@mcp.tool()
async def task_wait(
    job_id: Annotated[str, Field(description="Job to wait for.")],
    timeout_sec: Annotated[int, Field(ge=0, le=WAIT_MAX_SEC, description="Max seconds to block.")] = 240,
) -> dict:
    """Block until the job finishes or timeout_sec passes. Returns progress, or the final answer when done."""
    deadline = time.monotonic() + timeout_sec
    while True:
        ad, status, _meta, _ev = _job(job_id)
        if status in ad.TERMINAL or time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_SEC)
    view = await asyncio.to_thread(ad.job_view, job_id)  # may run `git status`
    if view["status"] not in ad.TERMINAL:
        view["note"] = "still running; call task_wait again or do other work meanwhile"
    return view


@mcp.tool()
async def task_status(
    job_id: Annotated[str | None, Field(description="Job to inspect; omit to list recent jobs.")] = None,
    agent: Annotated[str | None, Field(description="When listing: only jobs of this agent.")] = None,
    limit: Annotated[int, Field(ge=1, le=50, description="How many recent jobs to list.")] = 10,
) -> dict:
    """Non-blocking: one job's progress (or final answer), or a list of recent jobs."""
    if job_id:
        ad = _job(job_id)[0]
        return await asyncio.to_thread(ad.job_view, job_id)

    def listing() -> list[dict]:
        out = []
        for jid in jobs.list_job_ids(None, limit=500):
            if len(out) >= limit:
                break
            try:
                meta = jobs.load_meta(jid)
                if agent and (_agent_id_of(jid, meta) or _provider_of(jid, meta)) != agent:
                    continue
                out.append(agents.adapter_for(_provider_of(jid, meta)).job_view(jid, "brief"))
            except (KeyError, ValueError):
                continue
        return out

    return {"jobs": await asyncio.to_thread(listing)}


@mcp.tool()
async def task_cancel(job_id: Annotated[str, Field(description="Job to stop.")]) -> dict:
    """Stop a running job (kills its process tree). Files it already changed stay changed."""
    ad, status, _meta, _ev = _job(job_id)
    if status in ad.TERMINAL:
        return {"job_id": job_id, "status": status, "note": "already finished"}
    ok, msg = jobs.cancel(job_id)
    await asyncio.sleep(1)
    return {"job_id": job_id, "kill_ok": ok, "kill_output": msg, "status": _job(job_id)[1]}


@mcp.tool()
async def delegator_doctor() -> dict:
    """Health check: agent kinds and accounts found, executables, versions, logins, config files, paths."""
    found, problems = await asyncio.to_thread(agents.discover)
    info: dict[str, Any] = {
        "delegator_version": __version__,
        "python": sys.version.split()[0],
        "state_dir": str(jobs.STATE_ROOT),
        "keep_days": KEEP_DAYS,
        "agents_file": {"path": str(agents.agents_file()), "exists": agents.agents_file().exists()},
        "accounts_dirs": {kind: str(agents.accounts_dir(kind)) for kind in agents.load_kinds()},
        "codex_usage": {
            "script": limits.usage_script(),
            "exists": Path(limits.usage_script()).is_file(),
        },
        "thresholds": limits.thresholds(),
    }
    kinds: dict[str, dict] = {}
    for kind, ad in agents.load_kinds().items():
        exe, how = ad.find_exe()
        kinds[kind] = {"exe": exe, "found_via": how}
    info["kinds"] = kinds
    info["agents"] = {}
    for a in found.values():
        entry: dict[str, Any] = {"home": a.home or "(default)", "max_jobs": _max_jobs(a)}
        if a.setting("name"):
            entry["name"] = a.setting("name")
        exe = kinds[a.kind]["exe"]
        if exe:
            entry.update(await asyncio.to_thread(_adapter(a).doctor, a, exe))
        info["agents"][a.id] = entry
    if problems:
        info["problems"] = problems
    return info


def main() -> None:
    try:
        jobs.cleanup_old(KEEP_DAYS)
    except OSError:
        pass
    mcp.run()


if __name__ == "__main__":
    main()
