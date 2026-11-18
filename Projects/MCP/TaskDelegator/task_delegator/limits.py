"""Codex rate limits: read them via the shared usage script and judge them against thresholds.

The reader is an external script, `codex_usage.py`, that also feeds Codex's own Stop hook; it is
not part of this repository (contract: docs/codex-usage-requirements.md). We call it as a
subprocess and rely only on its documented contract: `--json` prints schema-1 JSON on stdout,
exit code 0 = success, anything else = unavailable (reason on stderr).

Where it is: TASK_DELEGATOR_CODEX_USAGE_SCRIPT, otherwise the author's layout, a `Codex`
workspace next to this clone (`<clone>\\..\\Codex\\Utils\\Scritps\\GetUsage\\codex_usage.py`;
"Scritps" is the real folder name there). Without it every check is "unknown".
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import procs

# <clone>/Projects/MCP/TaskDelegator/task_delegator/limits.py -> parents[4] is the clone root.
_CLONE_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_USAGE_SCRIPT = str(_CLONE_ROOT.parent / "Codex" / "Utils" / "Scritps" / "GetUsage" / "codex_usage.py")
SUPPORTED_SCHEMA = 1
SCRIPT_TIMEOUT_SEC = 20          # passed to the script (its app-server timeout)
SUBPROCESS_TIMEOUT_SEC = 45      # hard cap on the whole script run

# A reset moves resets_at by a whole window; anything smaller is server-side jitter.
_SAME_WINDOW_TOLERANCE_SEC = 600

# Thresholds by window kind, the same for every agent. Codex reports the short (5h) window as
# "primary" and the weekly (7d) window as "secondary"; keying by kind survives a change of length.
# The older TASK_DELEGATOR_CODEX_* names are still read when the new ones are not set.
_THRESHOLD_ENV = {
    ("primary", "warn"): ("TASK_DELEGATOR_WARN_5H", 90.0),
    ("primary", "pause"): ("TASK_DELEGATOR_PAUSE_5H", 95.0),
    ("secondary", "warn"): ("TASK_DELEGATOR_WARN_7D", 95.0),
    ("secondary", "pause"): ("TASK_DELEGATOR_PAUSE_7D", 98.0),
}


class LimitsUnavailable(RuntimeError):
    """The usage script could not produce data (missing, failed, timed out, bad output)."""


def usage_script() -> str:
    return os.environ.get("TASK_DELEGATOR_CODEX_USAGE_SCRIPT") or DEFAULT_USAGE_SCRIPT


def thresholds() -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {"primary": {}, "secondary": {}}
    for (kind, level), (env, default) in _THRESHOLD_ENV.items():
        legacy = env.replace("TASK_DELEGATOR_", "TASK_DELEGATOR_CODEX_", 1)
        try:
            out[kind][level] = float(os.environ.get(env) or os.environ.get(legacy) or default)
        except ValueError:
            out[kind][level] = default
    return out


def read(codex_exe: str | None = None, env: dict[str, str] | None = None) -> dict:
    """Run the usage script and return its schema-1 JSON. Raises LimitsUnavailable.

    env: the environment for the script (e.g. with CODEX_HOME of another account); None = inherit.
    """
    script = usage_script()
    if not Path(script).is_file():
        raise LimitsUnavailable(f"usage script not found: {script} (set TASK_DELEGATOR_CODEX_USAGE_SCRIPT)")
    argv = [sys.executable, script, "--json", "--timeout", str(SCRIPT_TIMEOUT_SEC)]
    if codex_exe:
        argv += ["--codex-exe", codex_exe]
    try:
        r = subprocess.run(
            argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=SUBPROCESS_TIMEOUT_SEC, env=env,
            creationflags=procs.CREATE_NO_WINDOW if procs.IS_WINDOWS else 0,
        )
    except subprocess.TimeoutExpired as e:
        raise LimitsUnavailable(f"usage script did not finish within {SUBPROCESS_TIMEOUT_SEC} s") from e
    except OSError as e:
        raise LimitsUnavailable(f"could not run usage script: {e}") from e
    stderr = r.stderr.decode("utf-8", "replace").strip()
    if r.returncode != 0:
        raise LimitsUnavailable(stderr[-400:] or f"usage script exited with code {r.returncode}")
    try:
        data = json.loads(r.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise LimitsUnavailable("usage script printed invalid JSON") from e
    if not isinstance(data, dict) or data.get("schema") != SUPPORTED_SCHEMA:
        schema = data.get("schema") if isinstance(data, dict) else None
        raise LimitsUnavailable(f"unsupported usage schema {schema!r} (task-delegator understands {SUPPORTED_SCHEMA})")
    return data


def _windows(usage: dict):
    for bucket in usage.get("buckets") or []:
        if not isinstance(bucket, dict):
            continue
        for w in bucket.get("windows") or []:
            if isinstance(w, dict):
                yield bucket, w


def _buckets(usage: dict) -> list[dict]:
    return [b for b in usage.get("buckets") or [] if isinstance(b, dict)]


def _fmt_reset(w: dict) -> str:
    """Local reset time for humans. Uses the unix time when present: the script's ISO string
    carries the UTC offset of the *measurement* moment, which is wrong across a DST change."""
    unix = w.get("resets_at_unix")
    if isinstance(unix, (int, float)) and unix > 0:
        dt = datetime.fromtimestamp(unix).astimezone()
    else:
        iso = w.get("resets_at")
        if not iso:
            return "?"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return iso
    # Round to the nearest minute: the server's reset time jitters by a second or two
    # (19:12:59 vs 19:13:00), which plain truncation would show as two different minutes.
    dt = dt + timedelta(seconds=30)
    today = datetime.now(dt.tzinfo).date()
    return dt.strftime("%H:%M") if dt.date() == today else dt.strftime("%d.%m %H:%M")


def evaluate(usage: dict, limits: dict[str, dict[str, float]] | None = None) -> dict:
    """Judge a schema-1 usage reading. state: ok | warn | pause."""
    limits = limits or thresholds()
    reasons_pause: list[str] = []
    reasons_warn: list[str] = []
    resume_unix: float | None = None
    reached_resets: list[float] = []
    windows = []
    buckets = _buckets(usage)
    multi = len(buckets) > 1

    if usage.get("ordinary_usage_allowed") is False:
        reasons_pause.append("Codex reports ordinary usage is not allowed right now")

    for bucket in buckets:
        bucket_windows = [w for w in bucket.get("windows") or [] if isinstance(w, dict)]
        # Checked per bucket, not per window: a bucket may report "limit reached" with no windows.
        if bucket.get("limit_reached"):
            reasons_pause.append(f"{bucket.get('name')}: limit reached ({bucket['limit_reached']})")
            reached_resets += [float(w["resets_at_unix"]) for w in bucket_windows
                               if isinstance(w.get("resets_at_unix"), (int, float))]
        for w in bucket_windows:
            label = w.get("label") or w.get("kind") or "window"
            name = f"{bucket.get('name')}/{label}" if multi else label
            used = w.get("used_percent")
            lim = limits.get(w.get("kind") or "", {})
            state = "ok"
            if isinstance(used, (int, float)):
                if "pause" in lim and used >= lim["pause"]:
                    state = "pause"
                    reasons_pause.append(f"{name} at {used:g}% (pause at {lim['pause']:g}%)")
                    if isinstance(w.get("resets_at_unix"), (int, float)):
                        resume_unix = max(resume_unix or 0.0, float(w["resets_at_unix"]))
                elif "warn" in lim and used >= lim["warn"]:
                    state = "warn"
                    reasons_warn.append(f"{name} at {used:g}% (warn at {lim['warn']:g}%)")
            windows.append({
                "window": name,
                "used_percent": used,
                "state": state,
                "resets_at": w.get("resets_at"),
                "resets_in_min": (None if w.get("resets_in_sec") is None
                                  else round(w["resets_in_sec"] / 60)),
                "_reset": _fmt_reset(w),
            })

    if resume_unix is None and reached_resets:
        # "Limit reached" without a window over our threshold: Codex does not say which window
        # tripped, so assume the nearest reset; a re-check after it decides again.
        resume_unix = min(reached_resets)
    state = "pause" if reasons_pause else ("warn" if reasons_warn else "ok")
    summary = " · ".join(
        f"{w['window']}: {w['used_percent']:g}% (reset {w.pop('_reset')})"
        if isinstance(w["used_percent"], (int, float)) else f"{w['window']}: ? ({w.pop('_reset')})"
        for w in windows
    ) or "no limit windows reported"
    result = {
        "state": state,
        "summary": summary,
        "reasons": reasons_pause + reasons_warn,
        "windows": windows,
        "plan_type": usage.get("plan_type"),
        "measured_at": usage.get("measured_at"),
    }
    if state == "pause" and resume_unix:
        result["resume_after"] = datetime.fromtimestamp(resume_unix).astimezone().isoformat(timespec="seconds")
    return result


def snapshot(usage: dict) -> dict:
    """Compact per-window record kept with a job to compute its cost later."""
    multi = len(_buckets(usage)) > 1
    return {
        "measured_at": usage.get("measured_at"),
        "windows": {
            f"{b.get('id')}/{w.get('kind')}": {
                # Same naming as evaluate(): bucket prefix only when there are several buckets.
                "label": f"{b.get('name') or b.get('id')}/{w.get('label')}" if multi else w.get("label"),
                "used_percent": w.get("used_percent"),
                "resets_at_unix": w.get("resets_at_unix"),
            }
            for b, w in _windows(usage)
        },
    }


def cost(before: dict, after: dict) -> dict:
    """Percent-point change per window between two snapshots.

    Approximate by nature: it also includes any other Codex use in between.
    A window that reset in between has no meaningful delta and is reported as such.
    """
    out: dict[str, Any] = {}
    for key, b in (before.get("windows") or {}).items():
        a = (after.get("windows") or {}).get(key)
        label = b.get("label") or key
        if label in out:  # never let two windows overwrite each other
            label = key
        if not a or not isinstance(a.get("used_percent"), (int, float)) \
                or not isinstance(b.get("used_percent"), (int, float)):
            out[label] = "unknown"
            continue
        rb, ra = b.get("resets_at_unix"), a.get("resets_at_unix")
        if rb and ra and abs(float(ra) - float(rb)) > _SAME_WINDOW_TOLERANCE_SEC:
            out[label] = "window reset during the job"
            continue
        out[label] = f"+{max(0.0, a['used_percent'] - b['used_percent']):g}%"
    return out
