"""End-to-end smoke test: spawns server.py over stdio like Claude Code does and drives real Codex jobs.

    E:\\Development\\AI\\Claude\\Utils\\Bin\\Windows\\Python3\\python.exe tests\\smoke_test.py [--quick]

Uses an isolated state dir and a temp workspace; costs a few small Codex turns.
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


async def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="task-delegator-smoke-"))
    state = Path(tempfile.mkdtemp(prefix="task-delegator-state-"))
    (work / "data.txt").write_text("секретное слово: альбатрос\n", encoding="utf-8")
    env = {**os.environ, "TASK_DELEGATOR_STATE_DIR": str(state)}
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "server.py")], env=env)
    print(f"workspace={work}\nstate={state}")

    async with stdio_client(params) as (r, w), ClientSession(r, w) as s:
        await s.initialize()
        tools = {t.name for t in (await s.list_tools()).tools}
        expected = {"codex_start", "codex_reply", "codex_wait", "codex_status", "codex_cancel", "delegator_doctor"}
        check(expected <= tools, "all tools listed", tools)

        doc = await call(s, "delegator_doctor")
        check(bool(doc.get("codex", {}).get("exe")), "doctor finds codex.exe", doc)
        check("Logged in" in str(doc.get("codex", {}).get("login")), "doctor sees login", doc)
        print("   doctor:", json.dumps(doc, ensure_ascii=False))

        check(doc.get("codex_usage", {}).get("exists") is True, "doctor finds the usage script", doc)
        lim = await call(s, "codex_limits")
        print("   limits:", json.dumps(lim, ensure_ascii=False)[:300])
        check(lim.get("state") in ("ok", "warn", "pause") and lim.get("windows"), "codex_limits reports a verdict", lim)
        check(any(w["window"] == "5h" for w in lim.get("windows", [])), "codex_limits has the 5h window", lim)

        bad = await call(s, "codex_start", {"task": "x", "cwd": "relative\\path"})
        check("absolute path" in bad.get("__error__", ""), "relative cwd rejected with a readable reason", bad)
        unk = await call(s, "codex_status", {"job_id": "codex-nope"})
        check("unknown job id" in unk.get("__error__", ""), "unknown job id gives a readable error", unk)

        # 1) read-only question in Russian (UTF-8 through stdin)
        t0 = time.monotonic()
        st = await call(s, "codex_start", {
            "task": "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.",
            "cwd": str(work), "effort": "low",
        })
        check(st.get("status") == "running" and "job_id" in st, "start read-only job", st)
        check(st.get("limits", {}).get("state") in ("ok", "warn"), "start result carries the limits check", st)
        jid = st["job_id"]
        res = await call(s, "codex_wait", {"job_id": jid, "timeout_sec": 300})
        print("   limits_cost:", res.get("limits_cost"))
        check(isinstance(res.get("limits_cost", {}).get("cost"), dict), "finished job reports limits_cost", res)
        print(f"   read-only job: {res.get('status')} in {time.monotonic() - t0:.0f}s, "
              f"answer={res.get('final_message', '')[:120]!r}")
        check(res.get("status") == "completed", "read-only job completed", res)
        check("альбатрос" in res.get("final_message", "").lower(), "UTF-8 prompt/answer round-trip", res)
        check(bool(res.get("codex_session_id")), "session id captured", res)

        # 2) follow-up in the same session
        rep = await call(s, "codex_reply", {"job_id": jid, "message": "Повтори то же слово задом наперёд, только слово."})
        res2 = await call(s, "codex_wait", {"job_id": rep["job_id"], "timeout_sec": 300})
        print(f"   reply: {res2.get('status')} answer={res2.get('final_message', '')[:80]!r}")
        check(res2.get("status") == "completed", "reply completed", res2)
        check("сортабьла" in res2.get("final_message", "").lower(), "reply kept session context", res2)
        check(res2.get("continues_job") == jid, "reply linked to parent job", res2)

        # 3) read-only must not be able to write
        ro = await call(s, "codex_start", {
            "task": "Создай файл ro_probe.txt с текстом 1 любым способом. Если не получится, просто скажи BLOCKED.",
            "cwd": str(work), "effort": "low",
        })
        await call(s, "codex_wait", {"job_id": ro["job_id"], "timeout_sec": 300})
        check(not (work / "ro_probe.txt").exists(), "read-only sandbox blocks writes")

        if not QUICK:
            # 4) write mode creates a file and reports it
            wr = await call(s, "codex_start", {
                "task": "Создай в текущей папке файл hello.py с одной строкой: print('hi'). Больше ничего не делай.",
                "cwd": str(work), "mode": "write", "effort": "low",
            })
            resw = await call(s, "codex_wait", {"job_id": wr["job_id"], "timeout_sec": 300})
            print(f"   write job: {resw.get('status')} files={resw.get('files_changed')}")
            check(resw.get("status") == "completed", "write job completed", resw)
            check((work / "hello.py").exists(), "write mode created the file")

            # 4b) add_dirs survive codex_reply (resume has no --add-dir; we pass writable_roots).
            # The extra dir must be OUTSIDE %TEMP%: workspace-write allows temp dirs anyway.
            outside = ROOT / "tests" / ".tmp"
            outside.mkdir(exist_ok=True)
            extra = Path(tempfile.mkdtemp(prefix="extra-", dir=outside))
            blocked = Path(tempfile.mkdtemp(prefix="blocked-", dir=outside))
            neg = await call(s, "codex_start", {
                "task": f"Создай файл {blocked}\\x.txt с текстом 1. Если не получится, просто скажи BLOCKED.",
                "cwd": str(work), "mode": "write", "effort": "low",
            })
            await call(s, "codex_wait", {"job_id": neg["job_id"], "timeout_sec": 300})
            check(not (blocked / "x.txt").exists(), "write mode cannot write outside cwd without add_dirs")
            wa = await call(s, "codex_start", {
                "task": f"Создай файл {extra}\\first.txt с текстом 1. Больше ничего не делай.",
                "cwd": str(work), "mode": "write", "effort": "low", "add_dirs": [str(extra)],
            })
            await call(s, "codex_wait", {"job_id": wa["job_id"], "timeout_sec": 300})
            check((extra / "first.txt").exists(), "write into add_dir on start")
            wb = await call(s, "codex_reply", {
                "job_id": wa["job_id"],
                "message": f"Теперь создай файл {extra}\\second.txt с текстом 2. Больше ничего не делай.",
            })
            resb = await call(s, "codex_wait", {"job_id": wb["job_id"], "timeout_sec": 300})
            print(f"   add_dir reply: {resb.get('status')} files={resb.get('files_changed')} "
                  f"failed={resb.get('failed_file_changes')}")
            check((extra / "second.txt").exists(), "write into add_dir after codex_reply", resb)
            shutil.rmtree(outside, ignore_errors=True)

            # 5) cancel a running job
            lc = await call(s, "codex_start", {
                "task": "Подумай очень тщательно и напиши подробное эссе на 3000 слов о истории компиляторов.",
                "cwd": str(work), "effort": "low",
            })
            await asyncio.sleep(4)
            mid = await call(s, "codex_status", {"job_id": lc["job_id"]})
            check(mid.get("status") == "running", "status shows running", mid)
            cc = await call(s, "codex_cancel", {"job_id": lc["job_id"]})
            check(cc.get("status") == "cancelled", "cancel stops the job", cc)

        lst = await call(s, "codex_status", {})
        check(len(lst.get("jobs", [])) >= 3, "status lists recent jobs", lst)

    print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED: {failures}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
