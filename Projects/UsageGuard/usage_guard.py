# -*- coding: utf-8 -*-
"""Reads the Claude plan usage telemetry and tells the assistant when to stop.

Scope: this machine and the Claude account it runs under; no project of its own.
Lives in <clone>/Projects/UsageGuard, wired as user-level hooks (see README.md).

Three data sources are combined:

  0. The server itself.  Every response from the messages endpoint carries
     anthropic-ratelimit-unified-{5h,7d}-{utilization,reset}: the real numbers
     AND the real reset times, which nothing local can do better than guess.
     Needs a token of THIS account: <config dir>/usage-limits-token, where the
     config dir is CLAUDE_CONFIG_DIR or ~/.claude.  The default account may
     still use CLAUDE_USAGE_LIMITS_CHECK_TOKEN from the environment instead.
     Without a token the script falls back to sources 1 and 2.
     Cached with a short adaptive TTL, so a tool call costs +20 ms rather than
     the ~1 s a request takes.

  1. %APPDATA%\\Claude\\plan-usage-history.json  -- the local fallback.
     Written by the desktop app, i.e. for the default account only: under any
     other CLAUDE_CONFIG_DIR this source is off.
     The desktop app polls the server and appends a sample
     {"t": <epoch ms>, "u": {"fh": <5-hour window %>, "sd": <7-day %>}}.
     This is exactly what /usage shows.  Ground truth, but up to 15 minutes
     stale: the polling interval was 5 minutes until 2026-08-11 and is 15 now.

  2. <config dir>/projects/**/*.jsonl -- the session transcripts.  Every assistant
     message carries its own token usage, written the moment the message ends.
     Used only to interpolate the gap since the last sample: bursts of 15-20
     percentage points inside one 5-minute interval do happen.

Modes:

  status  human-readable report
  json    the same as machine-readable JSON
  probe   one request to the server, printed raw -- for checking the plumbing
  prompt  UserPromptSubmit hook: the budget into the context, on every message
  stop    Stop hook: the budget to the USER once the turn has ended, as
          {"systemMessage": ...} -- plain stdout would only reach the transcript
  watch   PostToolUse hook: the same line, but only once the budget gets tight
  hook    PreToolUse hook: denies tool calls once the window is nearly spent
  wait    blocks until the 5-hour window resets, prints one line, exits.
          Meant to be run detached (Monitor tool / background bash) so that
          waiting for the reset costs no tokens at all.

The three hook modes exist because an assistant that has to *remember* to check
its own budget will not: between messages there is nothing to remind it, and the
burn rate reaches 1.2 %/min. So the harness reports the number instead.

Calibration (see usage-limits.md next to this script): 1 % of the 5-hour window is
about 160 000 "weighted" tokens, where

    weighted = input + 1.25*cache_write + 0.1*cache_read + 5*output

scaled by a per-model factor.  Re-measured 2026-08-10 over 238 intervals of the
recorded history: 162 000 aggregate, 157 000 median.  The weighting shape is the
right one -- its per-interval spread is 57x, against 178x for raw tokens and
220x for output alone -- but it is still good only for interpolating minutes,
never as an absolute number.  The authoritative value always stays "fh".
"""

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

#-- Constants -----------------------------------------------------------------

WINDOW_HOURS = 5
SAMPLE_PERIOD_SEC = 300

# How far past a window's predicted end a stale sample is declared expired.
# The prediction is good to about 9 minutes and can only be early, never late,
# so this clears the measured error with room to spare.
EXPIRY_MARGIN_MIN = 15

# Relative cost of one token of each kind, mirroring the published price ratios.
W_INPUT = 1.0
W_CACHE_WRITE = 1.25
W_CACHE_READ = 0.1
W_OUTPUT = 5.0

# Relative cost of one weighted token per model family.
MODEL_WEIGHT = (
    ("claude-opus", 1.0),
    ("claude-fable", 1.0),
    ("claude-sonnet", 0.2),
    ("claude-haiku", 0.0667),
)

# Tools that stay allowed in the pause zone: they are what the assistant needs
# in order to stop cleanly (write a checkpoint, arm the waiter, report).
PAUSE_ALLOWED_TOOLS = frozenset((
    "Write", "Edit", "NotebookEdit", "Read", "TodoWrite",
    "Monitor", "TaskCreate", "TaskUpdate", "TaskStop", "TaskGet", "TaskList",
    "ScheduleWakeup", "CronCreate", "CronList", "CronDelete",
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode",
))

# A Bash/PowerShell call is let through in the pause zone when it mentions the
# guard itself -- otherwise the assistant could not even check its own state.
PAUSE_ALLOWED_MARKER = "usage_guard"

DENY_TEMPLATE = """\
{limits}
({basis})

Tool calls that spend the budget are blocked until the 5-hour window resets at
the time above. Do NOT retry, do NOT try to work around this. Follow the pause
protocol:

  1. Write down where you stopped and what comes next -- file, function, the
     exact next step -- into {checkpoint}
  2. Arm the waiter so the reset wakes you up without burning tokens. It is
     yours alone -- other dialogs pause and resume on their own waiters:
       Monitor(command='"{python}" -u "{script}" -mode wait -session {tag}',
               description='waiting for the 5-hour limit to reset',
               persistent=true, timeout_ms=3600000)
  3. Say one short sentence to the user and end the turn. Do not think, do not
     summarise at length -- every token now comes out of the next window.

When the monitor fires, re-check -mode status, re-read the checkpoint and
continue the task on your own -- that is the default, no confirmation needed.\
"""

WEEKLY_DENY_TEMPLATE = """\
{limits}
({basis})

The 7-day window is what blocks here, and it does not come back in five hours --
it unwinds over days. Waiting for it is not a plan, and neither is an unattended
run.

  1. Write the checkpoint: where you stopped and what comes next.
  2. Do NOT arm the reset waiter -- it would sit there for a day or more.
  3. Tell the user plainly that the weekly budget is nearly gone and that the
     rest of the task needs their decision. Then stop.\
"""


#-- Source 0: asking the server directly ---------------------------------------
#
# Every response from the messages endpoint carries the account's real
# utilisation and, more importantly, the real reset times:
#
#   anthropic-ratelimit-unified-5h-utilization: 0.33      <- a FRACTION, not a %
#   anthropic-ratelimit-unified-5h-reset:       1786980000 <- epoch SECONDS
#   anthropic-ratelimit-unified-7d-utilization: 0.39
#   anthropic-ratelimit-unified-7d-reset:       1787454000
#
# That removes both weaknesses of the file-based path: the 15-minute sampling
# gap and the guessed reset time, which has been two hours out.  See
# usage-limits.md next to this script, "Source 0".
#
# The dedicated endpoint api.anthropic.com/api/oauth/usage would be tidier but
# is refused: it wants scope "user:profile" and `claude setup-token` only grants
# "user:inference".  The headers carry the same numbers, so nothing is lost.

PROBE_URL = "https://api.anthropic.com/v1/messages"
PROBE_TOKEN_ENV = "CLAUDE_USAGE_LIMITS_CHECK_TOKEN"
PROBE_TOKEN_FILE = "usage-limits-token"
PROBE_BETA = "oauth-2025-04-20"
PROBE_MODEL = "claude-haiku-4-5-20251001"

# Measured round-trip: 724-1254 ms.  The timeout is what keeps a hung network
# from freezing every tool call, so it stays close to the observed worst case.
PROBE_TIMEOUT_SEC = 4

# How long a probe stays good enough to reuse, by utilisation.  The unit that
# matters is TIME, not tool calls: between two consecutive calls only seconds
# pass, and at the worst burn rate on record (3.8 %/min) 20 s moves the counter
# by 1.3 %.  Probing on every call would pay ~1 s of latency to re-read a number
# that cannot have changed.
PROBE_TTL_BANDS = ((85, 20), (70, 45), (0, 90))

# Past this, a cached probe is dropped even when the network is down: better to
# fall back to the file than to quote a number from a window that may be over.
PROBE_MAX_STALE_SEC = 600

# How far past the served reset time the waiter sleeps before believing it. The
# server rolls the window over on its own clock, and a probe fired at the exact
# second can still come back on the old one -- which would wake the assistant up
# into a window that is not yet empty and burn the checkpoint for nothing.
RESET_MARGIN_SEC = 120

# Once the margin has passed and the counter has NOT dropped, the reset is
# re-checked on this cadence instead of being declared on trust.
RESET_RECHECK_SEC = 30


def probe_ttl_sec(pct):
    for threshold, ttl in PROBE_TTL_BANDS:
        if (pct or 0) >= threshold:
            return ttl
    return PROBE_TTL_BANDS[-1][1]


def parse_probe_headers(headers):
    """Pulls the unified rate-limit headers out of a response. None if absent."""
    def num(name, scale=1.0):
        try:
            return float(headers.get("anthropic-ratelimit-unified-" + name)) * scale
        except (TypeError, ValueError):
            return None

    fh = num("5h-utilization", 100.0)
    sd = num("7d-utilization", 100.0)
    if fh is None and sd is None:
        return None                      # not a rate-limited response at all
    reset_5h = num("5h-reset")
    reset_7d = num("7d-reset")
    return {
        "t": int(time.time() * 1000),
        "fh": fh,
        "sd": sd,
        # Seconds in the header, milliseconds everywhere in this script. Mixing
        # the two would be a 1000x error in the reset time that still looks
        # plausible on screen.
        "reset_5h_ms": int(reset_5h * 1000) if reset_5h else None,
        "reset_7d_ms": int(reset_7d * 1000) if reset_7d else None,
        "status_5h": headers.get("anthropic-ratelimit-unified-5h-status"),
        "status_7d": headers.get("anthropic-ratelimit-unified-7d-status"),
    }


#-- Whose limits: one machine, several Claude accounts -------------------------
#
# A second account runs with its own CLAUDE_CONFIG_DIR (e.g. ~/.claude-worker-b).
# Everything account-bound follows that dir: the token, the probe cache, the
# watch state and the transcripts.  The token used to live only in a user-level
# environment variable, which every process on the machine inherits -- so both
# accounts probed with the same token and saw the same account's numbers.

def default_config_dir():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.normpath(os.path.join(home, ".claude"))


def claude_config_dir():
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        return os.path.normpath(os.path.expanduser(configured))
    return default_config_dir()


def is_default_account():
    return (os.path.normcase(claude_config_dir())
            == os.path.normcase(default_config_dir()))


def account_suffix():
    """'' for the default account (keeps the old file names), '-<hash>' otherwise."""
    if is_default_account():
        return ""
    digest = hashlib.sha1(os.path.normcase(claude_config_dir()).encode("utf-8"))
    return "-" + digest.hexdigest()[:8]


def temp_path(name, ext):
    return os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"),
                        "%s%s%s" % (name, account_suffix(), ext))


def probe_token_file():
    return os.path.join(claude_config_dir(), PROBE_TOKEN_FILE)


def probe_token():
    """(token, where it came from), or (None, why there is none).

    The file in the account's own config dir wins. The environment variable is
    honoured for the default account only: it is user-level, so a second
    account inherits it too, and with it the default account's numbers.
    """
    path = probe_token_file()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        if token:
            return token, path
    except OSError:
        pass
    if is_default_account():
        token = (os.environ.get(PROBE_TOKEN_ENV) or "").strip()
        if token:
            return token, PROBE_TOKEN_ENV
        return None, "no %s and no %s" % (path, PROBE_TOKEN_ENV)
    return None, "no %s" % path


def probe_server(timeout_sec=PROBE_TIMEOUT_SEC):
    """One probe. Returns the parsed headers, or None on any problem at all.

    Cheapest request that still gets the headers: one token in, one token out,
    on the cheapest model -- about 11 weighted tokens against 160 000 per
    percent, i.e. four orders of magnitude below its own measurement error.
    """
    token, _ = probe_token()
    if not token:
        return None
    body = json.dumps({
        "model": PROBE_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode("utf-8")
    req = urllib.request.Request(PROBE_URL, data=body, method="POST")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("anthropic-beta", PROBE_BETA)
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            headers = resp.headers
            resp.read()
    except urllib.error.HTTPError as exc:
        # A 429 is the single most valuable answer here -- it still carries the
        # unified headers, and it is exactly the moment the numbers matter.
        headers = exc.headers
    except Exception:
        return None
    try:
        return parse_probe_headers(headers)
    except Exception:
        return None


def get_probe(args):
    """A probe fresh enough to trust: from cache when it is, over the wire when not."""
    if getattr(args, "probe", "auto") == "off":
        return None
    # A caller that never heard of the probe -- the self-test builds its own
    # args -- gets the old file-only behaviour, and stays hermetic by default.
    cache_path = getattr(args, "probe_cache", None)
    if not cache_path:
        return None
    now_ms = int(time.time() * 1000)
    cached = read_state_file(cache_path)
    if not (isinstance(cached, dict) and cached.get("fh") is not None
            and isinstance(cached.get("t"), (int, float))):
        cached = None
    # A cached probe whose own window has already ended describes a window that
    # is over -- the same trap the file-based path fell into for hours.
    if cached and cached.get("reset_5h_ms") and now_ms > cached["reset_5h_ms"]:
        cached = None

    if cached and args.probe != "force":
        age_sec = (now_ms - cached["t"]) / 1000.0
        if 0 <= age_sec < probe_ttl_sec(cached.get("fh")):
            return cached

    fresh = probe_server()
    if fresh:
        write_state_file(cache_path, fresh)
        return fresh
    # No network and no token both land here. A recent probe still beats a file
    # sample that can be 15 minutes old; an old one does not.
    if cached and (now_ms - cached["t"]) < PROBE_MAX_STALE_SEC * 1000:
        return cached
    return None


#-- Argument types (same convention as Firmware/Utils/Script) ------------------

def file_path(value):
    value = os.path.normpath(value)
    if not os.path.isfile(value):
        raise argparse.ArgumentTypeError('the file "%s" does not exist' % value)
    return value


def dir_path(value):
    value = os.path.normpath(value)
    if not os.path.isdir(value):
        raise argparse.ArgumentTypeError('the directory "%s" does not exist' % value)
    return value


def default_history_file():
    # The desktop app writes this for the account it is signed in to, which is
    # the default one. Any other account has no such file: '' turns source 1 off
    # instead of silently reading somebody else's numbers.
    if not is_default_account():
        return ""
    # Deliberate deviation from "all input paths are explicit arguments": this
    # one is not a repo path but a per-user profile path, identical on every
    # Windows machine, so it is derived here rather than passed by every hook.
    appdata = os.environ.get("APPDATA", "")
    return os.path.join(appdata, "Claude", "plan-usage-history.json")


def default_projects_dir():
    return os.path.join(claude_config_dir(), "projects")


#-- Reading the telemetry -----------------------------------------------------

NO_HISTORY = "<no desktop-app history for this account>"


def load_samples(history_file):
    """Returns the list of usage samples, oldest first. Empty on any problem."""
    if not history_file:
        return []
    try:
        with open(history_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    samples = data.get("samples") or []
    samples = [s for s in samples if isinstance(s.get("t"), (int, float))
               and isinstance(s.get("u"), dict)]
    samples.sort(key=lambda s: s["t"])
    return samples


def find_last_reset(samples):
    """Epoch ms of the most recent moment the 5-hour counter dropped to ~zero."""
    prev = None
    reset_ms = samples[0]["t"] if samples else None
    for s in samples:
        fh = s["u"].get("fh")
        if fh is None:
            continue
        if prev is not None and fh < prev - 5:
            reset_ms = s["t"]
        prev = fh
    return reset_ms


def reset_residual(samples):
    """What the 5-hour counter read immediately after the most recent reset.

    Zero means the previous window ended with nothing else running, and the new
    one starts here.  Anything above that means a SECOND window was already in
    flight and only became visible when the first expired -- so it started
    earlier than the drop, by an unknown amount, and "drop + 5 h" is then too
    late.  Measured 2026-08-10: of 36 drops in the recorded history all the ones
    that landed on zero predicted the next reset within a minute, and the one
    that landed on 12 % was two hours out.
    """
    prev = None
    residual = 0
    for s in samples:
        fh = s["u"].get("fh")
        if fh is None:
            continue
        if prev is not None and fh < prev - 5:
            residual = fh
        prev = fh
    return residual


def model_weight(model):
    for prefix, weight in MODEL_WEIGHT:
        if model.startswith(prefix):
            return weight
    return 1.0


def weighted_tokens(usage, model):
    return model_weight(model) * (
        W_INPUT * (usage.get("input_tokens") or 0)
        + W_CACHE_WRITE * (usage.get("cache_creation_input_tokens") or 0)
        + W_CACHE_READ * (usage.get("cache_read_input_tokens") or 0)
        + W_OUTPUT * (usage.get("output_tokens") or 0)
    )


def iter_transcript_messages(projects_dir, since_ms, tail_bytes=4 * 1024 * 1024):
    """Yields (epoch_ms, weighted_tokens) for assistant messages after since_ms.

    Only files touched after since_ms are opened, and only their tail is read --
    this runs on every tool call, so it has to stay cheap.

    ONE YIELD PER API REQUEST, NOT PER LINE. Claude Code writes one transcript
    line per content block of a response (thinking, text, each tool call), and
    every one of them carries the full usage of that request. Counting lines
    counted the same request 2-9 times: measured 2026-09-14 against confirmed
    server figures, 4695 requests as 11672 lines, and 1.1-1.8 M weighted tokens
    per confirmed percent per line against 0.16-0.48 M per request -- the guard
    said 170 % while the server said 57 %.
    """
    if not os.path.isdir(projects_dir):
        return
    mtime_floor = since_ms / 1000.0 - 120.0
    pattern = os.path.join(projects_dir, "**", "*.jsonl")
    seen_requests = set()
    for path in glob.iglob(pattern, recursive=True):
        try:
            if os.path.getmtime(path) < mtime_floor:
                continue
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > tail_bytes:
                    fh.seek(size - tail_bytes)
                    fh.readline()          # drop the partial first line
                blob = fh.read()
        except OSError:
            continue
        for raw in blob.split(b"\n"):
            if b'"assistant"' not in raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            if obj.get("type") != "assistant":
                continue
            stamp = obj.get("timestamp")
            message = obj.get("message") or {}
            usage = message.get("usage") or {}
            if not stamp or not usage:
                continue
            try:
                when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                continue
            when_ms = int(when.timestamp() * 1000)
            if when_ms <= since_ms:
                continue
            key = obj.get("requestId") or message.get("id")
            if key:
                if key in seen_requests:
                    continue
                seen_requests.add(key)
            yield when_ms, weighted_tokens(usage, message.get("model") or "")


def first_message_after(projects_dir, since_ms):
    """Epoch ms of the earliest assistant message after since_ms, or None."""
    earliest = None
    for when_ms, _ in iter_transcript_messages(projects_dir, since_ms):
        if earliest is None or when_ms < earliest:
            earliest = when_ms
    return earliest


def burn_rate(samples, since_ms=None, minutes=20):
    """Percentage points of the 5-hour window spent per minute, recently.

    Samples from before the last reset are excluded: straddling a reset turns
    the difference negative and hides a burst that started right after it.
    """
    if len(samples) < 2:
        return 0.0
    now_ms = samples[-1]["t"]
    floor_ms = now_ms - minutes * 60000
    if since_ms is not None:
        floor_ms = max(floor_ms, since_ms)
    window = [s for s in samples if s["t"] >= floor_ms]
    if len(window) < 2:
        return 0.0
    first, last = window[0], window[-1]
    delta = last["u"].get("fh", 0) - first["u"].get("fh", 0)
    span_min = (last["t"] - first["t"]) / 60000.0
    if span_min <= 0 or delta <= 0:
        return 0.0
    return delta / span_min


def apply_probe(state, probe, args):
    """Overlay the server's own figures on top of the file-based state.

    What the probe replaces is measured; what it leaves alone -- the burn rate,
    the window start -- is still derived from the local sources, because the
    headers do not carry it.
    """
    now_ms = int(time.time() * 1000)
    state["source"] = "server"
    state["probe_age_sec"] = round((now_ms - probe["t"]) / 1000.0, 1)
    if probe.get("fh") is not None:
        state["five_hour_pct"] = round(probe["fh"], 1)
    if probe.get("sd") is not None:
        state["seven_day_pct"] = round(probe["sd"], 1)

    # The probe becomes the anchor in place of the file sample, so the
    # interpolation below now spans seconds rather than up to fifteen minutes.
    state["sample_age_min"] = round((now_ms - probe["t"]) / 60000.0, 1)
    state["history_stale"] = False
    state["sample_expired"] = False

    spent = sum(w for _, w in iter_transcript_messages(args.projects_dir, probe["t"]))
    delta_pct = spent / float(args.tokens_per_pct)
    state["estimated_delta_pct"] = round(delta_pct, 1)
    state["estimated_delta_tokens"] = int(spent)
    state["live_pct"] = round(
        min(100.0, (state["five_hour_pct"] or 0) + delta_pct), 1)

    if probe.get("reset_5h_ms"):
        state["reset_at"] = iso_local(probe["reset_5h_ms"])
        state["reset_in_min"] = round((probe["reset_5h_ms"] - now_ms) / 60000.0, 1)
        # Measured, not predicted: the "or EARLIER" caveat belongs to the
        # drop-plus-five-hours rule and does not apply to a served timestamp.
        state["window_start_certain"] = True
    if probe.get("reset_7d_ms"):
        state["weekly_reset_at"] = iso_local(probe["reset_7d_ms"])
    state["probe_status_5h"] = probe.get("status_5h")
    state["probe_status_7d"] = probe.get("status_7d")

    if state["burn_pct_per_min"] > 0:
        state["minutes_to_pause"] = round(
            max(0.0, args.pause_pct - state["live_pct"])
            / state["burn_pct_per_min"], 1)


def read_state(args):
    """Everything the caller needs to decide whether to keep going."""
    now_ms = int(time.time() * 1000)
    samples = load_samples(args.history_file)

    state = {
        "ok": bool(samples),
        "now": iso_local(now_ms),
        "source": "file",
        "probe_age_sec": None,
        "five_hour_pct": None,
        "seven_day_pct": None,
        "sample_age_min": None,
        "history_stale": True,
        "sample_expired": False,
        "estimated_delta_pct": 0.0,
        "live_pct": None,
        "window_start": None,
        "reset_at": None,
        "reset_in_min": None,
        "burn_pct_per_min": 0.0,
        "minutes_to_pause": None,
        "verdict": "unknown",
        "blocking_window": None,
        "warn_pct": args.warn_pct,
        "pause_pct": args.pause_pct,
        "weekly_warn_pct": args.weekly_warn_pct,
        "weekly_pause_pct": args.weekly_pause_pct,
    }
    if not samples:
        # No local history at all. A probe on its own is still a complete answer
        # -- and a better one than the file ever was -- so this is no longer the
        # dead end it used to be. Reached when the desktop app has never run on
        # this machine, or its data has been wiped.
        probe = get_probe(args)
        if probe:
            state["ok"] = True
            apply_probe(state, probe, args)
            return decide(state, args)
        state["error"] = ('no usable samples in "%s", and no probe available (%s)'
                          % (args.history_file or NO_HISTORY, probe_token()[1]))
        return state

    last = samples[-1]
    fh = last["u"].get("fh")
    state["five_hour_pct"] = fh
    state["seven_day_pct"] = last["u"].get("sd")
    age_min = (now_ms - last["t"]) / 60000.0
    state["sample_age_min"] = round(age_min, 1)
    # Two missed polls means the desktop app is not refreshing the numbers.
    state["history_stale"] = age_min > (2 * SAMPLE_PERIOD_SEC / 60.0)

    # Window start: the first message after the last reset.  Measured against
    # 22 observed resets, window_start + 5h predicts the reset within ~9 min.
    reset_ms = find_last_reset(samples)
    start_ms = first_message_after(args.projects_dir, reset_ms) if reset_ms else None
    if start_ms is None and (fh or 0) > 0 and reset_ms:
        start_ms = reset_ms          # usage came from outside Claude Code
    end_ms = None
    if start_ms:
        end_ms = start_ms + WINDOW_HOURS * 3600 * 1000
        state["window_start"] = iso_local(start_ms)
        state["reset_at"] = iso_local(end_ms)
        state["reset_in_min"] = round((end_ms - now_ms) / 60000.0, 1)
        # A reset that landed above zero means the window on show had already
        # been running - see reset_residual().  The reset time is then an upper
        # bound and the real one can be hours earlier, so say so rather than
        # quote a number that reads as measured.
        state["window_start_certain"] = reset_residual(samples) <= 2

    # A confirmed number only ever describes the window its sample was taken in.
    # When the desktop app stops writing, that sample stays the newest one for as
    # long as the app is quiet, so a window that was spent keeps reading as spent
    # hours after it has in fact rolled over -- a hard deny on a budget that is
    # empty only in the record, not in reality.
    #
    # A fresh sample always wins; this fallback applies only once the app has
    # gone quiet. And the predicted reset is an upper bound -- it can be early,
    # never late -- so being past it means the window really is over.
    state["sample_expired"] = bool(
        state["history_stale"] and end_ms is not None
        and now_ms > end_ms + EXPIRY_MARGIN_MIN * 60000)

    # Interpolate the gap since the last confirmed sample.  Once that sample has
    # expired there is no confirmed part left at all: the 5-hour counter is back
    # at zero and everything known about the new window comes from the
    # transcripts, counted from the rollover rather than from the sample.
    since_ms = max(last["t"], end_ms) if state["sample_expired"] else last["t"]
    spent = sum(w for _, w in iter_transcript_messages(args.projects_dir, since_ms))
    delta_pct = spent / float(args.tokens_per_pct)
    state["estimated_delta_pct"] = round(delta_pct, 1)
    state["estimated_delta_tokens"] = int(spent)
    if state["sample_expired"]:
        state["five_hour_pct"] = None
        live = min(100.0, delta_pct)
    else:
        live = min(100.0, (fh or 0) + delta_pct)
    state["live_pct"] = round(live, 1)

    # Burn measured inside a window that has already ended says nothing about
    # the one now running.
    rate = 0.0 if state["sample_expired"] else burn_rate(samples, since_ms=reset_ms)
    state["burn_pct_per_min"] = round(rate, 2)
    if rate > 0:
        state["minutes_to_pause"] = round(max(0.0, args.pause_pct - live) / rate, 1)

    # Source 0 wins over everything computed above: the server's own numbers need
    # no interpolation and its reset time needs no guessing.  With no probe
    # available nothing here fires and the file-based path stands exactly as it
    # did -- that is deliberate, the probe is an overlay and never a dependency.
    probe = get_probe(args)
    if probe:
        apply_probe(state, probe, args)

    return decide(state, args)


def decide(state, args):
    """Turns the numbers into ok / warn / pause.

    Split out so that the probe-only path -- no local history file at all --
    reaches exactly the same thresholds instead of a second copy of them.
    """
    # The 7-day window is judged too. It matters more than the 5-hour one for an
    # unattended run: it does not reset overnight, so hitting it turns a planned
    # pause into a stall of days.
    weekly = state["seven_day_pct"] or 0
    if weekly >= args.weekly_pause_pct:
        state["verdict"] = "pause"
        state["blocking_window"] = "seven_day"
    elif (state["live_pct"] or 0) >= args.pause_pct:
        # Read out of the state, not out of the local `live`: the probe overlay
        # above may have replaced it with a measured figure.
        state["verdict"] = "pause"
        state["blocking_window"] = "five_hour"
    elif weekly >= args.weekly_warn_pct:
        state["verdict"] = "warn"
        state["blocking_window"] = "seven_day"
    elif (state["five_hour_pct"] or 0) >= args.warn_pct:
        # WARNED ON THE CONFIRMED FIGURE, DENIED ON THE PESSIMISTIC ONE, and the
        # asymmetry is deliberate.
        #
        # The estimate interpolates from the last sample and it runs high - on the
        # night of 2026-08-17 it read 80, 85 and 91 % against confirmed readings of
        # 71 %, and three times a step was wrapped up early for nothing. Half an
        # hour of a five-hour window went to a number that was wrong.
        #
        # A deny is different: refusing tools a few minutes early costs one pause,
        # while refusing them a minute late costs the whole turn and the checkpoint
        # with it. So the hard stop keeps the pessimistic number and only the
        # warning waits for a confirmed one.
        #
        # READ OUT OF THE STATE, NOT OUT OF fh: once the sample has expired the
        # state's copy is None, because a confirmed figure describes the window its
        # sample was taken in and that window is over. The local still holds the
        # stale number, and using it warned on an empty window - which the
        # self-test caught the moment this was written.
        state["verdict"] = "warn"
        state["blocking_window"] = "five_hour"
    else:
        state["verdict"] = "ok"
    return state


def iso_local(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


#-- Modes ---------------------------------------------------------------------

def mode_status(args):
    state = read_state(args)
    if not state["ok"]:
        print("usage-guard: %s" % state.get("error", "no data"))
        return 1

    # The canonical block first, byte-for-byte what the hooks report, so that the
    # human view and the assistant's view can never drift apart.
    print(limits_block_of(state))
    print()

    bar_len = 40
    filled = int(round(bar_len * (state["live_pct"] or 0) / 100.0))
    bar = "#" * filled + "." * (bar_len - filled)

    print("5-hour window : %5.1f%%  [%s]" % (state["live_pct"], bar))
    if state["sample_expired"]:
        print("  confirmed   : none usable -- the newest sample is %.0f min old and"
              % state["sample_age_min"])
        print("                belongs to the window that ended %s" % state["reset_at"])
        print("  estimated   : %.1f%% (%s weighted tokens since that rollover)"
              % (state["estimated_delta_pct"],
                 "{:,}".format(state["estimated_delta_tokens"])))
    else:
        if state["source"] == "server":
            print("  measured    : %s%% (from the server, probe %.0f s old)"
                  % (fmt_pct(state["five_hour_pct"]), state["probe_age_sec"] or 0))
        else:
            print("  confirmed   : %s%% (sample %.1f min old%s)"
                  % (state["five_hour_pct"], state["sample_age_min"],
                     ", STALE" if state["history_stale"] else ""))
        print("  estimated + : %.1f%% (%s weighted tokens since the sample)"
              % (state["estimated_delta_pct"],
                 "{:,}".format(state["estimated_delta_tokens"])))
    weekly = state["seven_day_pct"] or 0
    note = ""
    if weekly >= args.weekly_pause_pct:
        note = "  <-- SPENT: does not reset overnight, an unattended run is pointless"
    elif weekly >= args.weekly_warn_pct:
        note = "  <-- getting tight; this one unwinds over days, not hours"
    print("7-day window  : %s%%%s" % (fmt_pct(state["seven_day_pct"]), note))
    if state.get("weekly_reset_at"):
        print("                resets %s" % state["weekly_reset_at"])
    print()
    if state["sample_expired"]:
        print("window        : rolled over at %s; the new one cannot be dated"
              % state["reset_at"])
        print("                until the desktop app writes a sample again")
    elif state["window_start"]:
        print("window started: %s" % state["window_start"])
        if state.get("window_start_certain", True):
            print("resets at     : %s  (in %.0f min)"
                  % (state["reset_at"], state["reset_in_min"]))
        else:
            print("resets at     : %s or EARLIER  (in <= %.0f min)"
                  % (state["reset_at"], state["reset_in_min"]))
            print("                the last reset landed above zero, so a second")
            print("                window was already running and this is only")
            print("                an upper bound -- it has been 2 h out")
    else:
        print("window        : idle, no messages since the last reset")
    print("burn rate     : %.2f %%/min" % state["burn_pct_per_min"])
    if state["minutes_to_pause"] is not None:
        print("headroom      : ~%.0f min until the %d%% pause threshold"
              % (state["minutes_to_pause"], args.pause_pct))
    print()
    print("verdict       : %s%s  (5h: warn %d%% / pause %d%%; 7d: warn %d%% / pause %d%%)"
          % (state["verdict"].upper(),
             "" if not state["blocking_window"] else " on the %s window"
             % ("7-day" if state["blocking_window"] == "seven_day" else "5-hour"),
             args.warn_pct, args.pause_pct,
             args.weekly_warn_pct, args.weekly_pause_pct))
    return 0


def mode_json(args):
    print(json.dumps(read_state(args), ensure_ascii=False, indent=2))
    return 0


def mode_probe(args):
    """One probe over the wire, printed raw. For checking the plumbing by hand.

    Always goes to the network -- it never reads the cache, because the point of
    running it by hand is to find out whether the token and the endpoint are
    still alive. It does *write* the cache, so a manual check also refreshes
    what the hooks will see instead of throwing the measurement away.
    """
    probe = probe_server()
    if probe:
        cache_path = getattr(args, "probe_cache", None)
        if cache_path:
            write_state_file(cache_path, probe)
    if not probe:
        token, origin = probe_token()
        if not token:
            print("probe unavailable: %s" % origin)
            print("mint one with `claude setup-token` under this account and "
                  "save it to %s" % probe_token_file())
        else:
            print("probe failed (token from %s): no unified rate-limit headers "
                  "came back" % origin)
            print("network down, token rejected, or the endpoint has changed")
        return 1
    print(limits_block(
        probe.get("fh"),
        iso_local(probe["reset_5h_ms"]) if probe.get("reset_5h_ms") else None,
        probe.get("sd"),
        iso_local(probe["reset_7d_ms"]) if probe.get("reset_7d_ms") else None))
    print("(measured just now; server says 5h %s, 7d %s)"
          % (probe.get("status_5h") or "?", probe.get("status_7d") or "?"))
    return 0


def fmt_pct(value):
    """41.0 -> '41', 39.5 -> '39.5', None -> '?'.

    The file gives whole percents, the server a fraction with decimals. Without
    this the same line reads '7d 33%' one minute and '7d 41.0%' the next.
    """
    if value is None:
        return "?"
    try:
        return "%g" % round(float(value), 1)
    except (TypeError, ValueError):
        return str(value)


def limits_block(five_pct, five_reset, seven_pct, seven_reset):
    """The canonical limits report.

    One shape, printed identically everywhere the numbers surface -- the two
    hooks, -mode status, -mode probe and the deny text -- so that a figure read
    in one place is never a different figure read in another. ASCII only: hook
    stdout is decoded with the console codepage and anything outside it reaches
    the assistant as replacement characters.
    """
    def tenth(value):
        # One decimal, always -- including the trailing ".0". A field that
        # changes width between readings is harder to compare at a glance, and
        # near the ceiling the tenth is the part that moves.
        if value is None:
            return "?"
        try:
            return "%.1f" % float(value)
        except (TypeError, ValueError):
            return str(value)

    return "\n".join((
        "Limits:",
        "5h: %s%%, R: %s" % (tenth(five_pct), five_reset or "unknown"),
        "7d: %s%%, R: %s" % (tenth(seven_pct), seven_reset or "unknown"),
    ))


def limits_block_of(state):
    # The headline is the pessimistic figure, because that is the one every
    # threshold is judged on; the measured/estimated split goes in the notes.
    return limits_block(state["live_pct"], state["reset_at"],
                        state["seven_day_pct"], state.get("weekly_reset_at"))


def state_notes(state):
    """Everything qualifying the two numbers, or an empty list when nothing does."""
    notes = []
    if state["sample_expired"]:
        notes.append("5h estimated only -- the window rolled over and no "
                     "confirmed sample has landed since")
    elif state["estimated_delta_pct"]:
        notes.append("5h = %s%% %s + %.0f%% est"
                     % (fmt_pct(state["five_hour_pct"]),
                        "measured" if state["source"] == "server" else "confirmed",
                        state["estimated_delta_pct"]))
    if state["source"] != "server":
        # WHY, NOT ONLY THAT. Without the token the probe is skipped silently, and on 2026-09-15 the guard had been
        # running on the desktop app's file for days before anyone could tell from this line.
        token, origin = probe_token()
        notes.append("local files, not measured" if token
                     else "local files, not measured - %s" % origin)
    if state["burn_pct_per_min"]:
        notes.append("burn %.1f%%/min" % state["burn_pct_per_min"])
    if state["minutes_to_pause"] is not None and state["verdict"] != "ok":
        notes.append("~%.0f min of headroom" % state["minutes_to_pause"])
    if state["history_stale"]:
        notes.append("STALE telemetry")
    return notes


def one_liner(state):
    """The budget report injected into the assistant's context."""
    line = limits_block_of(state)
    notes = state_notes(state)
    if notes:
        line += "\n(%s)" % "; ".join(notes)

    if state["verdict"] == "pause":
        line += "\nPause now: checkpoint, arm the waiter, stop. See the long-run skill."
    elif state["verdict"] == "warn":
        line += ("\nWrap up the current step and do not start a new one -- "
                 "checkpoint while there is still budget to write it.")
    return line


def read_state_file(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def write_state_file(path, data):
    # Written to a temp file and renamed into place. PreToolUse fires on every
    # tool call of every open dialog and they share these files, so a reader
    # catching a half-written one would throw away a good probe and pay for a
    # fresh request. os.replace is atomic on Windows as well as POSIX.
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def emit_hook_context(event_name, text):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": text,
        }
    }, ensure_ascii=False))


def mode_prompt(args):
    """UserPromptSubmit hook: state the budget on every user message."""
    try:
        sys.stdin.read()
    except Exception:
        pass
    state = read_state(args)
    if not state["ok"]:
        return 0
    emit_hook_context("UserPromptSubmit", one_liner(state))
    return 0


def mode_stop(args):
    """Stop hook: show the user where the budget stands once the turn has ended.

    The UserPromptSubmit line reports the budget as it was when the user pressed
    enter; by the time the answer is written the turn has spent some of it, and
    that increment is exactly what nobody could see. This closes it.

    Output MUST be JSON with a "systemMessage" field: plain stdout from a Stop
    hook only reaches the transcript view (ctrl+o), not the conversation.

    Wired with -probe force. A stale-by-90-seconds figure is fine before a tool
    call and wrong here -- the number is supposed to include the turn that just
    finished. One request per turn, about a second, at the one moment in the
    cycle when nobody is waiting on it.
    """
    try:
        sys.stdin.read()
    except Exception:
        pass
    state = read_state(args)
    if not state["ok"]:
        return 0
    text = limits_block_of(state)
    notes = state_notes(state)
    if notes:
        text += "\n(%s)" % "; ".join(notes)
    print(json.dumps({"systemMessage": text}, ensure_ascii=False))
    return 0


def mode_watch(args):
    """PostToolUse hook: speak up only once the budget gets tight.

    A long unattended run has no user messages, so this is the only thing that
    reports the budget while it burns. Throttled, otherwise it would repeat on
    every single tool call for the whole warn zone.
    """
    try:
        payload = json.loads((sys.stdin.read() or "").lstrip(" \t\r\n") or "{}")
    except Exception:
        payload = {}
    state = read_state(args)
    if not state["ok"] or state["verdict"] == "ok":
        return 0

    now = time.time()
    band = int((state["live_pct"] or 0) / 5)      # a fresh line every 5 points
    # Throttled per session, not per machine: two dialogs open at once share the
    # account budget, and one must not swallow the other's warning.
    key = str(payload.get("session_id") or "unknown")
    saved = read_state_file(args.state_file)
    if not isinstance(saved, dict):
        saved = {}
    mine = saved.get(key) or {}
    quiet_for = now - float(mine.get("last_notice_at") or 0)
    if band == mine.get("last_band") and quiet_for < args.notice_every_sec:
        return 0

    saved = {k: v for k, v in saved.items()
             if isinstance(v, dict) and now - float(v.get("last_notice_at") or 0) < 86400}
    saved[key] = {"last_notice_at": now, "last_band": band}
    write_state_file(args.state_file, saved)
    emit_hook_context("PostToolUse", one_liner(state))
    return 0


def mode_hook(args):
    """PreToolUse hook. Denies budget-spending tools once the window is spent.

    Any internal failure must end in exit 0 with no output: a broken guard is
    not allowed to block the session.
    """
    try:
        # A BOM shows up whenever the payload is piped in by PowerShell.
        raw = (sys.stdin.read() or "").lstrip(" \t\r\n")
        payload = json.loads(raw or "{}")
    except Exception:
        # Cannot tell which tool this is, so the whitelist below cannot work
        # either -- and a deny would then also block Write and Monitor, turning
        # a soft pause into a deadlock with no way to checkpoint. Let it pass
        # and complain on stderr instead.
        sys.stderr.write("usage_guard: unreadable PreToolUse payload, allowing\n")
        return 0
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    # The hook is the only place that knows which dialog this is, so it is the
    # hook that stamps the session into the paths it hands back.
    tag = session_tag(args.session or payload.get("session_id"))

    state = read_state(args)
    if state["verdict"] != "pause":
        return 0
    if tool_name in PAUSE_ALLOWED_TOOLS:
        return 0
    if tool_name in ("Bash", "PowerShell"):
        command = str(tool_input.get("command") or "")
        if PAUSE_ALLOWED_MARKER in command:
            return 0

    # The same block the hooks report, so the numbers in the deny text cannot
    # disagree with the numbers that led to it.
    limits = limits_block_of(state)
    notes = state_notes(state)
    basis = "; ".join(notes) if notes else "measured just now"
    if state["blocking_window"] == "seven_day":
        reason = WEEKLY_DENY_TEMPLATE.format(limits=limits, basis=basis)
    else:
        reason = DENY_TEMPLATE.format(
            limits=limits,
            basis=basis,
            tag=tag,
            checkpoint=checkpoint_path(args.checkpoint_dir, tag),
            python=sys.executable.replace("\\", "/"),
            script=os.path.abspath(__file__).replace("\\", "/"),
        )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }, ensure_ascii=False))
    return 0


def mode_wait(args):
    """Blocks until the 5-hour window resets. Prints exactly one line, then exits.

    Run it detached: the whole point is that waiting out a 5-hour limit must
    cost zero tokens.  Every line printed here becomes a chat notification, so
    there are deliberately no heartbeats -- only terminal events.
    """
    started = time.time()
    samples = load_samples(args.history_file)
    baseline = samples[-1]["u"].get("fh", 0) if samples else 0
    last_sample_t = samples[-1]["t"] if samples else 0
    last_progress = time.time()

    tag = session_tag(args.session)
    checkpoint = checkpoint_path(args.checkpoint_dir, tag)
    stop_files = (args.stop_file, "%s-%s" % (args.stop_file, tag))

    # With a probe there is a served reset time, so the waiter can sleep to it
    # instead of watching for a drop. Without one, target_ms stays None and the
    # file-based drop detection below does the whole job exactly as before.
    margin_sec = getattr(args, "reset_margin_sec", RESET_MARGIN_SEC)
    recheck_sec = getattr(args, "reset_recheck_sec", RESET_RECHECK_SEC)
    target_ms = None
    armed = get_probe(args)
    if armed:
        baseline = max(baseline, armed.get("fh") or 0)
        if armed.get("reset_5h_ms"):
            target_ms = armed["reset_5h_ms"] + margin_sec * 1000

    while True:
        waiting_for_target = (target_ms is not None
                              and time.time() * 1000 < target_ms)
        time.sleep(args.poll_sec if waiting_for_target else recheck_sec)

        # The kill switch: cancelling must not require the user to spend tokens
        # talking to an assistant that is already out of budget.
        if stop_requested(stop_files, started):
            emit("USAGE-GUARD CANCELLED (%s): a stop flag was raised. Do not "
                 "resume the task. Confirm to the user that the auto-resume is "
                 "off for this dialog." % tag)
            return 0

        if (time.time() - started) / 60.0 > args.max_wait_min:
            emit("USAGE-GUARD TIMEOUT: waited %d min and the window has still "
                 "not rolled over. Check the state manually." % args.max_wait_min)
            return 0

        if target_ms is not None:
            if time.time() * 1000 < target_ms:
                continue          # not there yet, and checking costs a request

            # The margin has passed. Confirm against the server rather than
            # trusting the clock: this is the whole reason for the re-check.
            fresh = probe_server()
            if fresh:
                cache_path = getattr(args, "probe_cache", None)
                if cache_path:
                    write_state_file(cache_path, fresh)
                fh = fresh.get("fh") or 0
                if fh < baseline - 30 or (baseline >= 40 and fh <= 5):
                    emit("USAGE-GUARD RESET (%s): the 5-hour window rolled over "
                         "(was %.1f%%). Measured just now:\n%s\n"
                         "Re-read %s and continue that task on your own, without "
                         "asking the user. The numbers above are measured, but "
                         "re-check -mode status if anything has to be decided on "
                         "them."
                         % (tag, baseline,
                            limits_block(
                                fresh.get("fh"),
                                iso_local(fresh["reset_5h_ms"])
                                if fresh.get("reset_5h_ms") else None,
                                fresh.get("sd"),
                                iso_local(fresh["reset_7d_ms"])
                                if fresh.get("reset_7d_ms") else None),
                            checkpoint))
                    return 0
                # Still not empty. Re-arm to whatever reset the server names now
                # and keep re-checking on the recheck cadence until it is.
                if fresh.get("reset_5h_ms"):
                    moved = fresh["reset_5h_ms"] + margin_sec * 1000
                    if moved > time.time() * 1000:
                        target_ms = moved
                continue
            # The probe failed. Fall through to the file, which is what the
            # waiter ran on before there was a probe at all.

        samples = load_samples(args.history_file)
        if not samples:
            emit("USAGE-GUARD ERROR: cannot read %s -- resume manually."
                 % (args.history_file or NO_HISTORY))
            return 0

        current = samples[-1]
        fh = current["u"].get("fh", 0)

        if current["t"] != last_sample_t:
            last_sample_t = current["t"]
            last_progress = time.time()

        # The window reset: a drop of that size never happens any other way.
        if fh < baseline - 30 or (baseline >= 40 and fh <= 5):
            emit("USAGE-GUARD RESET: the 5-hour window is back to %d%% (was "
                 "%d%%). Re-check the real numbers with -mode status BEFORE "
                 "starting anything -- this reading came from the local file, "
                 "not the server. Then re-read %s and continue that task on "
                 "your own, without asking the user."
                 % (fh, baseline, checkpoint))
            return 0

        # Someone kept spending, so the baseline moves up with it.
        if fh > baseline:
            baseline = fh

        # Only meaningful while the file is what we are watching. With a served
        # reset time in hand a frozen file decides nothing, and bailing out on it
        # would abandon a wait that was going fine.
        stale_min = (time.time() - last_progress) / 60.0
        if target_ms is None and stale_min > args.stale_min:
            emit("USAGE-GUARD STALE: %s has not been updated for %d min -- the "
                 "desktop app is probably not refreshing usage. Current reading "
                 "is %d%%, treat it as unreliable."
                 % (os.path.basename(args.history_file), int(stale_min), fh))
            return 0
        # The overall timeout is checked at the top of the loop, so that it also
        # bounds the stretch spent sleeping towards a served reset time.


def session_tag(raw):
    """A short, filename-safe id for one dialog. 'shared' when unknown."""
    if not raw:
        return "shared"
    safe = "".join(c for c in str(raw) if c.isalnum() or c in "-_")
    return (safe[:8] or "shared")


def checkpoint_path(checkpoint_dir, tag):
    return os.path.join(checkpoint_dir, "CHECKPOINT-%s.md" % tag).replace("\\", "/")


def stop_requested(paths, since):
    """True if any stop flag was (re)created after this waiter started.

    Deliberately not consume-on-read: with several dialogs paused at once, the
    first waiter to poll would eat a global flag meant for all of them. Judging
    by mtime instead lets one flag stop every waiter that is already running,
    while a stale flag from an earlier pause is ignored by definition.
    """
    for path in paths:
        if not path:
            continue
        try:
            if os.path.getmtime(path) >= since:
                return True
        except OSError:
            continue
    return False


def emit(text):
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


#-- Entry point ---------------------------------------------------------------

def main():
    handlers = {
        "status": mode_status,
        "json": mode_json,
        "probe": mode_probe,
        "prompt": mode_prompt,
        "stop": mode_stop,
        "watch": mode_watch,
        "hook": mode_hook,
        "wait": mode_wait,
    }
    return handlers[g_args.mode](g_args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Watches the Claude 5-hour / 7-day usage windows.")
    parser.add_argument("-mode", dest="mode", required=True,
                        choices=("status", "json", "probe", "prompt", "stop",
                                 "watch", "hook", "wait"),
                        help="what to do")
    parser.add_argument("-history-file", dest="history_file",
                        default=default_history_file(),
                        help="plan-usage-history.json written by the desktop app")
    parser.add_argument("-projects-dir", dest="projects_dir",
                        default=default_projects_dir(),
                        help="<CLAUDE_CONFIG_DIR or ~/.claude>/projects, holding "
                             "the session transcripts")
    # Raised 2026-08-17 from 88/92 and 93. The old margin paid for the ESTIMATE's
    # error: the figure could be a 15-minute-old sample plus an interpolation
    # good to about +-40 %. The probe made it a measurement seconds old, so the
    # same safety now fits in a smaller gap. At the worst burn rate on record
    # (3.8 %/min) the 5 points between pause and the ceiling are ~80 seconds --
    # enough for the checkpoint, which is whitelisted and costs well under 1 %.
    parser.add_argument("-warn-pct", dest="warn_pct", type=int, default=90,
                        help="start warning at this %% of the 5-hour window")
    parser.add_argument("-pause-pct", dest="pause_pct", type=int, default=95,
                        help="deny budget-spending tools at this %%")
    parser.add_argument("-weekly-warn-pct", dest="weekly_warn_pct", type=int,
                        default=95,
                        help="start warning at this %% of the 7-day window")
    parser.add_argument("-weekly-pause-pct", dest="weekly_pause_pct", type=int,
                        default=98,
                        help="deny budget-spending tools at this %% of the 7-day window")
    parser.add_argument("-tokens-per-pct", dest="tokens_per_pct", type=int,
                        default=160000,
                        help="weighted tokens per 1%% of the window (calibration)")
    parser.add_argument("-session", dest="session", default=None,
                        help="dialog id; the hook takes it from its own payload. "
                             "Keeps checkpoints and stop flags of parallel "
                             "dialogs from colliding")
    parser.add_argument("-checkpoint-dir", dest="checkpoint_dir",
                        default=".claude/tmp/long-run",
                        help="where the pause protocol saves CHECKPOINT-<session>.md")
    parser.add_argument("-state-file", dest="state_file",
                        default=temp_path("claude-usage-guard-state", ".json"),
                        help="watch mode: throttling state (per account, not per repo)")
    parser.add_argument("-notice-every-sec", dest="notice_every_sec", type=int,
                        default=600,
                        help="watch mode: minimum seconds between two notices")
    parser.add_argument("-stop-file", dest="stop_file",
                        default=os.path.join(
                            os.environ.get("TEMP") or os.path.expanduser("~"),
                            "claude-usage-guard-stop"),
                        help="wait mode: creating this file cancels every armed "
                             "auto-resume; '<this>-<session>' cancels one dialog")
    parser.add_argument("-probe", dest="probe",
                        choices=("auto", "off", "force"), default="auto",
                        help="ask the server directly for the real numbers. "
                             "auto: reuse a cached probe while it is fresh "
                             "enough (%s); force: always go over the wire; "
                             "off: local files only"
                             # '%%%%' -> '%%' here -> '%' once argparse
                             # %-formats the help itself (3.14+ checks it eagerly)
                             % ", ".join("%s%%%% -> %ss" % b for b in PROBE_TTL_BANDS))
    parser.add_argument("-probe-cache", dest="probe_cache",
                        default=temp_path("claude-usage-guard-probe", ".json"),
                        help="where the last probe is cached, so that most tool "
                             "calls cost no network round-trip at all. Per "
                             "account: one account must never read another's")
    parser.add_argument("-poll-sec", dest="poll_sec", type=int, default=60,
                        help="wait mode: seconds between polls")
    parser.add_argument("-reset-margin-sec", dest="reset_margin_sec", type=int,
                        default=RESET_MARGIN_SEC,
                        help="wait mode: how far past the served reset time to "
                             "wait before believing it (default %d)"
                             % RESET_MARGIN_SEC)
    parser.add_argument("-reset-recheck-sec", dest="reset_recheck_sec", type=int,
                        default=RESET_RECHECK_SEC,
                        help="wait mode: re-check cadence once the margin has "
                             "passed but the counter has not dropped "
                             "(default %d)" % RESET_RECHECK_SEC)
    parser.add_argument("-max-wait-min", dest="max_wait_min", type=int, default=400,
                        help="wait mode: give up after this many minutes")
    parser.add_argument("-stale-min", dest="stale_min", type=int, default=25,
                        help="wait mode: report a frozen history file after this")
    g_args = parser.parse_args()

    if g_args.history_file:
        g_args.history_file = os.path.normpath(g_args.history_file)
    g_args.projects_dir = os.path.normpath(g_args.projects_dir)

    if g_args.mode == "status":
        print("The given arguments are:")
        for name, value in sorted(vars(g_args).items()):
            print("  %-16s %s" % (name, value))
        print()

    sys.exit(main())
