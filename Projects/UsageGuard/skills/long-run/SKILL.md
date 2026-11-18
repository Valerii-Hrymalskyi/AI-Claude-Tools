---
name: long-run
description: Carry a task across a usage limit — checkpoint, wait out the reset for free, resume by itself. This is the default behaviour on every task, not a mode: use it the moment the usage guard denies a tool or reports PAUSE, whenever a task will obviously outlast the current window, and whenever the user steps away ("я спать", "продолжай сам"). Also covers cancelling an armed auto-resume and pausing a headless run (`claude -p`, a TaskDelegator agent), which must not wait but hand back to its lead.
---

# Carrying a task across a usage limit

This skill is an entry point, not a manual. The numbers, the data sources and
why they are trustworthy live in `<guard>/usage-limits.md` — read it once per
session before the first pause.

**Paths.** `<python>` and `<guard>` below stand for this machine's interpreter
and UsageGuard folder. Take them from the `-mode status` command in the
"usage guard" section of your user `CLAUDE.md`, or from the hook commands in
`settings.json`: `install_user_hooks.py` writes both with the real paths.

## The one rule

**Never let the window reach 100 %.** A 429 is not a pause: it kills the turn
mid-thought, nothing is written down, and the session sits idle until a human
comes back — which is exactly what happened on 2026-08-03 (limit reset at 3:40,
work resumed at 8:30 only because the user woke up).

## Before starting

Ordinary work needs no preparation at all — the checkpoint is written at the
moment of the pause, out of context that is still fully there at 95 %. The two
steps below are for work that is *planned* to outlast the window, or that the
user will walk away from:

1. Check the budget:

   ```
   <python> <guard>/usage_guard.py -mode status
   ```

   `resets at` is when the current window ends; say in one line how many windows
   the task will take. Work planned past that point will be interrupted — that
   is fine, it is what this skill is for.

2. Create `.claude/tmp/long-run/CHECKPOINT-<session>.md` (gitignored) and keep it
   current **from the start**, not from the first warning. Contents: the goal,
   the plan as a checklist, what is already done, and the exact next step —
   file, function, the command to run. Assume the reader is a fresh session with
   no memory of this conversation, because after a compaction it effectively is.
   Keeping it live from the start is what protects against a compaction, not
   against the limit.

   The exact filename is handed to you by the guard — never invent it, and never
   write to another dialog's file.

## While working

The budget arrives on its own — no need to poll it:

- a `Limits: …` block on every user message (`UserPromptSubmit` hook);
- the same from 90 %, injected after a tool call, throttled (`PostToolUse`);
- a hard deny of budget-spending tools at 95 % (`PreToolUse`);
- the closing numbers to the user after each turn (`Stop`, with `-probe force`).

Since 2026-08-17 those numbers are **measured, not estimated**: the guard asks the
server and gets the true reset time of both windows. That is what allowed 80/92 to
become 90/95 — the old thresholds were padding against an estimator that was wrong
in both directions. Do not read an old 80 % or 92 % in some other document as
current; the code is in `<guard>/usage_guard.py`.

Act on the 90 % line when it appears: finish the current step, do not begin a
new one. Update the checkpoint whenever a step completes. `-mode status` by hand
is only for planning ahead — how much of the task fits before the reset.

## The pause protocol

Triggered by `verdict: PAUSE`, or by a hook denial, or by your own judgement
that the remaining headroom will not cover the next step.

**If the optional `PreToolUse` hook is wired, its deny message already contains
this filled in** — the checkpoint path and the exact `Monitor` command, both
stamped with this dialog's session id. Copy it from there rather than
reconstructing it. Otherwise fill in `<python>`, `<guard>` and `<tag>` yourself.

1. **Write the checkpoint.** Where you stopped, what is next, anything learned
   that is not yet in a file. This is the only thing that survives. Anything
   you started that is still running outside this conversation — a test run,
   a build, a flash, a hardware session in a terminal tab: its command, where
   it runs, when it should end. Leave it alone: killing a run half-way can
   leave hardware or files in a worse state than letting it finish. If the
   project has its own rules for such runs (its `CLAUDE.md` and the documents
   it points to), they come first.
2. **Arm the waiter.** It is a plain background process — waiting costs zero
   tokens, unlike a polling loop:

   ```
   Monitor(
     command: '"<python>" -u "<guard>/usage_guard.py" -mode wait -session <tag>',
     description: 'waiting for the 5-hour usage window to reset',
     persistent: true,
     timeout_ms: 3600000
   )
   ```

   It prints nothing until the window actually resets, then exactly one line
   naming the checkpoint to resume from. `USAGE-GUARD STALE` or
   `USAGE-GUARD TIMEOUT` instead means something is wrong with the telemetry —
   report it to the user and stop rather than guessing.
3. **Stop.** One short sentence to the user: percentage, reset time, what will
   happen next. No summary, no thinking out loud — every token spent now is
   borrowed from the next window.

## On resume

The monitor event arrives on its own; it is **not** a message from the user and
needs no confirmation — resuming is the default, silence is consent. Re-check
`-mode status` (a reset that did not actually happen would burn the new window
in minutes), re-read `CHECKPOINT.md` — do not trust your memory of the plan, the
context may have been compacted — then continue from the exact next step. Do not
re-explain what happened, do not re-derive finished work.

The resumed run stays inside the task that was paused. Anything irreversible or
outward-facing — a push, a force-push, a delete, anything leaving the machine —
still waits for the user, exactly as it would have before the pause. "Continue
without asking" is about the pause, not about the guardrails.

**Consent does not survive a pause.** Whatever needed the user's go before it
started — a run on hardware, a flash, a deploy, a write outside the project —
is neither started nor repeated on resume on the strength of a go given before
the pause, a line in the checkpoint or a journal entry. First read what the
interrupted run left — its output, its exit code, the state of what it drove —
and report it; the next run, or a repeat of one that failed or was cut off,
waits for a fresh "yes". Re-read the project's own rules for such runs before
asking: a project may say more than this skill does.

## Running headless (no user, e.g. a TaskDelegator agent)

You are headless when you run as `claude -p` with nobody on the other end —
typically as an agent of the TaskDelegator MCP server, briefed by a lead Claude
session: the first message is a self-contained brief, there are no earlier
user turns, and `Monitor` is not available. Nothing can wake you: the run ends
with your turn and takes any waiter with it. So on PAUSE:

1. **Write the checkpoint** as above, under `.claude/tmp/long-run/` of the
   working directory. In a read-only job you cannot write files: put the
   checkpoint's content into the answer instead.
2. **Do not arm the waiter.**
3. **End the turn with one last line**, for the lead to act on:
   `PAUSED: 5h <percent>, resets at <time>; checkpoint: <path or "in this answer">; next: <the exact next step>`.

Whoever started the run resumes it in the same session after the reset: a lead
Claude with `task_reply`, a script or a person with `claude -p --resume <session>`.
Then do what "On resume" says: re-check `-mode status`, re-read the checkpoint,
continue from the exact next step. The same goes for a 7-day block, except that
the line says so and the lead decides.

## Cancelling an armed auto-resume

The user says "стоп" / "останови" while the waiter is armed → `TaskStop` the
monitor, confirm in one line, do not resume. Any other message while paused is
**not** a cancellation: answer it and leave the waiter alone.

There is also a zero-token kill switch, for when the budget is too thin to even
talk. Creating a flag file makes the waiter exit with `USAGE-GUARD CANCELLED`
within a minute:

```
New-Item -ItemType File "$env:TEMP\claude-usage-guard-stop" -Force        # all dialogs
New-Item -ItemType File "$env:TEMP\claude-usage-guard-stop-<tag>" -Force  # just one
```

A waiter stops on a flag whose mtime is newer than its own start, so one global
flag cancels every dialog paused at that moment, while a flag left over from an
earlier pause is ignored rather than poisoning the next one.

## Several dialogs at once

They are fully independent: each arms its own waiter, writes its own
`CHECKPOINT-<session>.md`, and resumes itself. Never touch another dialog's
checkpoint, and do not assume a pause in one means a pause in the other.

What they *do* share is the account budget. Two dialogs resuming at the same
reset spend the new window roughly twice as fast, so expect both to pause again
sooner — that is working as intended, not a fault.

## Limits worth knowing

- The waiter lives in this session. If the Claude Desktop app is closed or
  restarted, it dies and nothing will resume — say so when the user leaves.
- In the desktop app the `Monitor` tool caps a watch at 30 minutes, whatever
  `timeout_ms` says, and `persistent` does not exist there. A reset hours away
  therefore needs the waiter re-armed on every expiry notice — an expiry is a
  turn that must end with a new `Monitor`, never with an empty reply. On
  2026-09-16 the second expiry was answered with nothing and the session then
  ended: the window reset at ~18:00 and nobody resumed until the user came back.
- What actually survives an app restart is not a process but a file: the
  checkpoint. Resuming after a restart is the user's "продолжай" plus the
  RESUME section — the skill promises nothing else.
- The 7-day window does not reset overnight. The guard judges it separately and
  will say so when it is the blocker — in that case **do not arm the waiter**,
  it would sit there for a day. Check it before the user goes to bed, not after:
  starting an all-night run at 85 % weekly means it dies for good around 3 a.m.
  The weekly reset time now comes from the server too, so "when" is a fact rather
  than a guess — read it off `-mode status` instead of estimating.
