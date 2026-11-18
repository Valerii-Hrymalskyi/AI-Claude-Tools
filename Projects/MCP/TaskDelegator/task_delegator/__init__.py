"""TaskDelegator (MCP server "task-delegator"; formerly AITools / "aitools"): a local MCP hub that lets Claude delegate work to other AI agents.

Layout:
    agents.py  - agents (kind + account): discovery, agents.json, the adapter interface
    codex.py   - kind "codex": Codex CLI (locate exe, build argv, parse JSONL events, limits_cost)
    claude.py  - kind "claude": Claude Code on another account (argv, permissions, stream-json, limits)
    jobs.py    - provider-agnostic background job store on disk
    limits.py  - thresholds and the ok/warn/pause verdict; the Codex usage script
    procs.py   - Windows process helpers (liveness check, tree kill)
"""

__version__ = "0.2.0"
