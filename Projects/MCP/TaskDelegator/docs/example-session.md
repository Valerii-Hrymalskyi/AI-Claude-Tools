# Пример сессии: от задачи до результата

Настоящий прогон 2026-11-18 (первые две задачи `tests\smoke_test.py --quick`, агент `codex`).
Ответы сервера приведены как есть, кроме двух сокращений: имя пользователя Windows заменено на
`<user>`, а поле `files` (пути к файлам задачи) опущено. Смоук-тест держит историю задач во
временной папке; при обычной работе она в `%LOCALAPPDATA%\TaskDelegator`.

В рабочей папке лежит `data.txt` со строкой `секретное слово: альбатрос`.

## 0. `list_agents` — кому поручить

Ответ (сокращён до одного агента):

```json
{
  "id": "codex", "kind": "codex", "account": null, "available": true,
  "running_jobs": 0, "max_jobs": 3,
  "defaults": {"model": "(the account's own default)", "effort": "(the account's own default)",
               "options": {"web_search": false}},
  "options": {"web_search": "let Codex search the live web (docs, release notes, error messages); works in read-only mode too. Its shell has no network either way."},
  "limits": {"state": "ok", "summary": "5h: 44% (reset 19.11 00:04) · 7d: 43% (reset 23.11 19:13)"}
}
```

## 1. `task_start` — поручить

Claude вызывает:

```json
{
  "agent": "codex",
  "task": "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-eckw8k5c",
  "effort": "low"
}
```

`mode` по умолчанию `read-only`. Смоук-тест не передаёт `model`, а в `agents.json` её нет, поэтому
Codex взял модель из своего конфига; в обычной работе Claude передаёт и `model`, и `effort` (см.
[пример правил](claude-md-example.md)).

Сервер проверяет лимиты агента, запускает `codex exec` в фоне и сразу отвечает:

```json
{
  "job_id": "codex-20261118-230914-d5a0",
  "agent": "codex",
  "status": "running",
  "mode": "read-only",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-eckw8k5c",
  "model": null,
  "effort": "low",
  "options": {"web_search": false},
  "limits": {"state": "ok", "summary": "5h: 44% (reset 19.11 00:04) · 7d: 43% (reset 23.11 19:13)"},
  "hint": "call task_wait(job_id) when you are ready for the result"
}
```

(Этот ответ собран по коду из значений, сохранённых в `job.json` задачи, и из строки лимитов,
которую напечатал смоук-тест; целиком он в лог не пишется. Остальные ответы ниже — дословно.)

Пока задача идёт, Claude может делать своё. При `warn` в `limits` будет `note`, при `pause`
сервер откажет и назовёт время сброса.

## 2. `task_wait` — дождаться

```json
{"job_id": "codex-20261118-230914-d5a0", "timeout_sec": 300}
```

Ответ через 23 секунды:

```json
{
  "job_id": "codex-20261118-230914-d5a0",
  "agent": "codex",
  "status": "completed",
  "mode": "read-only",
  "cwd": "C:\\Users\\<user>\\AppData\\Local\\Temp\\task-delegator-smoke-eckw8k5c",
  "elapsed_sec": 23,
  "task": "Прочитай data.txt и ответь одним словом: какое там секретное слово? Ничего не меняй.",
  "session_id": "01a1f07e-0be3-7b50-83ec-f5ffffff8b2d",
  "activity": {
    "events": 11,
    "items": {"agent_message": 2, "command_execution": 3},
    "commands_run": 3,
    "last_commands": [
      {"command": "Get-Content -Raw -LiteralPath .\\data.txt", "status": "completed", "exit_code": 0},
      {"command": "[Convert]::ToBase64String([IO.File]::ReadAllBytes((Resolve-Path -LiteralPath .\\data.txt)))",
       "status": "failed", "exit_code": 1},
      {"command": "\"Get-Content -Encoding Byte -LiteralPath .\\data.txt | ForEach-Object { '{0:X2}' -f \"'$_ }'",
       "status": "completed", "exit_code": 0}
    ]
  },
  "usage": {
    "input_tokens": 67242,
    "cached_input_tokens": 60416,
    "cache_write_input_tokens": 0,
    "output_tokens": 463,
    "reasoning_output_tokens": 148
  },
  "final_message": "альбатрос",
  "limits_cost": {
    "cost": {"5h": "+0%", "7d": "+0%"},
    "after": "5h: 44% (reset 19.11 00:04) · 7d: 43% (reset 23.11 19:13)",
    "note": "approximate: includes any other Codex use while the job ran"
  }
}
```

Если задача не успела за `timeout_sec`, придёт тот же вид со `status: "running"`, текущей
активностью и `note`; `task_wait` можно вызвать снова.

## 3. Разбор ответа

Что Claude смотрит, прежде чем сообщить результат пользователю:

- `status`: `completed`, а не `failed` / `lost` / `cancelled` (у неудачных есть `stderr_tail`);
- `final_message` — это **утверждение** агента. Здесь его легко проверить: прочитать `data.txt`.
  В задаче на запись — посмотреть `files_changed`, `failed_file_changes`, `git_status_after`,
  сам diff и прогнать тесты;
- `activity.last_commands` — что агент делал. Здесь видно, что Codex не сразу прочитал файл в
  нужной кодировке, но разобрался;
- `limits_cost` — во что обошлась задача (здесь меньше процента окна), чтобы прикидывать похожие задачи.

## 4. `task_reply` — продолжить ту же сессию

```json
{"job_id": "codex-20261118-230914-d5a0", "message": "Повтори то же слово задом наперёд, только слово."}
```

Это новая фоновая задача в той же сессии того же агента (у Codex — `codex exec resume`), с тем же
`mode`, `cwd`, `add_dirs` и опциями. После `task_wait`:

```json
{
  "job_id": "codex-20261118-230939-97a2",
  "agent": "codex",
  "status": "completed",
  "mode": "read-only",
  "elapsed_sec": 6,
  "task": "Повтори то же слово задом наперёд, только слово.",
  "continues_job": "codex-20261118-230914-d5a0",
  "session_id": "01a1f07e-0be3-7b50-83ec-f5ffffff8b2d",
  "activity": {"events": 4, "items": {"agent_message": 1}, "commands_run": 0, "last_commands": []},
  "usage": {"input_tokens": 84532, "cached_input_tokens": 76544, "cache_write_input_tokens": 0,
            "output_tokens": 520, "reasoning_output_tokens": 194},
  "final_message": "сортабьла",
  "limits_cost": {
    "cost": {"5h": "+0%", "7d": "+0%"},
    "after": "5h: 44% (reset 19.11 00:04) · 7d: 43% (reset 23.11 19:13)",
    "note": "approximate: includes any other Codex use while the job ran"
  }
}
```

(Поле `cwd` опущено — оно как в шаге 2.) Тот же `session_id` и `continues_job` показывают, что
контекст сохранился: Codex помнит слово, не перечитывая файл.

## Другой Claude

С агентом `claude:<имя>` всё так же: `task_start(agent="claude:<имя>", …)` → `task_wait` →
`task_reply`. Отличия в ответе: `files_edited` вместо `files_changed`, `permission_denials` (что
агент пытался сделать и не смог), `rate_limits` — лимиты аккаунта, которые приходят бесплатно с
каждым результатом, и `result` с числом ходов и условной стоимостью по прайсу API.
