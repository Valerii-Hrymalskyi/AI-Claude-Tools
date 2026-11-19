"""Agents: who the server can hand a task to.

An agent is a kind (an adapter module: codex, claude, ...) plus an account:

    codex          Codex with its default home (~/.codex or CODEX_HOME)
    codex:<name>   Codex on another account, home %USERPROFILE%\\.codex-accounts\\<name>
    claude:<name>  Claude Code on another account, home %USERPROFILE%\\.claude-accounts\\<name>

Accounts are discovered from those folders: adding one is "make the folder, sign in", no config.
The lead Claude's own account (~/.claude) is never an agent: it would share the lead's limits.

Optional per-agent settings live in agents.json (TASK_DELEGATOR_AGENTS_FILE, default
%LOCALAPPDATA%\\TaskDelegator\\agents.json):

    {"agents": {
        "claude:pro": {"description": "Pro plan, for development",
                       "model": "claude-opus-5-5", "effort": "xhigh",
                       "options": {"ultracode": true}, "max_jobs": 2},
        "codex":      {"model": "gpt-6.1-sol", "effort": "medium"},
        "claude:far": {"home": "D:\\\\Claude\\\\far"},        # an account folder outside the default root
        "codex:old":  {"disabled": true}
    }}

Keys: name (display name), description, model, effort, options (kind-specific, see each adapter),
max_jobs, home, disabled; kind-specific extras (claude: deny, workflow_agents).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

from . import jobs, limits

_ACCOUNT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
# Setting -> expected JSON type. A value of the wrong type is reported and ignored.
_SETTING_TYPES: dict[str, type] = {
    "name": str, "description": str, "model": str, "effort": str, "options": dict, "max_jobs": int,
    "home": str, "disabled": bool, "deny": list, "workflow_agents": int,
}


@dataclass(frozen=True)
class Agent:
    id: str                       # "codex", "codex:work", "claude:pro"
    kind: str                     # adapter name
    account: str | None           # None = the kind's default account
    home: str | None              # the account's config dir; None = the kind's default
    settings: dict[str, Any] = field(default_factory=dict, compare=False)

    def setting(self, key: str, default: Any = None) -> Any:
        value = self.settings.get(key)
        return default if value is None else value


class Adapter(Protocol):
    """What a kind module provides (codex.py, claude.py). See those modules for the details."""

    kind: str
    TERMINAL: set[str]
    OPTIONS: dict[str, tuple[Any, str]]      # option -> (default, description)
    DEFAULT_MAX_JOBS: int
    HAS_DEFAULT_ACCOUNT: bool                # codex: yes (~/.codex); claude: no (that is the lead)
    TRACKS_COST: bool                        # readings before/after a job give its cost (codex)

    def find_exe(self) -> tuple[str | None, str]: ...
    def doctor(self, agent: Agent, exe: str) -> dict: ...
    def validate(self, agent: Agent, model: str | None, effort: str | None) -> str | None: ...
    def read_usage(self, agent: Agent, exe: str, timeout: float | None = None) -> tuple[dict, str]: ...
    def build(self, agent: Agent, exe: str, job_dir: Path, meta: dict) -> tuple[list[str], dict, Any]: ...
    def job_status(self, job_id: str) -> tuple[str, dict, dict]: ...
    def job_view(self, job_id: str, detail: str = "full") -> dict: ...
    def session_of(self, meta: dict, ev: dict) -> str | None: ...


KINDS: dict[str, Adapter] = {}
# Job folders are named <provider>-<timestamp>-<rand>; old providers map onto current kinds.
LEGACY_PROVIDERS = {"worker": "claude"}


def register(adapter: Adapter) -> None:
    KINDS[adapter.kind] = adapter


def load_kinds() -> dict[str, Adapter]:
    """Import every kind module once; each registers itself. A new kind = a new import here."""
    from . import claude, codex  # noqa: F401  (imported for their register() side effect)
    return KINDS


def accounts_root() -> Path:
    return Path(os.environ.get("TASK_DELEGATOR_ACCOUNTS_ROOT") or Path.home())


def accounts_dir(kind: str) -> Path:
    return accounts_root() / f".{kind}-accounts"


def agents_file() -> Path:
    return Path(os.environ.get("TASK_DELEGATOR_AGENTS_FILE") or jobs.STATE_ROOT / "agents.json")


def load_config() -> tuple[dict[str, dict], list[str]]:
    """(settings by agent id, problems). A missing file is fine; a broken one is reported, not fatal."""
    path = agents_file()
    if not path.exists():
        return {}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        return {}, [f"{path}: unreadable ({e}); per-agent settings ignored"]
    entries = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}, [f"{path}: expected {{\"agents\": {{...}}}}; per-agent settings ignored"]
    out, problems = {}, []
    for aid, s in entries.items():
        if not isinstance(s, dict):
            problems.append(f"{path}: agents.{aid} is not an object; ignored")
            continue
        unknown = set(s) - set(_SETTING_TYPES)
        if unknown:
            problems.append(f"{path}: agents.{aid}: unknown keys {sorted(unknown)} ignored")
        clean = {}
        for k, v in s.items():
            t = _SETTING_TYPES.get(k)
            if t is None:
                continue
            ok = isinstance(v, t) and not (t is int and isinstance(v, bool))
            if ok and k == "deny":
                ok = all(isinstance(x, str) for x in v)
            if ok and k in ("max_jobs", "workflow_agents"):
                ok = v >= 1
            if not ok:
                problems.append(f"{path}: agents.{aid}.{k} must be "
                                f"{'a list of strings' if k == 'deny' else 'a positive integer' if t is int else t.__name__}; ignored")
                continue
            clean[k] = v
        out[aid] = clean
    return out, problems


def split_id(agent_id: str) -> tuple[str, str | None]:
    kind, _, account = agent_id.partition(":")
    return kind, (account or None)


def discover() -> tuple[dict[str, Agent], list[str]]:
    """All agents on this machine: (by id, problems worth showing in delegator_doctor)."""
    config, problems = load_config()
    found: dict[str, Agent] = {}
    for kind, adapter in load_kinds().items():
        if adapter.HAS_DEFAULT_ACCOUNT:
            found[kind] = Agent(kind, kind, None, None)
        root = accounts_dir(kind)
        if root.is_dir():
            for p in sorted(root.iterdir()):
                if p.is_dir() and _ACCOUNT_NAME.match(p.name):
                    aid = f"{kind}:{p.name}"
                    found[aid] = Agent(aid, kind, p.name, str(p))
    for aid, s in config.items():
        kind, account = split_id(aid)
        if kind not in KINDS or (account is not None and not _ACCOUNT_NAME.match(account)):
            problems.append(f"agents.json: {aid!r} is not a valid agent id (kinds: {', '.join(KINDS)})")
            continue
        if s.get("disabled"):
            found.pop(aid, None)
            continue
        base = found.get(aid)
        if s.get("home"):
            home = str(Path(os.path.expandvars(str(s["home"]))).expanduser())
            base = replace(base, home=home) if base else Agent(aid, kind, account, home)
        if base is None:
            problems.append(f"agents.json: {aid!r} has settings but no account folder "
                            f"({accounts_dir(kind) / (account or '')}) and no \"home\"")
            continue
        found[aid] = replace(base, settings=s)
    return found, problems


def get(agent_id: str) -> Agent:
    found, _ = discover()
    if agent_id not in found:
        known = ", ".join(found) or "none"
        raise KeyError(f"unknown agent {agent_id!r}; available: {known} (see agents())")
    return found[agent_id]


def adapter_for(provider: str) -> Adapter:
    """The adapter of a job's provider tag (job.json "provider"); KeyError if unknown."""
    return load_kinds()[LEGACY_PROVIDERS.get(provider, provider)]


def read_limits(agent: Agent, exe: str | None = None, timeout: float | None = None) -> tuple[dict, dict | None]:
    """An agent's limits: (verdict, snapshot). The same for every kind of agent.

    Each adapter's read_usage() returns its source's numbers in one shape - schema 1 of the Codex
    usage-script contract (docs/codex-usage-requirements.md): buckets of windows, each with a label,
    a kind (primary = the short window, secondary = the weekly one), used_percent and its reset
    time, plus an optional limit_reached flag. A kind whose source publishes other windows converts
    them there; the verdict (limits.evaluate), the start gate, list_agents() and
    scripts/team_limits.py need no kind-specific code. The snapshot (for a job's limits_cost) is
    returned only by kinds that track cost. Raises limits.LimitsUnavailable with the reason.
    """
    ad = load_kinds()[agent.kind]
    if exe is None:
        exe, how = ad.find_exe()
        if not exe:
            raise limits.LimitsUnavailable(f"{agent.kind} CLI not found ({how})")
    usage, source = ad.read_usage(agent, exe, timeout)
    verdict = limits.evaluate(usage)
    verdict["source"] = source
    return verdict, (limits.snapshot(usage) if getattr(ad, "TRACKS_COST", False) else None)
