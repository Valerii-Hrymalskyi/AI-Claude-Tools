"""TaskDelegator (MCP server "task-delegator"; formerly AITools / "aitools"): a local MCP hub that lets Claude delegate work to other AI agents.

Layout:
    procs.py   - Windows process helpers (liveness check, tree kill)
    jobs.py    - provider-agnostic background job store on disk
    codex.py   - Codex CLI provider (locate exe, build argv, parse JSONL events)
"""

__version__ = "0.1.0"
