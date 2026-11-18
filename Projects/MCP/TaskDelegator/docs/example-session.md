# Пример сессии: от задачи до результата

Настоящий прогон 2026-11-18 (первые две задачи `tests\smoke_test.py --quick`). Ответы сервера
приведены как есть, кроме двух сокращений: имя пользователя Windows заменено на `<user>`, а
повторяющиеся пути к файлам задачи свёрнуты в `…`. Смоук-тест держит историю задач во временной
папке; при обычной работе она в `%LOCALAPPDATA%\TaskDelegator`.

В рабочей папке лежит `data.txt` со строкой `секретное слово: альбатрос`.

## 1. `codex_start` — поручить

Claude вызывает:

```json
{
  "task": "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-10sixdx0",
  "effort": "low"
}
```

`mode` по умолчанию `read-only`. Смоук-тест не передаёт `model`, поэтому Codex взял модель из
своего конфига; в обычной работе Claude передаёт и `model`, и `effort` (см.
[пример правил](claude-md-example.md)).

Сервер проверяет лимиты Codex, запускает `codex exec` в фоне и сразу отвечает:

```json
{
  "job_id": "codex-20261118-220215-a67f",
  "status": "running",
  "mode": "read-only",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-10sixdx0",
  "limits": {"state": "ok", "summary": "5h: 29% (reset 19.11 00:04) · 7d: 41% (reset 23.11 19:13)"},
  "hint": "call codex_wait(job_id) when you are ready for the result"
}
```

(Этот ответ восстановлен по коду и по замеру лимитов, сохранённому в `job.json` задачи: сам он
в логе смоук-теста не печатается. Остальные ответы ниже — дословно.)

Пока задача идёт, Claude может делать своё. При `warn` в `limits` будет `note`, при `pause`
сервер откажет и назовёт время сброса.

## 2. `codex_wait` — дождаться

```json
{"job_id": "codex-20261118-220215-a67f", "timeout_sec": 300}
```

Ответ через 22 секунды:

```json
{
  "job_id": "codex-20261118-220215-a67f",
  "status": "completed",
  "mode": "read-only",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-10sixdx0",
  "elapsed_sec": 22,
  "task": "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.",
  "codex_session_id": "01a1f040-b80f-7381-9867-684f0da3e87b",
  "activity": {
    "events": 11,
    "items": {"agent_message": 2, "command_execution": 3},
    "commands_run": 3,
    "last_commands": [
      {"command": "'Get-Content -LiteralPath data.txt'", "status": "completed", "exit_code": 0},
      {"command": "'[System.Text.Encoding]::GetEncoding(1251).GetString([System.IO.File]::ReadAllBytes((Resolve-Path -LiteralPath data.txt)))'",
       "status": "failed", "exit_code": 1},
      {"command": "'Format-Hex -LiteralPath data.txt'", "status": "completed", "exit_code": 0}
    ]
  },
  "usage": {
    "input_tokens": 67134,
    "cached_input_tokens": 60416,
    "cache_write_input_tokens": 0,
    "output_tokens": 367,
    "reasoning_output_tokens": 74
  },
  "final_message": "альбатрос",
  "limits_cost": {
    "cost": {"5h": "+1%", "7d": "+0%"},
    "after": "5h: 30% (reset 19.11 00:04) · 7d: 41% (reset 23.11 19:13)",
    "note": "approximate: includes any other Codex use while the job ran"
  },
  "files": {
    "events": "…\\jobs\\codex-20261118-220215-a67f\\stdout.log",
    "final_message": "…\\jobs\\codex-20261118-220215-a67f\\last_message.txt",
    "stderr": "…\\jobs\\codex-20261118-220215-a67f\\stderr.log"
  }
}
```

Если задача не успела за `timeout_sec`, придёт тот же вид со `status: "running"`, текущей
активностью и `note`; `codex_wait` можно вызвать снова.

## 3. Разбор ответа

Что Claude смотрит, прежде чем сообщить результат пользователю:

- `status`: `completed`, а не `failed` / `lost` / `cancelled` (у неудачных есть `stderr_tail`);
- `final_message` — это **утверждение** Codex. Здесь его легко проверить: прочитать `data.txt`.
  В задаче на запись — посмотреть `files_changed`, `failed_file_changes`, `git_status_after`,
  сам diff и прогнать тесты;
- `activity.last_commands` — что Codex делал. Здесь видно, что он не сразу прочитал файл в
  нужной кодировке, но разобрался;
- `limits_cost` — во что обошлась задача (+1 % окна 5h), чтобы прикидывать похожие задачи.

## 4. `codex_reply` — продолжить ту же сессию

```json
{"job_id": "codex-20261118-220215-a67f", "message": "Повтори то же слово задом наперёд, только слово."}
```

Это новая фоновая задача в той же сессии Codex (`codex exec resume`), с тем же `mode`, `cwd` и
`add_dirs`. После `codex_wait`:

```json
{
  "job_id": "codex-20261118-220240-f1bc",
  "status": "completed",
  "mode": "read-only",
  "elapsed_sec": 6,
  "task": "Повтори то же слово задом наперёд, только слово.",
  "continues_job": "codex-20261118-220215-a67f",
  "codex_session_id": "01a1f040-b80f-7381-9867-684f0da3e87b",
  "activity": {"events": 4, "items": {"agent_message": 1}, "commands_run": 0, "last_commands": []},
  "usage": {"input_tokens": 84410, "cached_input_tokens": 76544, "cache_write_input_tokens": 0,
            "output_tokens": 426, "reasoning_output_tokens": 122},
  "final_message": "сортабьла",
  "limits_cost": {
    "cost": {"5h": "+0%", "7d": "+0%"},
    "after": "5h: 30% (reset 19.11 00:04) · 7d: 41% (reset 23.11 19:13)",
    "note": "approximate: includes any other Codex use while the job ran"
  }
}
```

(Поля `cwd` и `files` опущены — они как в шаге 2.) Тот же `codex_session_id` и `continues_job`
показывают, что контекст сохранился: Codex помнит слово, не перечитывая файл.

## Worker B

Инструменты `worker_*` работают так же: `worker_start` → `worker_wait` → `worker_reply`. Отличия
в ответе: `session_id` вместо `codex_session_id`, `files_edited` вместо `files_changed`,
`permission_denials` (что B пытался сделать и не смог) и `rate_limits` — лимиты второго
аккаунта, которые приходят бесплатно с каждым результатом.
