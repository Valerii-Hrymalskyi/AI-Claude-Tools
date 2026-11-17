"""Measure the usage estimator against the confirmed numbers, and say how far off it is.

WHY THIS EXISTS. usage_guard.py interpolates the gap since its last known-good
number from weighted tokens in the local transcripts. The interpolation was
believed and never checked. This checks it.

STILL RELEVANT AFTER 2026-08-17. That day the guard learned to ask the server
directly (-mode probe), so this estimator stopped being the main source and became
the correction on top of a measured number - seconds' worth, not minutes'. It is
still worth measuring for the same reason as before: whenever the probe cannot
run, it is what remains. See context/usage-limits.md, "Source 2".

HOW. The app writes one confirmed sample every five minutes. Between two
consecutive samples inside one window the truth is the difference of their `fh`,
and the estimate is what the guard would have predicted for that same interval.
Every pair is one measurement; there are hundreds of them sitting in the history
file already.

WHAT IT FOUND, 2026-08-17: the estimator reports 0.42 of the real spend - 733
points against 1749 over 304 intervals - and in most intervals it reports zero
while the real figure rose. It reads the Claude session storage only, so it is
blind to every conversation held in the desktop app, and those spend the same
window.
Inside its own project it overshoots instead. It is wrong in both directions and
no constant fixes that.

    E:/Development/AI/Claude/Utils/Bin/Windows/Python3/python.exe E:/Development/AI/Claude/Projects/UsageGuard/calibrate_usage_estimate.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import usage_guard as guard

HISTORY = os.path.expandvars(r"%APPDATA%\Claude\plan-usage-history.json")
PROJECTS = os.path.expandvars(r"%USERPROFILE%\.claude\projects")

#Longer than this between two samples means the app was not running, so the pair
#says nothing about a five-minute interval.
MAX_GAP_MIN = 12

#The same figure usage_guard.py defaults to; it is a command-line option there, so
#it is repeated rather than imported.
TOKENS_PER_PCT = 160000.0


def main():
    samples = guard.load_samples(HISTORY)
    print("samples in the history file: %d" % len(samples))

    rows = []
    for older, newer in zip(samples, samples[1:]):
        real = (newer["u"]["fh"] or 0) - (older["u"]["fh"] or 0)
        minutes = (newer["t"] - older["t"]) / 60000.0

        #A drop is a window rollover, not a spend; a gap is the app being closed.
        if (real <= 0) or (minutes <= 0) or (minutes > MAX_GAP_MIN):
            continue

        spent = sum(w for when, w in guard.iter_transcript_messages(PROJECTS, older["t"])
                    if when <= newer["t"])
        rows.append((minutes, real, spent / float(TOKENS_PER_PCT)))

    if not rows:
        print("no usable pairs - the app has not been running long enough")
        return 0

    print("")
    print("%9s %8s %9s %8s" % ("minutes", "real", "estimated", "ratio"))
    for minutes, real, guess in rows[-20:]:
        print("%9.1f %8d %9.1f %8.2f" % (minutes, real, guess, guess / real))

    real_total = sum(r[1] for r in rows)
    guess_total = sum(r[2] for r in rows)
    blind = sum(1 for r in rows if r[2] < 0.05)

    print("")
    print("over %d intervals: real %d points, estimated %.0f -- the estimator runs %.2fx"
          % (len(rows), real_total, guess_total, guess_total / real_total))
    print("intervals where it saw nothing at all while the real figure rose: %d of %d"
          % (blind, len(rows)))
    print("")
    print("A ratio far from 1.00 is not a calibration problem to be tuned away - see")
    print("the note in the file header for what it actually means.")

    return 0


sys.exit(main())
