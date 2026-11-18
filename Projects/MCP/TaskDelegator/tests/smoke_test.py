"""End-to-end smoke test: spawns server.py over stdio like Claude Code does and drives real jobs.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe tests\\smoke_test.py [--agent <id>] [--quick]

Uses an isolated state dir and a temp workspace. SPENDS REAL QUOTA of the agent under test:
  --agent codex (default)   about 9 small Codex jobs (the account's default model, effort low), ~3 min;
  --agent claude:<name>     about 5 small jobs on that Claude account (haiku, effort low, no Ultracode);
  --quick                   3 jobs: no write / add_dirs / cancel.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
QUICK = "--quick" in sys.argv
AGENT = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv[:-1] else "codex"
KIND = AGENT.partition(":")[0]
# The cheapest run that still proves the path.
CHEAP = ({"effort": "low"} if KIND == "codex"
         else {"model": "haiku", "effort": "low", "options": {"ultracode": False}})
TOOLS = {"list_agents", "task_start", "task_reply", "task_wait", "task_status", "task_cancel", "delegator_doctor"}

failures: list[str] = []


def check(cond: bool, label: str, extra: object = "") -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {label}" + (f"  -> {extra}" if (extra and not cond) else ""))
    if not cond:
        failures.append(label)


async def call(s: ClientSession, name: str, args: dict | None = None) -> dict:
    r = await s.call_tool(name, args or {}, allow_input_required=False)
    if r.is_error:
        return {"__error__": " ".join(getattr(c, "text", "") for c in r.content)}
    if r.structured_content is not None:
        sc = r.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(r.content[0].text)


def start_args(task: str, cwd: Path, **extra) -> dict:
    args = {"agent": AGENT, "task": task, "cwd": str(cwd), **CHEAP, **extra}
    if "options" in extra and "options" in CHEAP:
        args["options"] = {**CHEAP["options"], **extra["options"]}
    return args


async def common(s: ClientSession) -> bool:
    """Tools, doctor, the agent card. False = the agent cannot run, stop before spending quota."""
    tools = {t.name for t in (await s.list_tools()).tools}
    check(TOOLS <= tools, "all tools listed", tools)
    doc = await call(s, "delegator_doctor")
    agent_doc = (doc.get("agents") or {}).get(AGENT)
    print("   doctor:", json.dumps(agent_doc, ensure_ascii=False)[:400])
    check(bool((doc.get("kinds") or {}).get(KIND, {}).get("exe")), f"doctor finds the {KIND} executable", doc)
    check(agent_doc is not None, f"doctor lists agent {AGENT}", list((doc.get("agents") or {})))
    if KIND == "codex":
        check("logged in" in str((agent_doc or {}).get("login")).lower(), "doctor sees the Codex login", agent_doc)
        check((doc.get("codex_usage") or {}).get("exists") is True, "doctor finds the Codex usage script", doc)
    else:
        check(((agent_doc or {}).get("auth") or {}).get("loggedIn") is True, "the Claude account is logged in", agent_doc)
    lst = await call(s, "list_agents")
    card = next((a for a in lst.get("agents", []) if a["id"] == AGENT), None)
    print("   card:", json.dumps(card, ensure_ascii=False)[:400])
    check(card is not None and card.get("available") is True, f"list_agents shows {AGENT} as available", lst)
    if KIND == "codex":
        lim = (card or {}).get("limits") or {}
        check(lim.get("state") in ("ok", "warn", "pause") and "5h" in str(lim.get("summary")),
              "list_agents reports Codex limits", lim)

    bad = await call(s, "task_start", {"agent": AGENT, "task": "x", "cwd": "relative\\path"})
    check("absolute path" in bad.get("__error__", ""), "relative cwd rejected with a readable reason", bad)
    unk = await call(s, "task_status", {"job_id": "codex-nope"})
    check("unknown job id" in unk.get("__error__", ""), "unknown job id gives a readable error", unk)
    noagent = await call(s, "task_start", {"agent": "nobody:here", "task": "x", "cwd": str(ROOT)})
    check("unknown agent" in noagent.get("__error__", ""), "unknown agent gives a readable error", noagent)
    return not failures


async def suite(s: ClientSession, work: Path) -> None:
    # 1) read-only question in Russian (UTF-8 through stdin)
    t0 = time.monotonic()
    st = await call(s, "task_start", start_args(
        "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.", work))
    check(st.get("status") == "running" and "job_id" in st, "start a read-only job", st)
    if "job_id" not in st:
        return
    jid = st["job_id"]
    res = await call(s, "task_wait", {"job_id": jid, "timeout_sec": 300})
    print(f"   read-only job: {res.get('status')} in {time.monotonic() - t0:.0f}s, "
          f"answer={res.get('final_message', '')[:120]!r}")
    rl = res.get("rate_limits") or {}
    if rl.get("status") == "rejected":
        check(False, f"{AGENT} is rate-limited ({rl.get('rateLimitType')}), nothing else can be tested; "
                     f"resets {time.strftime('%Y-%m-%d %H:%M', time.localtime(rl.get('resetsAt') or 0))}")
        return
    check(res.get("status") == "completed", "read-only job completed", res)
    check(res.get("agent") == AGENT, "the result names its agent", res.get("agent"))
    check("альбатрос" in res.get("final_message", "").lower(), "UTF-8 prompt/answer round-trip", res)
    check(bool(res.get("session_id")), "session id captured", res)
    if KIND == "codex":
        print("   limits:", st.get("limits"), "cost:", res.get("limits_cost"))
        check(st.get("limits", {}).get("state") in ("ok", "warn"), "start result carries the limits check", st)
        check(isinstance(res.get("limits_cost", {}).get("cost"), dict), "finished job reports limits_cost", res)
    else:
        check(isinstance(res.get("rate_limits"), dict), "the account's rate limits come with the result", res)

    # 2) follow-up in the same session
    # The message names neither the file nor the word: only the session's context knows it.
    # (Upper case rather than reversal: small models garble reversed Cyrillic.)
    rep = await call(s, "task_reply", {"job_id": jid, "message": "Какое слово ты нашёл в прошлый раз? "
                                                                "Напиши его заглавными буквами, только слово."})
    res2 = await call(s, "task_wait", {"job_id": rep.get("job_id", "?"), "timeout_sec": 300})
    print(f"   reply: {res2.get('status')} answer={res2.get('final_message', '')[:80]!r}")
    check(res2.get("status") == "completed", "reply completed", res2)
    check("АЛЬБАТРОС" in res2.get("final_message", ""), "reply kept session context", res2)
    check(res2.get("continues_job") == jid, "reply linked to parent job", res2)

    # 3) read-only must not be able to write
    ro = await call(s, "task_start", start_args(
        "Создай файл ro_probe.txt с текстом 1 любым способом. Если не получится, просто скажи BLOCKED.", work))
    await call(s, "task_wait", {"job_id": ro.get("job_id", "?"), "timeout_sec": 300})
    check(not (work / "ro_probe.txt").exists(), "read-only mode blocks writes")

    if QUICK:
        return
    # 4) write mode creates a file (a claude agent without a shell: edits only)
    extra = {"options": {"shell": False}} if KIND == "claude" else {}
    wr = await call(s, "task_start", start_args(
        "Создай в текущей папке файл hello.py с одной строкой: print('hi'). Больше ничего не делай.", work,
        mode="write", **extra))
    resw = await call(s, "task_wait", {"job_id": wr.get("job_id", "?"), "timeout_sec": 300})
    print(f"   write job: {resw.get('status')} files={resw.get('files_changed') or resw.get('files_edited')}")
    check(resw.get("status") == "completed" and (work / "hello.py").exists(), "write mode created the file", resw)

    if KIND == "codex":
        # 4b) add_dirs survive task_reply (resume has no --add-dir; we pass writable_roots).
        # The extra dir must be OUTSIDE %TEMP%: workspace-write allows temp dirs anyway.
        outside = ROOT / "tests" / ".tmp"
        outside.mkdir(exist_ok=True)
        extra_dir = Path(tempfile.mkdtemp(prefix="extra-", dir=outside))
        blocked = Path(tempfile.mkdtemp(prefix="blocked-", dir=outside))
        neg = await call(s, "task_start", start_args(
            f"Создай файл {blocked}\\x.txt с текстом 1. Если не получится, просто скажи BLOCKED.", work, mode="write"))
        await call(s, "task_wait", {"job_id": neg.get("job_id", "?"), "timeout_sec": 300})
        check(not (blocked / "x.txt").exists(), "write mode cannot write outside cwd without add_dirs")
        wa = await call(s, "task_start", start_args(
            f"Создай файл {extra_dir}\\first.txt с текстом 1. Больше ничего не делай.", work,
            mode="write", add_dirs=[str(extra_dir)]))
        await call(s, "task_wait", {"job_id": wa.get("job_id", "?"), "timeout_sec": 300})
        check((extra_dir / "first.txt").exists(), "write into add_dir on start")
        wb = await call(s, "task_reply", {
            "job_id": wa.get("job_id", "?"),
            "message": f"Теперь создай файл {extra_dir}\\second.txt с текстом 2. Больше ничего не делай.",
        })
        resb = await call(s, "task_wait", {"job_id": wb.get("job_id", "?"), "timeout_sec": 300})
        print(f"   add_dir reply: {resb.get('status')} files={resb.get('files_changed')} "
              f"failed={resb.get('failed_file_changes')}")
        check((extra_dir / "second.txt").exists(), "write into add_dir after task_reply", resb)
        shutil.rmtree(outside, ignore_errors=True)

    # 5) cancel a running job
    lc = await call(s, "task_start", start_args(
        "Подумай очень тщательно и напиши подробное эссе на 3000 слов об истории компиляторов.", work))
    await asyncio.sleep(4)
    mid = await call(s, "task_status", {"job_id": lc.get("job_id", "?")})
    check(mid.get("status") == "running", "status shows running", mid)
    cc = await call(s, "task_cancel", {"job_id": lc.get("job_id", "?")})
    check(cc.get("status") == "cancelled", "cancel stops the job", cc)

    lst = await call(s, "task_status", {"agent": AGENT})
    check(len(lst.get("jobs", [])) >= 3 and all(j.get("agent") == AGENT for j in lst["jobs"]),
          "task_status lists this agent's recent jobs", lst)


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="task-delegator-smoke-"))
    state = Path(tempfile.mkdtemp(prefix="task-delegator-state-"))
    (work / "data.txt").write_text("секретное слово: альбатрос\n", encoding="utf-8")
    # The isolated state dir hides the real agents.json; point at it explicitly so the agents keep
    # their real settings (and homes outside the default folders).
    real_agents = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "TaskDelegator" / "agents.json"
    env = {**os.environ, "TASK_DELEGATOR_STATE_DIR": str(state)}
    if "TASK_DELEGATOR_AGENTS_FILE" not in env and real_agents.exists():
        env["TASK_DELEGATOR_AGENTS_FILE"] = str(real_agents)
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env)
    print(f"agent={AGENT}\nworkspace={work}\nstate={state}")

    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        if await common(s):  # no point spending quota on an agent that cannot start
            await suite(s, work)
    return report()


def report() -> int:
    print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
