"""Offline unit tests (no Codex calls).

    E:\\Development\\AI\\Claude\\Utils\\Bin\\Windows\\Python3\\python.exe tests\\unit_test.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = Path(tempfile.mkdtemp(prefix="task-delegator-unit-"))
os.environ["TASK_DELEGATOR_STATE_DIR"] = str(STATE)
sys.path.insert(0, str(ROOT))

from task_delegator import codex, jobs, procs  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str, extra: object = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {extra}" if (extra and not cond) else ""))
    if not cond:
        failures.append(label)


def fake_job(meta: dict, events: list[dict] | None = None, exit_info: dict | None = None) -> str:
    job_id, d = jobs.create_job("codex", "test prompt", {"mode": "read-only", "cwd": str(STATE), **meta})
    jobs.update_meta(job_id, **meta)
    (d / "stdout.log").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events or []),
                                  encoding="utf-8")
    if exit_info is not None:
        jobs.write_json(d / "exit.json", exit_info)
    return job_id


def spawn_sleeper(seconds: int = 60) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"],
                            creationflags=procs.POPEN_FLAGS)


# --- parse_events --------------------------------------------------------------------
evs = [
    {"type": "thread.started", "thread_id": "t1"},
    {"type": "item.started", "item": {"id": "a", "type": "file_change", "status": "in_progress",
                                      "changes": [{"path": "ok.py", "kind": "add"}]}},
    {"type": "item.completed", "item": {"id": "a", "type": "file_change", "status": "completed",
                                        "changes": [{"path": "ok.py", "kind": "add"}]}},
    {"type": "item.started", "item": {"id": "b", "type": "file_change", "status": "in_progress",
                                      "changes": [{"path": "bad.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "b", "type": "file_change", "status": "failed",
                                        "changes": [{"path": "bad.py", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "line\u2028sep\x85end"}},
    {"type": "turn.completed", "usage": {"output_tokens": 1}},
]
log = STATE / "events.jsonl"
log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")
ev = codex.parse_events(log)
check([f["path"] for f in ev["file_changes"]] == ["ok.py"], "only completed patches count as changed", ev["file_changes"])
check([f["path"] for f in ev["failed_file_changes"]] == ["bad.py"], "failed patch reported separately", ev)
check(ev["messages"] == ["line\u2028sep\x85end"], "U+2028/U+0085 inside JSON do not split events", ev["messages"])
check(ev["turn_completed"] and ev["thread_id"] == "t1", "turn + thread parsed")

# --- process identity ----------------------------------------------------------------
p = spawn_sleeper()
created = procs.process_created(p.pid)
check(created is not None, "process_created reads a live process")
check(procs.pid_alive(p.pid, None, created), "pid_alive true for the same process")
check(not procs.pid_alive(p.pid, None, created + 1), "pid_alive false when creation time differs (PID reuse)")

# cancel must not kill a process that merely reuses the job's PID
reused = fake_job({"pid": p.pid, "pid_created": created + 1, "image": "python.exe"})
ok, msg = jobs.cancel(reused)
check(not ok and p.poll() is None, "cancel refuses to kill a recycled PID", msg)
check(codex.job_status(reused)[0] == "cancelled", "that job ends up cancelled, not running")
p.kill()
p.wait()

# --- unobserved finish gets finalized once -------------------------------------------
dead = spawn_sleeper(0)
dead.wait()
done_events = [{"type": "thread.started", "thread_id": "t2"},
               {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "done"}},
               {"type": "turn.completed", "usage": {}}]
orphan = fake_job({"pid": dead.pid, "pid_created": 1, "image": "codex.exe"}, done_events)
st = codex.job_status(orphan)[0]
rec = jobs.read_json(jobs.job_dir(orphan) / "exit.json")
check(st == "completed", "unwatched job that finished reads as completed", st)
check(bool(rec and rec.get("unobserved")), "its end is persisted to exit.json", rec)

lost = fake_job({"pid": dead.pid, "pid_created": 1, "image": "codex.exe"}, [{"type": "thread.started", "thread_id": "t3"}])
check(codex.job_status(lost)[0] == "lost", "unwatched job without turn.completed reads as lost")

# old jobs without exit.json are cleaned up too
old = fake_job({"pid": dead.pid, "pid_created": 1, "image": "codex.exe"})
(jobs.job_dir(old) / "exit.json").unlink(missing_ok=True)
past = 1_000_000_000
os.utime(jobs.job_dir(old) / "job.json", (past, past))
jobs.cleanup_old(14)
check(not jobs.job_dir(old).exists(), "cleanup_old removes stale never-finalized jobs")

# --- resume argv carries add_dirs as valid TOML --------------------------------------
dirs = [r"E:\shared dir\R&D", "C:\\Пример\\x"]
argv = codex.build_resume_argv("codex.exe", "t1", "write", Path("o.txt"), None, None, dirs)
ov = next(a for a in argv if a.startswith("sandbox_workspace_write.writable_roots="))
parsed = tomllib.loads(ov.replace("sandbox_workspace_write.writable_roots", "x", 1))["x"]
check(parsed == dirs, "writable_roots override is valid TOML with Windows paths", parsed)
check(argv[-2:] == ["t1", "-"], "session id and stdin marker come last")
ro = codex.build_resume_argv("codex.exe", "t1", "read-only", Path("o.txt"), None, None, dirs)
check(not any("writable_roots" in a for a in ro), "read-only resume grants no extra roots")

# --- PATH shim is not launched through cmd.exe ---------------------------------------
shim_dir = Path(tempfile.mkdtemp(prefix="task-delegator-shim-"))
(shim_dir / "codex.cmd").write_text("@echo off\r\n", encoding="ascii")
saved = {k: os.environ.get(k) for k in ("PATH", "LOCALAPPDATA", "TASK_DELEGATOR_CODEX_EXE")}
os.environ.pop("TASK_DELEGATOR_CODEX_EXE", None)
os.environ["LOCALAPPDATA"] = str(shim_dir / "nolocal")
os.environ["PATH"] = str(shim_dir)
exe, how = codex.find_codex()
check(exe is None and "shim" in how, "a bare codex.cmd shim is refused", (exe, how))
vend = shim_dir / "node_modules" / "@openai" / "codex" / "vendor" / "x86_64-pc-windows-msvc" / "codex"
vend.mkdir(parents=True)
(vend / "codex.exe").write_bytes(b"")
exe, how = codex.find_codex()
check(exe is not None and exe.endswith("codex.exe") and "vendored" in how, "npm vendored codex.exe is used", (exe, how))
for k, v in saved.items():
    if v is None:
        os.environ.pop(k, None)
    else:
        os.environ[k] = v

# --- server-side validation ----------------------------------------------------------
import server  # noqa: E402

for bad in ['high" sandbox_mode="danger-full-access', "a b", ""]:
    try:
        server._check_token("effort", bad)
        check(False, f"rejects effort {bad!r}")
    except Exception as e:  # ToolError
        check("invalid effort" in str(e), f"rejects effort {bad!r}")
server._check_token("effort", "xhigh")
check(True, "accepts a normal effort value")

# --- limits: reading the usage script ------------------------------------------------
from task_delegator import limits  # noqa: E402

FAKE_DIR = Path(tempfile.mkdtemp(prefix="task-delegator-usage-"))
FAKE_SCRIPT = FAKE_DIR / "fake_usage.py"
FAKE_STATE = FAKE_DIR / "state.json"
FAKE_SCRIPT.write_text(
    "import json, sys, pathlib\n"
    f"st = json.loads(pathlib.Path(r'{FAKE_STATE}').read_text(encoding='utf-8'))\n"
    "if st.get('exit'):\n"
    "    sys.stderr.write(st.get('stderr', 'boom')); sys.exit(st['exit'])\n"
    "sys.stdout.write(st['stdout'])\n",
    encoding="utf-8",
)
os.environ["TASK_DELEGATOR_CODEX_USAGE_SCRIPT"] = str(FAKE_SCRIPT)
NOW = 1_800_000_000


def usage_json(p5: float, p7: float, r5: int = NOW + 3600, r7: int = NOW + 86400 * 3,
               reached=None, allowed=True, schema=1) -> dict:
    return {
        "schema": schema, "measured_at": "2027-01-15T10:00:00+02:00", "plan_type": "plus",
        "ordinary_usage_allowed": allowed, "codex_exe": "x", "source": "s", "reset_credits_available": 0,
        "buckets": [{"id": "codex", "name": "codex", "limit_reached": reached, "windows": [
            {"kind": "primary", "label": "5h", "window_minutes": 300, "used_percent": p5,
             "remaining_percent": 100 - p5, "resets_at": "2027-01-15T11:00:00+02:00",
             "resets_at_unix": r5, "resets_in_sec": 3600},
            {"kind": "secondary", "label": "7d", "window_minutes": 10080, "used_percent": p7,
             "remaining_percent": 100 - p7, "resets_at": "2027-01-18T10:00:00+02:00",
             "resets_at_unix": r7, "resets_in_sec": 259200},
        ]}],
    }


def fake(stdout=None, exit=0, stderr=""):
    FAKE_STATE.write_text(json.dumps({"stdout": stdout or "", "exit": exit, "stderr": stderr}),
                          encoding="utf-8")


def unavailable(label: str) -> None:
    try:
        limits.read()
        check(False, label)
    except limits.LimitsUnavailable as e:
        check(True, label)
        print("     ", e)


fake(json.dumps(usage_json(10, 20)))
check(limits.read()["schema"] == 1, "limits.read returns the script's JSON")
fake(exit=1, stderr="Codex limits unavailable: not signed in")
unavailable("exit 1 -> LimitsUnavailable with the script's reason")
fake("not json")
unavailable("garbage stdout -> LimitsUnavailable")
fake(json.dumps(usage_json(10, 20, schema=2)))
unavailable("unknown schema -> LimitsUnavailable")
os.environ["TASK_DELEGATOR_CODEX_USAGE_SCRIPT"] = str(FAKE_DIR / "missing.py")
unavailable("missing script -> LimitsUnavailable")
os.environ["TASK_DELEGATOR_CODEX_USAGE_SCRIPT"] = str(FAKE_SCRIPT)

# --- limits: verdicts ----------------------------------------------------------------
v = limits.evaluate(usage_json(10, 20))
check(v["state"] == "ok" and not v["reasons"], "10%/20% -> ok", v)
check("5h: 10%" in v["summary"] and "7d: 20%" in v["summary"], "summary names both windows", v["summary"])
v = limits.evaluate(usage_json(91, 20))
check(v["state"] == "warn" and "5h" in v["reasons"][0], "5h 91% -> warn", v)
v = limits.evaluate(usage_json(96, 20))
check(v["state"] == "pause" and v.get("resume_after"), "5h 96% -> pause with resume_after", v)
check(v["resume_after"] and v["resume_after"].startswith(
    __import__("datetime").datetime.fromtimestamp(NOW + 3600).astimezone().isoformat()[:16]),
    "pause resumes at the 5h reset, not the weekly one", v.get("resume_after"))
v = limits.evaluate(usage_json(50, 99))
check(v["state"] == "pause" and "7d" in v["reasons"][0], "7d 99% -> pause", v)
v = limits.evaluate(usage_json(50, 20, reached="rate_limit_reached"))
check(v["state"] == "pause", "limit_reached flag -> pause", v)
check(v["resume_after"].startswith(
    __import__("datetime").datetime.fromtimestamp(NOW + 3600).astimezone().isoformat()[:16]),
    "limit_reached without a window over threshold waits for the NEAREST reset", v.get("resume_after"))
check(sum("limit reached" in r for r in v["reasons"]) == 1, "limit_reached reported once per bucket", v["reasons"])
v = limits.evaluate(usage_json(10, 20, allowed=False))
check(v["state"] == "pause" and "resume_after" not in v, "ordinary_usage_allowed=false -> pause", v)
v = limits.evaluate(usage_json(91, 20), {"primary": {"warn": 95, "pause": 99}, "secondary": {}})
check(v["state"] == "ok", "custom thresholds are honoured", v)

# --- limits: cost of a job -----------------------------------------------------------
b = limits.snapshot(usage_json(10, 20))
c = limits.cost(b, limits.snapshot(usage_json(13, 21)))
check(c == {"5h": "+3%", "7d": "+1%"}, "cost = percentage-point delta per window", c)
c = limits.cost(b, limits.snapshot(usage_json(2, 21, r5=NOW + 3600 + 5 * 3600)))
check(c["5h"] == "window reset during the job" and c["7d"] == "+1%", "a reset window is not reported as negative", c)
c = limits.cost(b, limits.snapshot(usage_json(13, 21, r5=NOW + 3600 + 3)))
check(c["5h"] == "+3%", "a few seconds of reset jitter is still the same window", c)

# --- server gate ---------------------------------------------------------------------
fake(json.dumps(usage_json(96, 20)))
try:
    server._limits_gate("codex.exe", False)
    check(False, "pause refuses to start")
except Exception as e:
    check("exhausted" in str(e) and "resets at" in str(e), "pause refuses to start, says when it resets", e)
info, snap = server._limits_gate("codex.exe", True)
check(info.get("overridden") and snap, "ignore_limits starts anyway and records the override", info)
fake(json.dumps(usage_json(92, 20)))
info, snap = server._limits_gate("codex.exe", False)
check(info["state"] == "warn" and "note" in info and snap, "warn starts with a note", info)
fake(exit=1, stderr="no login")
info, snap = server._limits_gate("codex.exe", False)
check(info["state"] == "unknown" and snap is None and "no login" in info["note"],
      "unavailable limits fail open and say so", info)

# --- limits_cost on a finished job ---------------------------------------------------
fake(json.dumps(usage_json(13, 21)))
dead2 = spawn_sleeper(0)
dead2.wait()
job = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe", "limits_before": limits.snapshot(usage_json(10, 20))},
               done_events, {"exit_code": 0, "finished_at": jobs.now_iso()})
view = codex.job_view(job, "full")
check(view.get("limits_cost", {}).get("cost") == {"5h": "+3%", "7d": "+1%"}, "finished job reports limits_cost", view.get("limits_cost"))
check((jobs.job_dir(job) / "limits_after.json").exists(), "the after-reading is cached in the job dir")
fake(exit=1, stderr="should not be called")
view = codex.job_view(job, "full")
check(view.get("limits_cost", {}).get("cost") == {"5h": "+3%", "7d": "+1%"}, "later views reuse the cached reading", view.get("limits_cost"))
job2 = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe", "limits_before": limits.snapshot(usage_json(10, 20))},
                done_events, {"exit_code": 0, "finished_at": jobs.now_iso()})
view = codex.job_view(job2, "full")
check(view["limits_cost"]["cost"] == "unknown" and not (jobs.job_dir(job2) / "limits_after.json").exists(),
      "a failed after-reading is reported and not cached", view.get("limits_cost"))
job3 = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe"}, done_events,
                {"exit_code": 0, "finished_at": jobs.now_iso()})
check("limits_cost" not in codex.job_view(job3, "full"), "jobs started without a reading report no cost")

# =====================================================================================
# Regression tests for the second review (limits integration)
# =====================================================================================
import asyncio  # noqa: E402
import time as _time  # noqa: E402
from datetime import datetime as _dt, timedelta as _td  # noqa: E402
from unittest import mock  # noqa: E402

# --- limit_reached on a bucket without windows ----------------------------------------
u = {"schema": 1, "ordinary_usage_allowed": True,
     "buckets": [{"id": "codex", "name": "codex", "limit_reached": "rate_limit_reached", "windows": []}]}
check(limits.evaluate(u)["state"] == "pause", "limit_reached on a bucket with no windows -> pause")
u = usage_json(10, 20)
u["buckets"].append({"id": "other", "name": "other", "limit_reached": "rate_limit_reached", "windows": []})
check(limits.evaluate(u)["state"] == "pause", "a second bucket with only the limit_reached flag -> pause")

# --- several buckets: cost keys do not collide ----------------------------------------
def two_buckets(p_main, p_other):
    u = usage_json(p_main, p_main)
    other = json.loads(json.dumps(u["buckets"][0]))
    other.update(id="other", name="other")
    for w in other["windows"]:
        w["used_percent"] = p_other
    u["buckets"].append(other)
    return u


c = limits.cost(limits.snapshot(two_buckets(30, 0)), limits.snapshot(two_buckets(42, 0)))
check(c.get("codex/5h") == "+12%" and c.get("other/5h") == "+0%", "multi-bucket cost keeps each bucket", c)

# --- DST: the reset time is shown from the unix time, not the stale ISO offset --------
w = {"resets_at": "2027-03-30T19:13:00+02:00", "resets_at_unix": 1806423180}
expected = _dt.fromtimestamp(1806423180).astimezone().strftime("%d.%m %H:%M")
check(limits._fmt_reset(w) == expected, "reset time uses the unix timestamp (DST-safe)", (limits._fmt_reset(w), expected))
a = limits._fmt_reset({"resets_at_unix": 1806423180 - 1})   # hh:mm:59 of the previous minute
b = limits._fmt_reset({"resets_at_unix": 1806423180})
c29 = limits._fmt_reset({"resets_at_unix": 1806423180 - 31})  # hh:mm:29 of the previous minute
check(a == b != c29, "reset time is rounded to the nearest minute (no 19:12/19:13 flip)", (a, b, c29))

# --- race: capacity and session checks are atomic with job creation -------------------
fake(json.dumps(usage_json(10, 20)))
launched: list[str] = []


def slow_read(exe=None):
    _time.sleep(0.4)  # the real script takes ~1 s
    return usage_json(10, 20)


def fake_create(prompt, cwd, mode, model, effort, add_dirs, resume_thread, parent_job, exe, info, snap, opts=None):
    launched.append(resume_thread or "new")
    return {"job_id": f"fake-{len(launched)}", "status": "running", "limits": info}


async def race_start(n: int):
    return await asyncio.gather(*(server.codex_start(task="t", cwd=str(STATE)) for _ in range(n)),
                                return_exceptions=True)


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda: len(launched)), \
        mock.patch.object(server, "MAX_RUNNING", 3):
    res = asyncio.run(race_start(5))
ok = [r for r in res if isinstance(r, dict)]
check(len(ok) == 3 and len(launched) == 3, "5 parallel codex_start with limit 3 -> exactly 3 start", [type(r).__name__ for r in res])

# Two concurrent replies on the same finished job: only one may resume the session.
launched.clear()
parent = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe"}, done_events,
                  {"exit_code": 0, "finished_at": jobs.now_iso()})


async def race_reply():
    return await asyncio.gather(*(server.codex_reply(job_id=parent, message="m") for _ in range(2)),
                                return_exceptions=True)


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda: 0), \
        mock.patch.object(server, "_job_running_on_thread",
                          lambda t: "fake-busy" if t in launched else None):
    res = asyncio.run(race_reply())
check(sum(isinstance(r, dict) for r in res) == 1 and launched == ["t2"],
      "2 parallel codex_reply on one session -> exactly 1 resumes it", [str(r)[:60] for r in res])

# --- cancellation during the limits read creates nothing ------------------------------
launched.clear()


async def cancel_midway():
    t = asyncio.create_task(server.codex_start(task="t", cwd=str(STATE)))
    await asyncio.sleep(0.1)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.6)  # let the worker thread finish its read


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda: 0):
    asyncio.run(cancel_midway())
check(launched == [], "a codex_start cancelled during the limits read launches nothing", launched)

# --- the after-reading is taken when the job exits -------------------------------------
fake(json.dumps(usage_json(14, 21)))
jid, d = jobs.create_job("codex", "p", {"mode": "read-only", "cwd": str(STATE), "codex_exe": None,
                                        "limits_before": limits.snapshot(usage_json(10, 20))})
jobs.launch(jid, [sys.executable, "-c", "pass"], str(STATE), dict(os.environ),
            on_exit=codex.capture_limits_after)
for _ in range(100):
    if (d / "limits_after.json").exists():
        break
    _time.sleep(0.1)
check((d / "limits_after.json").exists(), "on_exit hook records limits right when the job ends")
fake(exit=1, stderr="must not be called")
lc = codex.limits_cost(jobs.load_meta(jid), d)
check(lc and lc["cost"] == {"5h": "+4%", "7d": "+1%"}, "limits_cost uses the reading taken at exit", lc)

# --- a late first look does not attribute later usage to the job ----------------------
fake(json.dumps(usage_json(60, 30)))
late = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe",
                 "limits_before": limits.snapshot(usage_json(10, 20))}, done_events,
                {"exit_code": 0, "finished_at": (_dt.now().astimezone() - _td(minutes=30)).isoformat()})
lc = codex.limits_cost(jobs.load_meta(late), jobs.job_dir(late))
check(lc["cost"] == "unknown" and not (jobs.job_dir(late) / "limits_after.json").exists(),
      "a reading 30 min after the end is refused (cost unknown)", lc)

# --- codex_job.py: setup errors fail before waiting; pause waits are floored ----------
RUNNER = ROOT / "scripts" / "codex_job.py"
task_file = STATE / "task.md"
task_file.write_text("x", encoding="utf-8")
t0 = _time.monotonic()
r = subprocess.run([sys.executable, str(RUNNER), "--task-file", str(task_file), "--cwd", r"C:\no\such\dir",
                    "--start-at", "23:59", "--no-limits"], capture_output=True, text=True, encoding="utf-8")
check(r.returncode == 1 and _time.monotonic() - t0 < 20 and "setup error" in r.stdout,
      "codex_job: a bad cwd fails immediately, not after --start-at", r.stdout[-200:])
r = subprocess.run([sys.executable, str(RUNNER), "--task-file", str(task_file), "--cwd", str(STATE),
                    "--effort", 'x" y', "--dry-run", "--no-limits"], capture_output=True, text=True, encoding="utf-8")
check(r.returncode == 1 and "invalid effort" in r.stdout, "codex_job: --dry-run rejects what _launch rejects", r.stdout[-200:])

sys.path.insert(0, str(ROOT / "scripts"))
import codex_job  # noqa: E402

past = usage_json(100, 20, r5=int(_time.time()) - 600)
targets: list = []
with mock.patch.object(codex_job.limits, "read", lambda exe=None: past), \
        mock.patch.object(codex_job, "sleep_until", lambda t, why: targets.append(t)), \
        mock.patch.object(codex_job, "log", lambda m: None):
    try:
        codex_job.gate_on_limits({"primary": None, "secondary": None}, 120, max_rounds=2)
    except SystemExit:
        pass
check(len(targets) == 2 and all(t >= _dt.now() + _td(seconds=50) for t in targets),
      "a reset already in the past still waits >= 60 s per round", targets)

# --- model / effort validation against Codex's model cache ----------------------------
cache_home = Path(tempfile.mkdtemp(prefix="codex-home-"))
(cache_home / "models_cache.json").write_text(json.dumps({"models": [
    {"slug": "gpt-6.1-sol", "visibility": "list", "description": "workhorse",
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max", "ultra")]},
    {"slug": "gpt-6-luna", "visibility": "list", "description": "cheap",
     "supported_reasoning_levels": [{"effort": e} for e in ("low", "medium", "high", "xhigh", "max")]},
    {"slug": "hidden-x", "visibility": "hide", "supported_reasoning_levels": []},
]}), encoding="utf-8")
saved_home = os.environ.get("CODEX_HOME")
os.environ["CODEX_HOME"] = str(cache_home)
check(codex.check_model_effort("gpt-6.1-sol", "high") is None, "valid model + effort passes")
check(codex.check_model_effort(None, None) is None, "no model / effort passes (Codex defaults)")
e = codex.check_model_effort("gpt-9", "low")
check(e and "unknown Codex model" in e and "gpt-6-luna" in e and "hidden-x" not in e,
      "unknown model is rejected with the listed models", e)
e = codex.check_model_effort("gpt-6-luna", "ultra")
check(e and "does not support effort 'ultra'" in e, "unsupported effort for that model is rejected", e)
e = codex.check_model_effort(None, "turbo")
check(e and "unknown effort" in e, "unknown effort without a model is rejected", e)
try:
    server._prepare("gpt-6-luna", "ultra")
    check(False, "server refuses an invalid model/effort before starting")
except Exception as ex:
    check("does not support" in str(ex), "server refuses an invalid model/effort before starting", ex)
os.environ["CODEX_HOME"] = str(cache_home / "nowhere")
check(codex.check_model_effort("anything", "whatever") is None, "missing model cache -> no validation (fail open)")
if saved_home is None:
    os.environ.pop("CODEX_HOME", None)
else:
    os.environ["CODEX_HOME"] = saved_home

# --- worker B with a fake claude ------------------------------------------------------
from task_delegator import claude_worker  # noqa: E402

WDIR = Path(tempfile.mkdtemp(prefix="task-delegator-worker-"))
WSTATE = WDIR / "behaviour.json"
fake_py = WDIR / "fake_claude.py"
fake_py.write_text(r'''
import json, os, sys, time, pathlib
st = json.loads(pathlib.Path(os.environ["FAKE_CLAUDE_STATE"]).read_text(encoding="utf-8"))
prompt = sys.stdin.read()
rec = {"argv": sys.argv[1:], "prompt": prompt, "config_dir": os.environ.get("CLAUDE_CONFIG_DIR"),
       "wf_agents": os.environ.get("CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS"),
       "leaked": sorted(k for k in os.environ if k.upper() in ("CLAUDECODE", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"))}
pathlib.Path(os.environ["FAKE_CLAUDE_STATE"] + ".calls").open("a", encoding="utf-8").write(json.dumps(rec) + "\n")
def emit(o): print(json.dumps(o, ensure_ascii=False), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "s-123", "model": "claude-x"})
if st.get("sleep"): time.sleep(st["sleep"])
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "работаю"},
     {"type": "tool_use", "name": "Edit", "input": {"file_path": "C:/p/a.py"}},
     {"type": "tool_use", "name": "Bash", "input": {"command": "git status"}}]}})
ok = not st.get("fail")
emit({"type": "result", "subtype": "success" if ok else "error_during_execution", "is_error": not ok,
      "result": "ГОТОВО: " + prompt.strip()[:40], "session_id": "s-123", "num_turns": 3,
      "total_cost_usd": 0.01, "usage": {"input_tokens": 5, "output_tokens": 7},
      "permission_denials": [{"tool_name": "Write", "tool_input": {"file_path": "D:/x"}}]})
sys.exit(0 if ok else 1)
''', encoding="utf-8")
fake_cmd = WDIR / "claude_fake.cmd"
fake_cmd.write_text(f'@"{sys.executable}" "{fake_py}" %*\r\n', encoding="ascii")
os.environ.update({"TASK_DELEGATOR_CLAUDE_EXE": str(fake_cmd), "TASK_DELEGATOR_WORKER_CONFIG_DIR": str(WDIR / "cfg"),
                   "FAKE_CLAUDE_STATE": str(WSTATE), "CLAUDECODE": "1"})


def wcalls() -> list[dict]:
    p = Path(str(WSTATE) + ".calls")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


WSTATE.write_text(json.dumps({}), encoding="utf-8")
st = asyncio.run(server.worker_start(task="Сделай X", cwd=str(STATE), model="sonnet", effort="low"))
res = asyncio.run(server.worker_wait(job_id=st["job_id"], timeout_sec=30))
check(res["status"] == "completed" and res["final_message"] == "ГОТОВО: Сделай X", "worker job completes with B's answer", res)
check(res["session_id"] == "s-123" and res["files_edited"] == ["C:/p/a.py"], "session id and edited files parsed", res)
check(res["permission_denials"][0]["tool"] == "Write", "permission denials are reported", res.get("permission_denials"))
call = wcalls()[-1]
check("dontAsk" in call["argv"] and "Bash" in call["argv"][call["argv"].index("--disallowedTools"):],
      "read-only: dontAsk + edits/shell disallowed", call["argv"])
check(call["argv"][call["argv"].index("--model") + 1] == "sonnet" and "--effort" in call["argv"], "model/effort passed", call["argv"])
check(call["prompt"].strip() == "Сделай X", "prompt goes in via stdin (UTF-8)", call["prompt"])
check(call["config_dir"] == str(WDIR / "cfg") and not call["leaked"], "B gets its own config dir and no lead-session env", call)

rp = asyncio.run(server.worker_reply(job_id=st["job_id"], message="Дальше"))
res2 = asyncio.run(server.worker_wait(job_id=rp["job_id"], timeout_sec=30))
call = wcalls()[-1]
check(res2["status"] == "completed" and call["argv"][call["argv"].index("--resume") + 1] == "s-123",
      "worker_reply resumes the same session", call["argv"])
check(res2.get("continues_job") == st["job_id"], "reply is linked to its parent job", res2)

wj = asyncio.run(server.worker_start(task="пиши", cwd=str(STATE), mode="write", model="opus"))
asyncio.run(server.worker_wait(job_id=wj["job_id"], timeout_sec=30))
call = wcalls()[-1]
check("acceptEdits" in call["argv"] and "Bash" in call["argv"] and "--disallowedTools" not in call["argv"],
      "write: acceptEdits, edits and shell allowed", call["argv"])
a = call["argv"]
check(a[a.index("--effort") + 1] == "ultracode" and a[a.index("--settings") + 1] == str(claude_worker.WORKFLOW_SETTINGS)
      and call["wf_agents"] == "3", "write jobs default to ultracode + workflow settings + 3 concurrent agents", a)
check(json.loads(claude_worker.WORKFLOW_SETTINGS.read_text(encoding="utf-8"))["enableWorkflows"] is True,
      "workflow settings file enables workflows")
first = wcalls()[0]["argv"]
check("--settings" not in first and first[first.index("--effort") + 1] == "low",
      "read-only jobs never get ultracode", first)
nj = asyncio.run(server.worker_start(task="без ultracode", cwd=str(STATE), mode="write", ultracode=False))
asyncio.run(server.worker_wait(job_id=nj["job_id"], timeout_sec=30))
a = wcalls()[-1]["argv"]
check("--settings" not in a and a[a.index("--effort") + 1] == "xhigh" and a[a.index("--model") + 1] == "claude-opus-5-5",
      "defaults: claude-opus-5-5 + xhigh when ultracode is off", a)
check(wcalls()[-1]["wf_agents"] is None, "no workflow agent cap without ultracode")

WSTATE.write_text(json.dumps({"fail": True}), encoding="utf-8")
fj = asyncio.run(server.worker_start(task="упади", cwd=str(STATE)))
fres = asyncio.run(server.worker_wait(job_id=fj["job_id"], timeout_sec=30))
check(fres["status"] == "failed" and fres["result"]["is_error"] is True, "a failed B run is reported as failed", fres)

WSTATE.write_text(json.dumps({"sleep": 30}), encoding="utf-8")
sj = asyncio.run(server.worker_start(task="долго", cwd=str(STATE)))
_time.sleep(2)
cres = asyncio.run(server.worker_cancel(job_id=sj["job_id"]))
check(cres["status"] == "cancelled", "worker_cancel stops a running B job", cres)
try:
    asyncio.run(server.worker_status(job_id=st["job_id"].replace("worker-", "codex-")))
    check(False, "codex ids are rejected by worker tools")
except Exception as e:
    check("not a worker job" in str(e), "codex ids are rejected by worker tools", e)
lst = asyncio.run(server.worker_status())
check(len(lst["jobs"]) >= 4 and all(j["job_id"].startswith("worker-") for j in lst["jobs"]), "worker_status lists only worker jobs", lst)
os.environ.pop("CLAUDECODE", None)

print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
