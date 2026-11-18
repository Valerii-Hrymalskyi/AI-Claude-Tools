# -*- coding: utf-8 -*-
"""Checks how usage_guard.py judges a stale sample against a rolled-over window.

Scope: usage_guard.py next to this script

The guard reads one number - "fh", the 5-hour window percentage - out of a file
the desktop app writes every 5 minutes. That number describes the window its
sample was taken in, and nothing else. When the app stops writing, the newest
sample stays newest for as long as the silence lasts, so a spent window keeps
reading as spent long after it has rolled over. That is a hard deny on a budget
that is empty only in the record.

The rule under test: a stale sample whose window has demonstrably ended is not
evidence about the window now running. A *fresh* sample always wins, so silence
is what makes the difference, not age alone.

Four cases, run against the real script through its command line:

    A  fresh and spent          -> pause, and the confirmed number is quoted
    B  stale, window ended      -> ok, the sample is dropped as expired
    C  stale, window running    -> pause; staleness alone must not clear it
    D  expired, new window hot  -> pause on the estimate, deny text renders

C is the regression that matters: it is the one that would turn this fix into a
hole in the pause protocol.

    <clone>/Utils/Bin/Windows/Python3/python.exe <clone>/Projects/UsageGuard/verify_usage_guard.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "usage_guard.py")

MIN = 60 * 1000
NOW = int(time.time() * 1000)


def samples(rows):
    """rows: (minutes before now, fh) -- sd is fixed, it is not under test."""
    return {"samples": [{"t": NOW - m * MIN, "u": {"fh": fh, "sd": 25}}
                        for m, fh in rows]}


def transcript(rows):
    """rows: (minutes before now, output tokens) as assistant messages."""
    lines = []
    for minutes, out in rows:
        when = datetime.fromtimestamp((NOW - minutes * MIN) / 1000.0, timezone.utc)
        lines.append(json.dumps({
            "type": "assistant",
            "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "message": {"model": "claude-opus-5",
                        "usage": {"input_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0,
                                  "output_tokens": out}},
        }))
    return "\n".join(lines) + "\n"


GUARD_TOKEN_ENV = "CLAUDE_USAGE_LIMITS_CHECK_TOKEN"


def probe_fixture(fh, sd, reset_min_ahead, age_sec=0):
    """A cached probe, as get_probe() would have written it."""
    now_ms = int(time.time() * 1000)
    return {
        "t": now_ms - int(age_sec * 1000),
        "fh": float(fh),
        "sd": float(sd),
        "reset_5h_ms": now_ms + int(reset_min_ahead * 60000),
        "reset_7d_ms": now_ms + 86400000,
        "status_5h": "allowed",
        "status_7d": "allowed",
    }


def run(root, history, messages, mode="json", payload=None, probe=None):
    hist_file = os.path.join(root, "history.json")
    proj_dir = os.path.join(root, "projects", "some-project")
    if os.path.isdir(os.path.join(root, "projects")):
        shutil.rmtree(os.path.join(root, "projects"))
    os.makedirs(proj_dir)
    with open(hist_file, "w", encoding="utf-8") as fh:
        json.dump(history, fh)
    if messages:
        with open(os.path.join(proj_dir, "session.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(transcript(messages))

    cmd = [sys.executable, GUARD, "-mode", mode,
           "-history-file", hist_file,
           "-projects-dir", os.path.join(root, "projects"),
           "-checkpoint-dir", root,
           "-state-file", os.path.join(root, "state.json")]
    if probe is None:
        cmd += ["-probe", "off"]
    else:
        cache = os.path.join(root, "probe.json")
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(probe, fh)
        cmd += ["-probe", "auto", "-probe-cache", cache]

    # The token is stripped unconditionally: with it in the environment a case
    # whose cached probe is rejected would fall through to a live request and
    # start asserting against the real account. Every probe here comes from the
    # fixture or not at all.
    env = dict(os.environ)
    env.pop(GUARD_TOKEN_ENV, None)
    # Same for the token FILE: it lives in the account's config dir, so the
    # cases run under an empty one of their own.
    env["CLAUDE_CONFIG_DIR"] = os.path.join(root, "config")

    proc = subprocess.run(cmd, input=json.dumps(payload or {}),
                          capture_output=True, text=True, env=env)
    return proc


def state_of(proc):
    #-mode json prints the argument banner first, so take the JSON object only.
    text = proc.stdout
    start = text.index("{", text.index('"ok"') - 2000 if '"ok"' in text else 0)
    return json.loads(text[text.index("{", start):])


CASES = []


def case(name, history, messages, expect, probe=None):
    CASES.append((name, history, messages, expect, probe))


# A: the app is talking, the window is nearly gone. Nothing about this fix may
#    touch it - this is the guard doing its job.
case("A  fresh and spent",
     samples([(120, 40), (115, 0), (60, 70), (10, 95), (3, 95)]),
     None,
     {"sample_expired": False, "verdict": "pause", "five_hour_pct": 95})

# B: the app went quiet 350 minutes ago. The window that sample belonged to
#    ended 300 minutes ago, so the 100% describes nothing that still exists.
case("B  stale, window ended",
     samples([(620, 80), (610, 0), (500, 60), (350, 100)]),
     None,
     {"sample_expired": True, "verdict": "ok", "five_hour_pct": None})

# C: equally stale, but the window is still running - the sample is old news,
#    not wrong news. Clearing this one would be the hole in the protocol.
case("C  stale, window running",
     samples([(110, 80), (100, 0), (70, 60), (40, 100)]),
     None,
     {"sample_expired": False, "verdict": "pause", "five_hour_pct": 100})

# D: expired, but the transcripts show the new window has itself been spent.
#    The estimate alone has to be enough to pause on.
#
#    The output figure is sized against the pause threshold, not picked freely:
#    3.1M output tokens weigh 5x and divide by 160 000 per point, so this lands
#    near 97 % -- clear of the 95 % threshold without saturating at 100, where
#    the case would pass on the cap instead of on the threshold it is testing.
#    Raising -pause-pct again means raising this too; the 2026-08-17 move from
#    92 % to 95 % is what caught it.
case("D  expired, new window hot",
     samples([(620, 80), (610, 0), (500, 60), (350, 100)]),
     [(590, 1000), (100, 3100000)],
     {"sample_expired": True, "verdict": "pause"})


# E: past the predicted reset, but the app is still talking - so the prediction
#    was simply wrong (the window started later than the first message after the
#    drop) and the number in front of us is measured. A fresh sample outranks
#    any inference. Without this case A would pass for two reasons at once and
#    the freshness term could be dropped unnoticed.
case("E  fresh, past predicted reset",
     samples([(410, 80), (400, 0), (200, 60), (3, 95)]),
     None,
     {"sample_expired": False, "verdict": "pause", "five_hour_pct": 95})


# F: the file is calm, the server says the window is nearly gone. The probe is a
#    measurement and the file is a 15-minute-old sample, so the probe wins - this
#    is the whole point of Source 0.
case("F  probe overrides a calm file",
     samples([(120, 40), (115, 0), (10, 10)]),
     None,
     {"verdict": "pause", "five_hour_pct": 95, "source": "server",
      "history_stale": False},
     probe=probe_fixture(fh=95, sd=40, reset_min_ahead=42))

# G: a cached probe whose OWN window has already ended describes nothing that
#    still exists - exactly the trap the file path fell into for hours on
#    2026-08-11. It must be dropped and the file believed instead.
case("G  probe past its own reset is dropped",
     samples([(120, 40), (115, 0), (10, 10)]),
     None,
     {"verdict": "ok", "five_hour_pct": 10, "source": "file"},
     probe=probe_fixture(fh=95, sd=40, reset_min_ahead=-10))

# H: the app has been quiet for hours and the file's sample belongs to a window
#    that is over - case B, where the guard has to give up on a confirmed number.
#    With a probe there is nothing to give up: the server still answers.
case("H  probe rescues an expired file",
     samples([(620, 80), (610, 0), (500, 60), (350, 100)]),
     None,
     {"sample_expired": False, "verdict": "ok", "five_hour_pct": 30,
      "source": "server", "window_start_certain": True},
     probe=probe_fixture(fh=30, sd=40, reset_min_ahead=180))


def check_accounts(root):
    """Two Claude accounts on one machine must never see each other's limits.

    The token used to come only from a user-level environment variable, which
    every process inherits: the second account (its own CLAUDE_CONFIG_DIR)
    probed with the first one's token and reported the first one's numbers.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("usage_guard_under_test", GUARD)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    home = os.path.join(root, "home")
    worker = os.path.join(root, "home", ".claude-worker-b")
    os.makedirs(os.path.join(home, ".claude"))
    os.makedirs(worker)
    failures = []

    def expect(label, got, want):
        ok = got == want
        print("  %-44s %s" % (label, "OK" if ok else "FAILED"))
        if not ok:
            failures.append("%s: expected %r, got %r" % (label, want, got))

    saved = dict(os.environ)
    try:
        os.environ["USERPROFILE"] = home
        os.environ[GUARD_TOKEN_ENV] = "env-token"

        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        expect("default: env token honoured", guard.probe_token()[0], "env-token")
        expect("default: old temp file names kept", guard.account_suffix(), "")
        with open(os.path.join(home, ".claude", "usage-limits-token"), "w") as fh:
            fh.write("lead-token\n")
        expect("default: token file beats env", guard.probe_token()[0], "lead-token")
        lead_cache = guard.temp_path("claude-usage-guard-probe", ".json")

        os.environ["CLAUDE_CONFIG_DIR"] = worker
        expect("second: inherited env token ignored", guard.probe_token()[0], None)
        with open(os.path.join(worker, "usage-limits-token"), "w") as fh:
            fh.write("worker-token")
        expect("second: own token file used", guard.probe_token()[0], "worker-token")
        expect("second: probe cache not shared",
               guard.temp_path("claude-usage-guard-probe", ".json") != lead_cache, True)
        expect("second: no desktop-app history", guard.default_history_file(), "")
        expect("second: own transcripts",
               guard.default_projects_dir(), os.path.join(worker, "projects"))
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return failures


def main():
    root = tempfile.mkdtemp(prefix="usage-guard-check-")
    failures = []
    try:
        failures += check_accounts(root)
        print()
        for name, history, messages, expect, probe in CASES:
            proc = run(root, history, messages, probe=probe)
            if proc.returncode != 0:
                failures.append("%s: exit %d\n%s" % (name, proc.returncode, proc.stderr))
                continue
            state = state_of(proc)
            bad = {k: (v, state.get(k)) for k, v in expect.items() if state.get(k) != v}
            print("  %-28s live %5.1f%%  expired=%-5s verdict=%-5s  %s"
                  % (name, state["live_pct"] or 0, state["sample_expired"],
                     state["verdict"], "OK" if not bad else "FAILED"))
            for key, (want, got) in bad.items():
                failures.append("%s: %s expected %r, got %r" % (name, key, want, got))

            # The deny text is built only on the pause path, and it is formatted
            # outside any try - a bad placeholder there would crash the PreToolUse
            # hook and block every tool in the session.
            if expect.get("verdict") == "pause":
                hook = run(root, history, messages, mode="hook", probe=probe,
                           payload={"tool_name": "Bash", "tool_input": {"command": "make"},
                                    "session_id": "test-session"})
                if hook.returncode != 0:
                    failures.append("%s: hook exit %d\n%s" % (name, hook.returncode,
                                                              hook.stderr))
                    continue
                try:
                    reason = json.loads(hook.stdout)["hookSpecificOutput"]
                except Exception as exc:
                    failures.append("%s: hook output unreadable (%s)\n%s"
                                    % (name, exc, hook.stdout))
                    continue
                if reason.get("permissionDecision") != "deny":
                    failures.append("%s: hook did not deny" % name)
                elif "{" in reason.get("permissionDecisionReason", ""):
                    failures.append("%s: deny text has an unfilled placeholder" % name)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if failures:
        for line in failures:
            print("  FAILED  %s" % line)
        return 1
    print("  OK - all %d cases and the account split behave as intended" % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
