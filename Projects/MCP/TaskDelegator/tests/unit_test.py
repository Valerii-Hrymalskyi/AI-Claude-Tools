"""Offline unit tests (fake Codex and Claude; no installed CLIs or accounts needed).

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe tests\\unit_test.py
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
# Isolate settings before importing adapters/server, including in runner subprocesses.
sys.dont_write_bytecode = True
for key in list(os.environ):
    if key.startswith("TASK_DELEGATOR_"):
        os.environ.pop(key)
BIN = STATE / "bin"
BIN.mkdir()
ACCOUNTS = STATE / "accounts"
AGENTS_FILE = STATE / "agents.json"
for kind, names in (("codex", ("work", "old")), ("claude", ("test", "other", "limited"))):
    for name in names:
        (ACCOUNTS / f".{kind}-accounts" / name).mkdir(parents=True)
DEFAULT_CODEX_HOME = STATE / "default-codex"
DEFAULT_CODEX_HOME.mkdir()
FAKE_CODEX = BIN / "codex_fake.cmd"
FAKE_CLAUDE = BIN / "claude_fake.cmd"
codex_script = BIN / "fake_codex.py"
codex_script.write_text('''
import json, os, pathlib, sys
if "--version" in sys.argv:
    print("codex fake 1.0")
elif sys.argv[1:3] == ["login", "status"]:
    print("fake account signed in")
else:
    prompt = sys.stdin.read()
    argv = sys.argv[1:]
    rec = {"argv": argv, "prompt": prompt, "home": os.environ.get("CODEX_HOME")}
    with open(os.environ["FAKE_CODEX_CALLS"], "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\\n")
    session = argv[-2] if "resume" in argv else "fake-codex-session"
    pathlib.Path(argv[argv.index("-o") + 1]).write_text("done: " + prompt, encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": session}))
    print(json.dumps({"type": "turn.completed", "usage": {}}))
''', encoding="utf-8")
FAKE_CODEX.write_text(f'@"{sys.executable}" "{codex_script}" %*\r\n', encoding="ascii")
# Replaced with the streaming fake below before any Claude job is launched.
FAKE_CLAUDE.write_text("@echo off\r\n", encoding="ascii")
AGENTS_FILE.write_text('{"agents": {}}', encoding="utf-8")
os.environ.update({
    "TASK_DELEGATOR_STATE_DIR": str(STATE),
    "TASK_DELEGATOR_ACCOUNTS_ROOT": str(ACCOUNTS),
    "TASK_DELEGATOR_AGENTS_FILE": str(AGENTS_FILE),
    "TASK_DELEGATOR_CODEX_EXE": str(FAKE_CODEX),
    "TASK_DELEGATOR_CLAUDE_EXE": str(FAKE_CLAUDE),
    "CODEX_HOME": str(DEFAULT_CODEX_HOME),
    "FAKE_CODEX_CALLS": str(STATE / "codex.calls"),
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
})
sys.path.insert(0, str(ROOT))

from task_delegator import agents, claude, codex, jobs, procs  # noqa: E402

failures: list[str] = []
checks = 0


def check(cond: bool, label: str, extra: object = "") -> None:
    global checks
    checks += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  -> {extra}" if (extra and not cond) else ""))
    if not cond:
        failures.append(label)


def fake_job(meta: dict, events: list[dict] | None = None, exit_info: dict | None = None, provider: str = "codex") -> str:
    job_id, d = jobs.create_job(provider, "test prompt", {"mode": "read-only", "cwd": str(STATE), **meta})
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

CODEX = server._agent("codex")
CLAUDE = server._agent("claude:test")
server.POLL_SEC = 0.05  # Poll fake jobs promptly; production polling is unrelated to these checks.

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
    server._limits_gate(CODEX, str(FAKE_CODEX), False)
    check(False, "pause refuses to start")
except Exception as e:
    check("exhausted" in str(e) and "resets at" in str(e), "pause refuses to start, says when it resets", e)
info, snap = server._limits_gate(CODEX, str(FAKE_CODEX), True)
check(info.get("overridden") and snap, "ignore_limits starts anyway and records the override", info)
fake(json.dumps(usage_json(92, 20)))
info, snap = server._limits_gate(CODEX, str(FAKE_CODEX), False)
check(info["state"] == "warn" and "note" in info and snap, "warn starts with a note", info)
fake(exit=1, stderr="no login")
info, snap = server._limits_gate(CODEX, str(FAKE_CODEX), False)
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


def slow_read(exe=None, env=None):
    _time.sleep(0.4)  # the real script takes ~1 s
    return usage_json(10, 20)


def fake_create(agent, exe, prompt, cwd, mode, model, effort, add_dirs, options,
                resume_session, parent_job, limits_info, limits_before):
    launched.append(resume_session or "new")
    return {"job_id": f"fake-{len(launched)}", "status": "running", "limits": limits_info}


async def race_start(n: int):
    return await asyncio.gather(*(server.task_start(agent="codex", task="t", cwd=str(STATE)) for _ in range(n)),
                                return_exceptions=True)


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda aid: len(launched)), \
        mock.patch.object(server, "_max_jobs", lambda agent: 3):
    res = asyncio.run(race_start(5))
ok = [r for r in res if isinstance(r, dict)]
check(len(ok) == 3 and len(launched) == 3, "5 parallel task_start with limit 3 -> exactly 3 start", [type(r).__name__ for r in res])

# Two concurrent replies on the same finished job: only one may resume the session.
launched.clear()
parent = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe"}, done_events,
                  {"exit_code": 0, "finished_at": jobs.now_iso()})


async def race_reply():
    return await asyncio.gather(*(server.task_reply(job_id=parent, message="m") for _ in range(2)),
                                return_exceptions=True)


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda aid: 0), \
        mock.patch.object(server, "_session_busy",
                          lambda aid, t: "fake-busy" if t in launched else None):
    res = asyncio.run(race_reply())
check(sum(isinstance(r, dict) for r in res) == 1 and launched == ["t2"],
      "2 parallel task_reply on one session -> exactly 1 resumes it", [str(r)[:60] for r in res])

# --- cancellation during the limits read creates nothing ------------------------------
launched.clear()


async def cancel_midway():
    t = asyncio.create_task(server.task_start(agent="codex", task="t", cwd=str(STATE)))
    await asyncio.sleep(0.1)
    t.cancel()
    try:
        await t
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0.6)  # let the worker thread finish its read


with mock.patch.object(limits, "read", slow_read), \
        mock.patch.object(server, "_create_and_launch", fake_create), \
        mock.patch.object(server, "_running_count", lambda aid: 0):
    asyncio.run(cancel_midway())
check(launched == [], "a task_start cancelled during the limits read launches nothing", launched)

# --- the after-reading is taken when the job exits -------------------------------------
fake(json.dumps(usage_json(14, 21)))
jid, d = jobs.create_job("codex", "p", {"mode": "read-only", "cwd": str(STATE), "exe": str(FAKE_CODEX),
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

# --- the exit hook and the first look race for the after-reading (CI, 2026-11-19) ----------
import threading as _threading  # noqa: E402

race_reads: list[int] = []


def slow_read(exe=None, env=None):
    race_reads.append(1)
    _time.sleep(0.3)  # the usage script takes a while: long enough for the other thread to arrive
    return usage_json(15, 21)


race_job = fake_job({"pid": dead2.pid, "pid_created": 1, "image": "x.exe",
                     "limits_before": limits.snapshot(usage_json(10, 20))}, done_events,
                    {"exit_code": 0, "finished_at": jobs.now_iso()})
race_dir = jobs.job_dir(race_job)
race_errors: list[BaseException] = []
race_costs: list[dict] = []


def race_hook():
    try:
        codex.capture_limits_after(race_job)
    except BaseException as e:  # noqa: BLE001
        race_errors.append(e)


def race_look():
    try:
        race_costs.append(codex.limits_cost(jobs.load_meta(race_job), race_dir))
    except BaseException as e:  # noqa: BLE001
        race_errors.append(e)


with mock.patch.object(limits, "read", slow_read):
    threads = [_threading.Thread(target=race_hook)] + [_threading.Thread(target=race_look) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
check(not race_errors and len(race_reads) == 1 and len(race_costs) == 3
      and all(c["cost"] == {"5h": "+5%", "7d": "+1%"} for c in race_costs),
      "the exit hook and concurrent first looks share one after-reading, no file clash",
      (race_errors, len(race_reads), race_costs))

# write_json survives many writers and readers on one file at once
clash = STATE / "clash.json"
clash_errors: list[BaseException] = []


def clash_writer(n: int):
    try:
        for i in range(50):
            jobs.write_json(clash, {"writer": n, "i": i})
    except BaseException as e:  # noqa: BLE001
        clash_errors.append(e)


def clash_reader():
    try:
        for _ in range(200):
            jobs.read_json(clash)
    except BaseException as e:  # noqa: BLE001
        clash_errors.append(e)


clash_threads = [_threading.Thread(target=clash_writer, args=(n,)) for n in range(4)] + \
                [_threading.Thread(target=clash_reader) for _ in range(2)]
for t in clash_threads:
    t.start()
for t in clash_threads:
    t.join(60)
leftovers = [p.name for p in STATE.glob("clash.json.*.tmp")]
check(not clash_errors and isinstance(jobs.read_json(clash), dict) and not leftovers,
      "write_json: concurrent writers and readers of one file never clash", (clash_errors, leftovers))

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
with mock.patch.object(codex_job.limits, "read", lambda exe=None, env=None: past), \
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
    server._prepare(CODEX, "gpt-6-luna", "ultra")
    check(False, "server refuses an invalid model/effort before starting")
except Exception as ex:
    check("does not support" in str(ex), "server refuses an invalid model/effort before starting", ex)
os.environ["CODEX_HOME"] = str(cache_home / "nowhere")
check(codex.check_model_effort("anything", "whatever") is None, "missing model cache -> no validation (fail open)")
if saved_home is None:
    os.environ.pop("CODEX_HOME", None)
else:
    os.environ["CODEX_HOME"] = saved_home

# --- Claude agent with a fake CLI ------------------------------------------------------

WDIR = Path(tempfile.mkdtemp(prefix="task-delegator-claude-"))
WSTATE = WDIR / "behaviour.json"
fake_py = WDIR / "fake_claude.py"
fake_py.write_text(r'''
import json, os, sys, time, pathlib
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "fake", "subscriptionType": "test"}))
    sys.exit(0)
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
fake_cmd = FAKE_CLAUDE
fake_cmd.write_text(f'@"{sys.executable}" "{fake_py}" %*\r\n', encoding="ascii")
os.environ.update({"TASK_DELEGATOR_CLAUDE_EXE": str(fake_cmd), "FAKE_CLAUDE_STATE": str(WSTATE), "CLAUDECODE": "1"})


def wcalls() -> list[dict]:
    p = Path(str(WSTATE) + ".calls")
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def opt_values(argv: list[str], flag: str) -> list[str]:
    """Values of a variadic CLI option: everything up to the next --flag."""
    i = argv.index(flag) + 1
    j = next((k for k in range(i, len(argv)) if argv[k].startswith("--")), len(argv))
    return argv[i:j]


WSTATE.write_text(json.dumps({}), encoding="utf-8")
st = asyncio.run(server.task_start(agent="claude:test", task="Сделай X", cwd=str(STATE), model="sonnet", effort="low",
                                   options={"ultracode": True}))
res = asyncio.run(server.task_wait(job_id=st["job_id"], timeout_sec=30))
check(res["status"] == "completed" and res["final_message"] == "ГОТОВО: Сделай X", "task job completes with the agent's answer", res)
check(res["session_id"] == "s-123" and res["files_edited"] == ["C:/p/a.py"], "session id and edited files parsed", res)
check(res["permission_denials"][0]["tool"] == "Write", "permission denials are reported", res.get("permission_denials"))
call = wcalls()[-1]
check("dontAsk" in call["argv"] and "Bash" in call["argv"][call["argv"].index("--disallowedTools"):],
      "read-only: dontAsk + edits/shell disallowed", call["argv"])
check(call["argv"][call["argv"].index("--model") + 1] == "sonnet" and "--effort" in call["argv"], "model/effort passed", call["argv"])
check(call["prompt"].strip() == "Сделай X", "prompt goes in via stdin (UTF-8)", call["prompt"])
check(call["config_dir"] == CLAUDE.home and not call["leaked"], "agent gets its own config dir and no lead-session env", call)

rp = asyncio.run(server.task_reply(job_id=st["job_id"], message="Дальше"))
res2 = asyncio.run(server.task_wait(job_id=rp["job_id"], timeout_sec=30))
call = wcalls()[-1]
check(res2["status"] == "completed" and call["argv"][call["argv"].index("--resume") + 1] == "s-123",
      "task_reply resumes the same session", call["argv"])
check(res2.get("continues_job") == st["job_id"], "reply is linked to its parent job", res2)

wj = asyncio.run(server.task_start(agent="claude:test", task="пиши", cwd=str(STATE), mode="write", model="opus", options={"ultracode": True}))
asyncio.run(server.task_wait(job_id=wj["job_id"], timeout_sec=30))
call = wcalls()[-1]
check("acceptEdits" in call["argv"] and "Bash" in opt_values(call["argv"], "--allowedTools")
      and opt_values(call["argv"], "--disallowedTools") == claude.DEFAULT_DENY == ["Bash(git push:*)"],
      "write: acceptEdits, edits and shell allowed, git push denied", call["argv"])
a = call["argv"]
check(a[a.index("--effort") + 1] == "ultracode" and a[a.index("--settings") + 1] == str(claude.WORKFLOW_SETTINGS)
      and call["wf_agents"] == "3", "write options enable ultracode + workflow settings + 3 concurrent agents", a)
check(json.loads(claude.WORKFLOW_SETTINGS.read_text(encoding="utf-8"))["enableWorkflows"] is True,
      "workflow settings file enables workflows")
check(st["options"]["ultracode"] is False, "agent_read_only_forces_ultracode_off", st)
first = wcalls()[0]["argv"]
check("--settings" not in first and first[first.index("--effort") + 1] == "low",
      "read-only jobs never get ultracode", first)
nj = asyncio.run(server.task_start(agent="claude:test", task="без ultracode", cwd=str(STATE), mode="write", options={"ultracode": False}))
asyncio.run(server.task_wait(job_id=nj["job_id"], timeout_sec=30))
a = wcalls()[-1]["argv"]
check("--settings" not in a and "--effort" not in a and "--model" not in a,
      "agent defaults: no model or effort override when ultracode is off", a)
check(wcalls()[-1]["wf_agents"] is None, "no workflow agent cap without ultracode")

WSTATE.write_text(json.dumps({"fail": True}), encoding="utf-8")
fj = asyncio.run(server.task_start(agent="claude:test", task="упади", cwd=str(STATE)))
fres = asyncio.run(server.task_wait(job_id=fj["job_id"], timeout_sec=30))
check(fres["status"] == "failed" and fres["result"]["is_error"] is True, "a failed agent run is reported as failed", fres)

WSTATE.write_text(json.dumps({"sleep": 30}), encoding="utf-8")
sj = asyncio.run(server.task_start(agent="claude:test", task="long", cwd=str(STATE)))
_time.sleep(2)
if os.environ.get("TASK_DELEGATOR_TEST_NO_TASKKILL"):
    # Only for sandboxes that deny taskkill (e.g. Codex's): stop the process ourselves and keep
    # the rest of the cancel path real. Everywhere else the real process-tree kill is tested.
    owned_process = jobs._procs[sj["job_id"]]

    def kill_owned(pid):
        assert pid == owned_process.pid
        owned_process.kill()
        owned_process.wait(timeout=10)
        return True, "terminated fake agent"

    with mock.patch.object(procs, "kill_tree", side_effect=kill_owned):
        cres = asyncio.run(server.task_cancel(job_id=sj["job_id"]))
else:
    cres = asyncio.run(server.task_cancel(job_id=sj["job_id"]))
check(cres["status"] == "cancelled", "task_cancel stops a running agent job", cres)
try:
    asyncio.run(server.task_status(job_id=st["job_id"].replace("claude-", "codex-")))
    check(False, "task_status rejects an unknown job id")
except Exception as e:
    check("unknown job id" in str(e), "task_status rejects an unknown job id", e)
lst = asyncio.run(server.task_status(agent="claude:test"))
check(len(lst["jobs"]) >= 4 and all(j["job_id"].startswith("claude-") and j["agent"] == "claude:test" for j in lst["jobs"]), "task_status agent filter lists only that agent's jobs", lst)

# --- overlapping write jobs are flagged; agents can write without a shell --------------------
sleeper = spawn_sleeper()
ov_root = Path(tempfile.mkdtemp(prefix="task-delegator-overlap-"))
(ov_root / "sub").mkdir()
busy = fake_job({"pid": sleeper.pid, "pid_created": procs.process_created(sleeper.pid), "image": "python.exe",
                 "mode": "write", "cwd": str(ov_root)})
w = server._conflict_warning("write", str(ov_root / "sub"), None)
check(w is not None and busy in w, "write job in a subfolder of a running write job is warned", w)
check(busy in (server._conflict_warning("write", str(ov_root.parent).upper(), None) or ""),
      "a parent folder in other letter case is warned too")
check(busy in (server._conflict_warning("write", str(STATE), [str(ov_root)]) or ""), "add_dirs are compared too")
elsewhere = Path(tempfile.mkdtemp(prefix="task-delegator-elsewhere-"))
busy2 = fake_job({"pid": sleeper.pid, "pid_created": procs.process_created(sleeper.pid), "image": "python.exe",
                  "mode": "write", "cwd": str(elsewhere), "add_dirs": [str(ov_root / "other" / ".." / "shared")]})
check(busy2 in (server._conflict_warning("write", str(ov_root / "shared"), None) or ""),
      "'..' in a running job's add_dirs is folded before comparing")
# 8.3 short names (C:\Users\RUNNER~1\... on CI runners) name the same folder as the long ones.
import ctypes  # noqa: E402

long_dir = ov_root / "LongFolderNameForShortNameCheck"
long_dir.mkdir()
_buf = ctypes.create_unicode_buffer(1024)
_n = ctypes.windll.kernel32.GetShortPathNameW(str(long_dir), _buf, 1024) if os.name == "nt" else 0
short_dir = _buf.value if _n else ""
if short_dir and short_dir.lower() != str(long_dir).lower():
    busy3 = fake_job({"pid": sleeper.pid, "pid_created": procs.process_created(sleeper.pid), "image": "python.exe",
                      "mode": "write", "cwd": short_dir})
    check(busy3 in (server._conflict_warning("write", str(long_dir), None) or ""),
          "an 8.3 short name and the long name of one folder overlap", (short_dir, str(long_dir)))
else:
    check(True, "an 8.3 short name and the long name of one folder overlap (no short names on this volume)")
check(server._conflict_warning("write", str(STATE), None) is None, "an unrelated folder is not warned")
check(server._conflict_warning("read-only", str(ov_root), None) is None, "read-only jobs are never warned")
with mock.patch.object(server.jobs, "launch", lambda *a, **k: None):
    cr = server._create_and_launch(CODEX, str(FAKE_CODEX), "t", str(ov_root), "write", None, None,
                                   None, {"web_search": False}, None, None, {"state": "ok"}, None)
check(busy in cr.get("warning", ""), "a task write start reports the overlapping job", cr)

WSTATE.write_text(json.dumps({}), encoding="utf-8")
ns = asyncio.run(server.task_start(agent="claude:test", task="без shell", cwd=str(ov_root), mode="write", options={"ultracode": False, "shell": False}))
check(busy in ns.get("warning", "") and ns.get("options", {}).get("shell") is False, "task_start reports the overlap and shell=false", ns)
asyncio.run(server.task_wait(job_id=ns["job_id"], timeout_sec=30))
a = wcalls()[-1]["argv"]
check("Edit" in opt_values(a, "--allowedTools") and "Bash" not in opt_values(a, "--allowedTools")
      and {"Bash", "PowerShell"} <= set(opt_values(a, "--disallowedTools")),
      "write with shell=false: edits allowed, Bash/PowerShell denied", a)
nr = asyncio.run(server.task_reply(job_id=ns["job_id"], message="дальше"))
asyncio.run(server.task_wait(job_id=nr["job_id"], timeout_sec=30))
a = wcalls()[-1]["argv"]
check("Bash" not in opt_values(a, "--allowedTools"), "task_reply keeps shell=false", a)
sleeper.kill()
sleeper.wait()
os.environ.pop("CLAUDECODE", None)

# --- discovery and agents.json -------------------------------------------------------
found, problems = agents.discover()
check(set(found) == {"codex", "codex:work", "codex:old", "claude:test", "claude:other", "claude:limited"}
      and not problems, "agent_discovery", (found, problems))
check("claude" not in found and found["claude:test"].home == str(ACCOUNTS / ".claude-accounts" / "test")
      and found["codex:work"].home == str(ACCOUNTS / ".codex-accounts" / "work"),
      "agent_account_homes", found)
(ACCOUNTS / ".claude-accounts" / "bad name").mkdir()
(ACCOUNTS / ".codex-accounts" / "not-a-directory").write_text("x", encoding="utf-8")
check(set(agents.discover()[0]) == set(found), "agent_discovery_ignores_invalid_folders")

explicit_home = STATE / "external-claude"
explicit_home.mkdir()
config = {"agents": {
    "codex:work": {"description": "Configured Codex", "model": "gpt-6.1-sol", "effort": "high",
                   "options": {"web_search": True}, "max_jobs": 1},
    "codex:old": {"disabled": True},
    "claude:configured": {"home": str(explicit_home), "model": "sonnet", "effort": "low",
                          "options": {"shell": True, "ultracode": False, "max_turns": 7},
                          "max_jobs": 1, "deny": ["Bash(git push:*)", "Bash(rm:*)"],
                          "workflow_agents": 2},
    "claude:test": {"unexpected_setting": True},
    "claude:bad name": {"home": str(explicit_home)},
}}
AGENTS_FILE.write_text(json.dumps(config), encoding="utf-8")
found, problems = agents.discover()
work = found["codex:work"]
configured = found["claude:configured"]
check(work.setting("description") == "Configured Codex" and work.setting("model") == "gpt-6.1-sol"
      and work.setting("effort") == "high" and work.setting("options") == {"web_search": True}
      and work.home == str(ACCOUNTS / ".codex-accounts" / "work"),
      "agent_config_merge", work)
check("codex:old" not in found, "agent_config_disabled")
try:
    asyncio.run(server.task_start(agent="codex:old", task="t", cwd=str(STATE)))
    check(False, "task_start_disabled_agent")
except server.ToolError as e:
    check("unknown agent" in str(e) and "codex:old" in str(e), "task_start_disabled_agent", e)
check(configured.home == str(explicit_home), "agent_config_explicit_home", configured)
check(any("not a valid agent id" in p and "claude:bad name" in p for p in problems),
      "agent_config_invalid_id", problems)
check(any("unknown keys" in p and "unexpected_setting" in p for p in problems)
      and "unexpected_setting" not in found["claude:test"].settings,
      "agent_config_unknown_keys", problems)
check(server._max_jobs(work) == server._max_jobs(configured) == 1
      and server._max_jobs(found["claude:other"]) == 2 and server._max_jobs(found["codex"]) == 3,
      "agent_config_max_jobs")

for aid, unknown in (("codex", "shell"), ("claude:test", "web_search")):
    try:
        asyncio.run(server.task_start(agent=aid, task="t", cwd=str(STATE), options={unknown: True}))
        check(False, f"task_unknown_option_{aid.split(':')[0]}")
    except server.ToolError as e:
        check("unknown option" in str(e) and unknown in str(e) and "supported:" in str(e),
              f"task_unknown_option_{aid.split(':')[0]}", e)

# --- real fake-CLI jobs: defaults, options, account environments and replies ------------
fake(json.dumps(usage_json(10, 20)))
WSTATE.write_text("{}", encoding="utf-8")
cw = asyncio.run(server.task_start(agent="codex:work", task="configured Codex", cwd=str(STATE)))
cv = asyncio.run(server.task_wait(cw["job_id"], timeout_sec=30))
meta = jobs.load_meta(cw["job_id"])
a = meta["argv"]
check(cv["status"] == "completed" and a[a.index("-m") + 1] == "gpt-6.1-sol"
      and 'model_reasoning_effort="high"' in a and 'web_search="live"' in a,
      "agent_codex_defaults_argv", (cv, a))
check(meta["agent"] == "codex:work" and meta["home"] == work.home and meta["exe"] == str(FAKE_CODEX)
      and meta["options"] == {"web_search": True} and meta["resume_session"] is None,
      "task_job_metadata", meta)
codex_calls = [json.loads(line) for line in Path(os.environ["FAKE_CODEX_CALLS"]).read_text(encoding="utf-8").splitlines()]
check(codex_calls[-1]["home"] == work.home, "agent_codex_home_env", codex_calls[-1])
check(cv["session_id"] == "fake-codex-session" and cv["agent"] == "codex:work"
      and "codex_session_id" not in cv, "task_codex_view_session_agent", cv)
cp = asyncio.run(server.task_reply(cw["job_id"], "follow-up"))
cpv = asyncio.run(server.task_wait(cp["job_id"], timeout_sec=30))
check(cpv["status"] == "completed" and cpv["continues_job"] == cw["job_id"]
      and cp["options"] == {"web_search": True}
      and jobs.load_meta(cp["job_id"])["resume_session"] == cv["session_id"],
      "task_codex_reply_inherits", cpv)
cp2 = asyncio.run(server.task_reply(cp["job_id"], "no search", options={"web_search": False}))
asyncio.run(server.task_wait(cp2["job_id"], timeout_sec=30))
check('web_search="live"' not in jobs.load_meta(cp2["job_id"])["argv"], "task_reply_options_override")

cj = asyncio.run(server.task_start(agent="claude:configured", task="configured Claude", cwd=str(STATE), mode="write"))
cv = asyncio.run(server.task_wait(cj["job_id"], timeout_sec=30))
call = wcalls()[-1]
a = call["argv"]
check(cv["status"] == "completed" and a[a.index("--model") + 1] == "sonnet"
      and a[a.index("--effort") + 1] == "low" and a[a.index("--max-turns") + 1] == "7"
      and "Bash" in opt_values(a, "--allowedTools"), "agent_claude_defaults_argv", a)
check(call["config_dir"] == str(explicit_home), "agent_explicit_home_env", call)
check(opt_values(a, "--disallowedTools") == config["agents"]["claude:configured"]["deny"],
      "agent_claude_deny_argv", a)
uj = asyncio.run(server.task_start(agent="claude:configured", task="workflow", cwd=str(STATE), mode="write",
                                   options={"ultracode": True}))
asyncio.run(server.task_wait(uj["job_id"], timeout_sec=30))
check(wcalls()[-1]["wf_agents"] == "2" and "ultracode" in wcalls()[-1]["argv"],
      "agent_workflow_agents_env", wcalls()[-1])

# --- capacity is scoped to one agent, even for two accounts of the same kind ------------
capacity_process = spawn_sleeper()
try:
    full_job = fake_job({"agent": work.id, "pid": capacity_process.pid,
                         "pid_created": procs.process_created(capacity_process.pid), "image": "python.exe"})
    check(server._running_count(work.id) == 1 and server._running_count("codex") == 0,
          "agent_running_count_isolated")
    before = set(jobs.list_job_ids(None, 500))
    try:
        asyncio.run(server.task_start(agent=work.id, task="full", cwd=str(STATE)))
        check(False, "task_agent_capacity_blocks_full")
    except server.ToolError as e:
        check(work.id in str(e) and "limit 1" in str(e) and set(jobs.list_job_ids(None, 500)) == before,
              "task_agent_capacity_blocks_full", e)
    other_job = asyncio.run(server.task_start(agent="codex", task="other account", cwd=str(STATE)))
    other_view = asyncio.run(server.task_wait(other_job["job_id"], timeout_sec=30))
    check(other_view["status"] == "completed" and other_view["agent"] == "codex",
          "task_agent_capacity_allows_other", other_view)
finally:
    capacity_process.kill()
    capacity_process.wait()

filtered = asyncio.run(server.task_status(agent=work.id, limit=50))["jobs"]
check({cw["job_id"], cp["job_id"], cp2["job_id"], full_job} <= {j["job_id"] for j in filtered}
      and all(j["agent"] == work.id for j in filtered), "task_status_agent_filter", filtered)
all_jobs = asyncio.run(server.task_status(limit=50))["jobs"]
check({"codex", "codex:work", "claude:test", "claude:configured"} <= {j["agent"] for j in all_jobs},
      "task_status_all_kinds", all_jobs)

# --- Claude limits: use real event files, not mocked adapter verdicts ------------------
limited = found["claude:limited"]
info, snap = server._limits_gate(limited, str(FAKE_CLAUDE), False)
check(info["state"] == "unknown" and snap is None and "no recent job" in info["note"],
      "agent_claude_limits_unknown", info)
rate_now = int(_time.time())
rl = {"unifiedWindows": {"five_hour": {"utilization": 0.97, "resetsAt": rate_now + 3600, "status": "allowed"},
                         "seven_day": {"utilization": 0.2, "resetsAt": rate_now + 86400, "status": "allowed"}}}
rate_job = fake_job({"agent": limited.id}, [{"type": "rate_limit_event", "rate_limit_info": rl}],
                    {"exit_code": 0, "finished_at": jobs.now_iso()}, provider="claude")
rate_log = jobs.job_dir(rate_job) / "stdout.log"
reading, measured, source = claude.last_rate_limits(limited.id)
check(reading == rl and measured and source == rate_job, "agent_claude_limits_event_source", (reading, measured, source))
try:
    server._limits_gate(limited, str(FAKE_CLAUDE), False)
    check(False, "agent_claude_limits_pause_97")
except server.ToolError as e:
    check(limited.id in str(e) and "97%" in str(e) and "resets at" in str(e),
          "agent_claude_limits_pause_97", e)
before = set(jobs.list_job_ids(None, 500))
try:
    asyncio.run(server.task_start(agent=limited.id, task="must pause", cwd=str(STATE)))
    check(False, "task_claude_limits_gate")
except server.ToolError as e:
    check("exhausted" in str(e) and set(jobs.list_job_ids(None, 500)) == before, "task_claude_limits_gate", e)
info, snap = server._limits_gate(limited, str(FAKE_CLAUDE), True)
check(info.get("overridden") is True and snap is None, "agent_claude_limits_override", info)
rl["unifiedWindows"]["five_hour"]["resetsAt"] = rate_now - 1
rate_log.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": rl}), encoding="utf-8")
info, _ = server._limits_gate(limited, str(FAKE_CLAUDE), False)
converted = claude.usage_from_rate_limits(rl, measured, now=rate_now)
check(info["state"] == "ok" and converted["buckets"][0]["windows"][0]["used_percent"] == 0,
      "agent_claude_limits_expired_ok", (info, converted))
rl["unifiedWindows"]["five_hour"].update(utilization=0.1, resetsAt=rate_now + 3600, status="rejected")
rate_log.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": rl}), encoding="utf-8")
try:
    server._limits_gate(limited, str(FAKE_CLAUDE), False)
    check(False, "agent_claude_limits_rejected")
except server.ToolError as e:
    check("rejected" in str(e), "agent_claude_limits_rejected", e)
rl["unifiedWindows"]["five_hour"]["resetsAt"] = rate_now - 1
rate_log.write_text(json.dumps({"type": "rate_limit_event", "rate_limit_info": rl}), encoding="utf-8")
check(server._limits_gate(limited, str(FAKE_CLAUDE), False)[0]["state"] == "ok",
      "agent_claude_limits_expired_rejection")
# Explicit timestamps avoid same-second random job-id ordering in the fixture.
with mock.patch.object(jobs, "new_job_id", return_value="claude-20990101-000000-0001"):
    newest = fake_job({"agent": limited.id}, [{"type": "rate_limit_event", "rate_limit_info": rl}],
                      {"exit_code": 0}, provider="claude")
with mock.patch.object(jobs, "new_job_id", return_value="claude-20990101-000001-0001"):
    fake_job({"agent": limited.id}, [], {"exit_code": 0}, provider="claude")
with mock.patch.object(jobs, "new_job_id", return_value="claude-20990101-000002-0001"):
    fake_job({"agent": "claude:other"}, [{"type": "rate_limit_event", "rate_limit_info": {"other": True}}],
             {"exit_code": 0}, provider="claude")
check(claude.last_rate_limits(limited.id)[2] == newest, "agent_claude_limits_newest_per_agent")

# --- legacy jobs remain readable through the generic tools ----------------------------
legacy_events = [{"type": "system", "subtype": "init", "session_id": "legacy-session"},
                 {"type": "result", "subtype": "success", "is_error": False,
                  "result": "legacy answer", "session_id": "legacy-session"}]
legacy_worker = fake_job({}, legacy_events, {"exit_code": 0}, provider="worker")
legacy_view = asyncio.run(server.task_status(legacy_worker))
check(legacy_view["status"] == "completed" and legacy_view["final_message"] == "legacy answer"
      and legacy_view["session_id"] == "legacy-session", "task_status_legacy_worker", legacy_view)
try:
    asyncio.run(server.task_reply(legacy_worker, "continue"))
    check(False, "task_reply_legacy_worker_refused")
except server.ToolError as e:
    check("account is unknown" in str(e), "task_reply_legacy_worker_refused", e)
legacy_codex = fake_job({"web_search": True}, done_events, {"exit_code": 0})
legacy_view = asyncio.run(server.task_status(legacy_codex))
check(legacy_view["agent"] == "codex" and legacy_view["session_id"] == "t2"
      and "agent" not in jobs.load_meta(legacy_codex), "task_status_legacy_codex", legacy_view)
check(legacy_codex in {j["job_id"] for j in asyncio.run(server.task_status(agent="codex", limit=50))["jobs"]},
      "task_status_legacy_codex_filter")
lr = asyncio.run(server.task_reply(legacy_codex, "legacy reply"))
asyncio.run(server.task_wait(lr["job_id"], timeout_sec=30))
check(lr["agent"] == "codex" and lr["options"]["web_search"] is True
      and jobs.load_meta(lr["job_id"])["resume_session"] == "t2", "task_reply_legacy_codex", lr)
legacy_process = spawn_sleeper()
try:
    legacy_running = fake_job({"pid": legacy_process.pid, "pid_created": procs.process_created(legacy_process.pid),
                               "image": "python.exe", "resume_thread": "legacy-busy"})
    check(server._running_count("codex") == 1 and server._session_busy("codex", "legacy-busy") == legacy_running,
          "agent_legacy_codex_capacity_session")
finally:
    legacy_process.kill()
    legacy_process.wait()

# --- discovery tools include defaults, limits, kinds and configuration problems ---------
cards_result = asyncio.run(server.list_agents())
cards = {c["id"]: c for c in cards_result["agents"]}
check(set(cards) == set(found) and all(c["available"] and "limits" in c for c in cards.values()),
      "list_agents_accounts_limits", cards_result)
check(cards[work.id]["defaults"] == {"model": "gpt-6.1-sol", "effort": "high", "options": {"web_search": True}}
      and cards[work.id]["description"] == "Configured Codex" and cards[work.id]["max_jobs"] == 1
      and cards[limited.id]["limits"]["state"] == "ok" and cards["claude:test"]["limits"]["state"] == "unknown",
      "list_agents_defaults_limits", cards)
check(cards_result.get("problems") == problems, "list_agents_config_problems", cards_result)
doctor = asyncio.run(server.delegator_doctor())
check(set(doctor["kinds"]) == {"codex", "claude"} and set(doctor["agents"]) == set(found)
      and doctor["agents"][configured.id]["home"] == str(explicit_home)
      and doctor["agents"][configured.id]["auth"]["loggedIn"] is True
      and doctor["agents"]["codex"]["version"] == "codex fake 1.0",
      "delegator_doctor_kinds_agents", doctor)
check(doctor.get("problems") == problems and doctor["agents_file"]["path"] == str(AGENTS_FILE),
      "delegator_doctor_config_problems", doctor)

# --- shared threshold names and runner account selection -------------------------------
with mock.patch.dict(os.environ, {"TASK_DELEGATOR_CODEX_WARN_5H": "88"}):
    check(limits.thresholds()["primary"]["warn"] == 88, "agent_threshold_legacy_env")
    with mock.patch.dict(os.environ, {"TASK_DELEGATOR_WARN_5H": "89"}):
        check(limits.thresholds()["primary"]["warn"] == 89, "agent_threshold_new_env_precedence")
with mock.patch.object(limits, "read", return_value=usage_json(10, 20)) as read:
    codex_job.gate_on_limits({}, 120, home=work.home)
    check(read.call_args.kwargs["env"]["CODEX_HOME"] == work.home, "task_runner_limits_home_env")
for aid, expected in (("claude:test", "Codex agents only"), ("codex:work", "dry-run")):
    r = subprocess.run([sys.executable, str(RUNNER), "--agent", aid, "--task-file", str(task_file),
                        "--cwd", str(STATE), "--dry-run", "--no-limits"],
                       capture_output=True, text=True, encoding="utf-8")
    check(r.returncode == (1 if aid.startswith("claude:") else 0) and expected in r.stdout,
          f"task_runner_agent_{aid.split(':')[0]}", r.stdout)

# --- review regressions: limits freshness and scope, rejected events, setting types ------
lim2 = "claude:limited"
with mock.patch.object(jobs, "new_job_id", return_value="claude-20990102-000020-0001"):
    later_started = fake_job({"agent": lim2}, [{"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.1, "resetsAt": rate_now + 3600}}}}],
        {"exit_code": 0}, provider="claude")
with mock.patch.object(jobs, "new_job_id", return_value="claude-20990102-000010-0001"):
    long_running = fake_job({"agent": lim2}, [{"type": "rate_limit_event", "rate_limit_info": {
        "unifiedWindows": {"five_hour": {"utilization": 0.99, "resetsAt": rate_now + 3600}}}}],
        {"exit_code": 0}, provider="claude")
t_new = _time.time() + 120
os.utime(jobs.job_dir(later_started) / "stdout.log", (t_new - 60, t_new - 60))
os.utime(jobs.job_dir(long_running) / "stdout.log", (t_new, t_new))
check(claude.last_rate_limits(lim2)[2] == long_running,
      "agent_claude_limits_freshest_log_wins (not the latest-started job)")
for i in range(25):  # more than any fixed window over all accounts' jobs
    with mock.patch.object(jobs, "new_job_id", return_value=f"claude-20990103-{i:06d}-0001"):
        other_job = fake_job({"agent": "claude:other"}, [], {"exit_code": 0}, provider="claude")
    os.utime(jobs.job_dir(other_job) / "stdout.log", (t_new + 10 + i, t_new + 10 + i))
check(claude.last_rate_limits(lim2)[2] == long_running, "agent_claude_limits_other_accounts_do_not_hide_it")
top = limits.evaluate(claude.usage_from_rate_limits(
    {"status": "rejected", "resetsAt": rate_now + 600, "rateLimitType": "overage"}, None, now=rate_now))
check(top["state"] == "pause" and top.get("resume_after"), "agent_claude_limits_top_level_rejected", top)
top5 = limits.evaluate(claude.usage_from_rate_limits(
    {"status": "rejected", "resetsAt": rate_now + 600, "rateLimitType": "five_hour"}, None, now=rate_now))
check(top5["state"] == "pause", "agent_claude_limits_top_level_window_without_details", top5)
try:
    server._options(found["claude:test"], "write", {"ultracode": "false"})
    check(False, "task_option_types_checked_before_normalizing")
except server.ToolError as e:
    check("ultracode" in str(e), "task_option_types_checked_before_normalizing", e)
saved_config = AGENTS_FILE.read_text(encoding="utf-8")
bad_config = json.loads(saved_config)
bad_config["agents"]["claude:test"] = {"options": ["ultracode"], "max_jobs": "2", "name": "Tester (fake)"}
AGENTS_FILE.write_text(json.dumps(bad_config), encoding="utf-8")
bad_res = asyncio.run(server.list_agents())
bad_card = next(c for c in bad_res["agents"] if c["id"] == "claude:test")
check(any("claude:test.options" in p for p in bad_res.get("problems", []))
      and any("claude:test.max_jobs" in p for p in bad_res.get("problems", []))
      and bad_card.get("name") == "Tester (fake)" and bad_card["max_jobs"] == 2,
      "agent_config_bad_types_reported_not_fatal", bad_res)
AGENTS_FILE.write_text(saved_config, encoding="utf-8")
old_proc = spawn_sleeper()
try:
    base_running = server._running_count("codex")
    with mock.patch.object(jobs, "new_job_id", return_value="codex-20000101-000000-0001"):
        fake_job({"agent": "codex", "pid": old_proc.pid, "pid_created": procs.process_created(old_proc.pid),
                  "image": "python.exe"})
    for i in range(120):
        with mock.patch.object(jobs, "new_job_id", return_value=f"claude-20990104-{i:06d}-0001"):
            fake_job({"agent": "claude:other"}, [], {"exit_code": 0}, provider="claude")
    check(server._running_count("codex") == base_running + 1,
          "agent_capacity_sees_a_running_job_behind_many_newer_ones")
finally:
    old_proc.kill()
    old_proc.wait()

print(f"\nRESULT: {checks} checks:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
sys.exit(1 if failures else 0)
