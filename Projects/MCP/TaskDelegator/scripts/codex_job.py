"""Run one Codex job through the TaskDelegator core, optionally at a set time, and wait for it.

Same job machinery as the MCP server (the job shows up in codex_status), but usable from a
shell, a scheduled task or a background process when the MCP tools are not loaded.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe scripts\\codex_job.py
        --task-file TASK.md --cwd E:\\Some\\Project [--mode write] [--effort medium]
        [--start-at 05:25] [--wait-min 60] [--dry-run] [--no-limits]

Before starting it checks Codex limits with the shared usage script (task_delegator.limits). If a
window is past its pause threshold it sleeps until that window resets and checks again,
instead of failing. While waiting or running it keeps Windows from idle-sleeping (released
when the process exits).

Exit codes: 0 job completed, 1 job failed/cancelled/lost or setup error, 3 still running
when --wait-min ran out (the job keeps going; check it with codex_status).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from task_delegator import codex, limits  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
except (AttributeError, ValueError):
    pass


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def keep_awake() -> None:
    if os.name != "nt":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ok = ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    log("keep-awake: on (until this process exits)" if ok else "keep-awake: request failed")


def sleep_until(target: datetime, why: str) -> None:
    secs = (target - datetime.now()).total_seconds()
    if secs <= 0:
        return
    log(f"waiting until {target:%Y-%m-%d %H:%M:%S} ({secs / 60:.0f} min): {why}")
    next_note = time.monotonic() + 1800
    while (left := (target - datetime.now()).total_seconds()) > 0:
        time.sleep(min(30.0, left))
        if time.monotonic() >= next_note:
            log(f"still waiting, {left / 60:.0f} min left")
            next_note += 1800


MIN_PAUSE_WAIT_SEC = 60


def gate_on_limits(pause: dict[str, float], margin_sec: int, max_rounds: int = 4) -> None:
    """Return when limits allow a start; sleep through resets while they do not."""
    custom = limits.thresholds()
    for kind, value in pause.items():
        if value is not None:
            custom[kind]["pause"] = value
    exe, _how = codex.find_codex()
    unavailable = pauses = 0
    while True:
        try:
            usage = limits.read(exe)
        except limits.LimitsUnavailable as e:
            unavailable += 1
            log(f"limits: unavailable: {e}")
            if unavailable >= 3:
                log("limits: still unavailable, starting anyway")
                return
            time.sleep(60)
            continue
        verdict = limits.evaluate(usage, custom)
        log(f"limits: {verdict['summary']} -> {verdict['state']}")
        if verdict["state"] != "pause":
            return
        pauses += 1
        if pauses > max_rounds:
            log("limits: still blocked after several resets, giving up")
            raise SystemExit(1)
        log("limits block the start: " + "; ".join(verdict["reasons"]))
        if verdict.get("resume_after"):
            # Compare absolute (aware) times: naive wall-clock math breaks across a DST change.
            wait = (datetime.fromisoformat(verdict["resume_after"])
                    - datetime.now().astimezone()).total_seconds() + margin_sec
        else:
            wait = 15 * 60
        # Never re-poll instantly: a reset already in the past would otherwise burn every round.
        wait = max(wait, MIN_PAUSE_WAIT_SEC)
        if wait > 12 * 3600:
            log("limits: reset is more than 12 h away, giving up")
            raise SystemExit(1)
        sleep_until(datetime.now() + timedelta(seconds=wait), "Codex limit window reset")


def parse_start_at(value: str) -> datetime:
    hh, mm = (int(x) for x in value.split(":"))
    # A time that already passed today means "now", not tomorrow: the caller picked it for today.
    return datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--task-file", required=True)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--mode", choices=["read-only", "write"], default="read-only")
    ap.add_argument("--effort")
    ap.add_argument("--model")
    ap.add_argument("--start-at", help="HH:MM local time today")
    ap.add_argument("--limits-script", help="override the usage script (default: task_delegator.limits)")
    ap.add_argument("--no-limits", action="store_true", help="skip the limits check entirely")
    ap.add_argument("--pause-5h", type=float, help="pause threshold for the 5h window (default: server's)")
    ap.add_argument("--pause-7d", type=float, help="pause threshold for the 7d window (default: server's)")
    ap.add_argument("--reset-margin-sec", type=int, default=120)
    ap.add_argument("--wait-min", type=float, default=60.0)
    ap.add_argument("--dry-run", action="store_true", help="do everything except starting Codex")
    ap.add_argument("--web-search", action="store_true", help="allow live web search")
    args = ap.parse_args()

    if args.limits_script:
        os.environ["TASK_DELEGATOR_CODEX_USAGE_SCRIPT"] = args.limits_script

    import server  # pulls in the MCP SDK; reuses the exact validation and launch path of codex_start

    # Validate everything that can be validated BEFORE any waiting: a typo should fail now,
    # not after sleeping until --start-at.
    try:
        task = Path(args.task_file).read_text(encoding="utf-8-sig")
        if not task.strip():
            raise ValueError(f"task file is empty: {args.task_file}")
        cwd = server._check_cwd(args.cwd, args.mode)
        exe = server._prepare(args.model, args.effort)
        if not args.no_limits and not Path(limits.usage_script()).is_file():
            raise ValueError(f"usage script not found: {limits.usage_script()} (or pass --no-limits)")
    except Exception as e:  # ToolError, OSError, ValueError
        log(f"setup error: {e}")
        return 1
    log(f"setup ok: codex={exe} mode={args.mode} effort={args.effort} cwd={cwd}")

    if args.dry_run:
        log(f"dry-run: task is {len(task)} chars, first line: {task.strip().splitlines()[0][:120]!r}")
        if args.start_at:
            log(f"dry-run: would wait until {parse_start_at(args.start_at):%H:%M}")
        if not args.no_limits:
            try:
                verdict = limits.evaluate(limits.read(exe))
                log(f"dry-run: limits now: {verdict['summary']} -> {verdict['state']}")
            except limits.LimitsUnavailable as e:
                log(f"dry-run: limits unavailable now: {e}")
        return 0

    keep_awake()
    if args.start_at:
        target = parse_start_at(args.start_at)
        if target <= datetime.now():
            log(f"--start-at {args.start_at} already passed today: starting now")
        sleep_until(target, f"--start-at {args.start_at}")
    if not args.no_limits:
        gate_on_limits({"primary": args.pause_5h, "secondary": args.pause_7d}, args.reset_margin_sec)

    # Our own gate above already decided (and may use custom thresholds), so the server-side
    # check must not veto it again; it still records the "before" reading for limits_cost.
    try:
        started = server._launch(task, cwd, args.mode, args.model, args.effort, None, None, None,
                                 ignore_limits=True,
                                 opts={"web_search": args.web_search})
    except Exception as e:  # e.g. too many jobs running by now
        log(f"could not start: {e}")
        return 1
    job_id = started["job_id"]
    log(f"started job {job_id} (mode={args.mode}, effort={args.effort}, cwd={cwd}); "
        f"limits: {started['limits'].get('summary') or started['limits'].get('note')}")

    deadline = time.monotonic() + args.wait_min * 60
    last = None
    while True:
        status, _meta, ev = codex.job_status(job_id)
        note = f"{status}, {len(ev['commands'])} commands, {len(ev['file_changes'])} files changed"
        if note != last:
            log(note)
            last = note
        if status in codex.TERMINAL or time.monotonic() >= deadline:
            break
        time.sleep(15)

    view = codex.job_view(job_id, "full")
    print("=== JOB RESULT ===", flush=True)
    print(json.dumps(view, ensure_ascii=False, indent=2), flush=True)
    if view["status"] == "completed":
        return 0
    return 3 if view["status"] not in codex.TERMINAL else 1


if __name__ == "__main__":
    sys.exit(main())
