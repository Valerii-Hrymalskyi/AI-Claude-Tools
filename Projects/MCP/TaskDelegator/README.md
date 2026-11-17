# TaskDelegator

MCP-сервер, через который Claude поручает задачи другим агентам (Codex, второй аккаунт Claude).
До 2026-11-18 проект назывался AITools, а сервер — `aitools`. Теперь: сервер `task-delegator`,
инструменты `mcp__task-delegator__*`, пакет `task_delegator`, переменные `TASK_DELEGATOR_*`,
история задач — `%LOCALAPPDATA%\TaskDelegator`.

Локальный MCP-сервер `task-delegator`. С ним Claude может поручать работу другим ИИ-агентам
на этом ПК. Сейчас подключён один агент — **OpenAI Codex** (через `codex exec`).

## Как это работает

```
Claude Code ──MCP/stdio──► server.py ──► codex.exe exec --json  (фоновый процесс)
                                │
                                └─► %LOCALAPPDATA%\TaskDelegator\jobs\<job_id>\   (всё по задаче)
```

Каждая задача — это «джоба» в отдельной папке:

| Файл | Что в нём |
|---|---|
| `job.json` | параметры запуска, PID, флаг отмены |
| `prompt.txt` | точный текст задания (передаётся в Codex через stdin) |
| `stdout.log` | поток событий Codex (JSONL): команды, правки файлов, сообщения |
| `last_message.txt` | финальный ответ Codex |
| `stderr.log`, `exit.json` | диагностика и код выхода |

Статус задачи вычисляется из этих файлов. Поэтому джоба переживает перезапуск сервера:
Codex продолжает работать, а `codex_status` после рестарта по-прежнему её видит.
Процесс опознаётся по паре «PID + время создания». Если Windows отдаст тот же PID
другому процессу, сервер не примет его за свою задачу и не убьёт его через `codex_cancel`.
Задачу, завершившуюся без наблюдателя, сервер один раз фиксирует в `exit.json`
(`"unobserved": true`) и дальше её не перепроверяет.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `codex_start(task, cwd, mode, model?, effort?, add_dirs?, ignore_limits?)` | проверяет лимиты, запускает задачу в фоне и сразу возвращает `job_id` |
| `codex_wait(job_id, timeout_sec)` | ждёт до N секунд (максимум 600); по готовности возвращает ответ, изменённые файлы, расход токенов и `limits_cost` |
| `codex_status(job_id?)` | без ожидания: прогресс задачи или список последних задач |
| `codex_reply(job_id, message, mode?, ignore_limits?)` | продолжает ту же сессию Codex с полным контекстом (`codex exec resume`); `add_dirs` исходной задачи сохраняются |
| `codex_cancel(job_id)` | останавливает задачу (убивает дерево процессов) |
| `codex_limits()` | текущий расход окон Codex (5 часов и неделя) и вердикт `ok` / `warn` / `pause`; бесплатно |
| `delegator_doctor()` | где найден `codex.exe`, его версия, вход в аккаунт, скрипт лимитов, пути |

Режимы `mode`:
- `read-only` (по умолчанию) — Codex читает файлы и запускает команды, но не пишет;
- `write` — может править файлы внутри `cwd` и `add_dirs` (песочница `workspace-write`, без сети).

В `files_changed` попадают только успешно применённые правки, неудавшиеся идут отдельно в
`failed_file_changes`. Правки через shell-команды (например, `sed`) туда не попадают; для
git-папок в write-режиме есть ещё `git_status_after`.

Для запуска всегда ставится `approval_policy="never"`: в фоне некому отвечать на запросы
подтверждения, поэтому отказ сразу возвращается самому Codex. Переменные окружения
`CLAUDE*`, `ANTHROPIC*` и `MCP_*` в Codex не передаются.

## Лимиты Codex

У Codex два окна лимитов: пятичасовое (`5h`, в данных `primary`) и недельное (`7d`, `secondary`).
Их читает общий с Codex скрипт
`E:\Development\AI\Codex\Utils\Scritps\GetUsage\codex_usage.py`. Сервер вызывает его
подпроцессом с `--json` и опирается только на контракт `schema: 1`, описанный в
[docs/codex-usage-requirements.md](docs/codex-usage-requirements.md). Чтение бесплатное:
квота не тратится.

| Состояние | Когда | Что делают `codex_start` / `codex_reply` |
|---|---|---|
| `ok` | ниже порогов | запускают задачу |
| `warn` | 5h ≥ 90 % или 7d ≥ 95 % | запускают, но в ответе предупреждение |
| `pause` | 5h ≥ 95 %, 7d ≥ 98 %, `limit reached` или Codex запрещает обычное использование | отказывают и называют время сброса; обойти можно только `ignore_limits=true` |
| `unknown` | скрипт недоступен или вернул ошибку | запускают и честно пишут, что лимиты не проверены |

Сервер сохраняет замер лимитов перед каждой задачей, а второй замер снимает в момент, когда
Codex завершился, и кладёт его в `limits_after.json`. В результате задачи (`codex_wait`,
`codex_status`) появляется `limits_cost`, например `{"5h": "+3%", "7d": "+1%"}`. Это прикидка:
туда входит и любое другое использование Codex за то же время. Если окно успело сброситься,
вместо числа будет `window reset during the job`. Если сервер в момент окончания не работал, а
на задачу посмотрели больше чем через 5 минут после её завершения, стоимость будет `unknown`:
замер, снятый так поздно, учёл бы и чужую работу.

## Worker B: второй аккаунт Claude Code

Инструменты `worker_start`, `worker_wait`, `worker_status`, `worker_reply`, `worker_cancel` запускают
`claude -p --output-format stream-json` на **втором аккаунте Claude** так же, как `codex_*` запускают
Codex. Второй аккаунт отделён папкой конфига `CLAUDE_CONFIG_DIR` (по умолчанию
`%USERPROFILE%\.claude-worker-b`): там его вход, настройки, история и свой `CLAUDE.md`. `task-delegator` в
нём не зарегистрирован, так что B не может поручать задачи дальше. Переменные окружения ведущей
сессии (`CLAUDE*`, `ANTHROPIC*`) к B не передаются.

| Режим | Права B |
|---|---|
| `read-only` | `--permission-mode dontAsk`; разрешены Read, Grep, Glob, WebSearch, WebFetch; правки и shell запрещены |
| `write` | `--permission-mode acceptEdits`; правки внутри `cwd`/`add_dirs` и shell (с сетью) |

B работает под твоим пользователем Windows, с теми же правами, что и ведущий Claude.

**Умолчания B:** `claude-opus-5-5`, effort `xhigh`; в задачах на запись — Ultracode
(`ultracode=true`), в задачах на чтение — никогда (субагенты workflow могли бы получить право на
правки). Как это включается в `claude -p` (исследовал сам B, строки проверены по бинарнику 2.1.325):
- ключевое слово «ultracode» в промпте `-p` **не работает**: оно срабатывает только на промпты,
  набранные человеком;
- на плане Pro workflows по умолчанию выключены; их включает `"enableWorkflows": true`, а запуск
  инструмента без вопросов — `"permissions": {"allow": ["Workflow"]}`. Обе настройки лежат в
  [task_delegator/worker_settings.json](task_delegator/worker_settings.json) и передаются в каждом запуске через
  `--settings`; собственный `settings.json` второго аккаунта не меняется;
- сам Ultracode включает `--effort ultracode` (подразумевает `xhigh`);
- `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS=3` ограничивает число одновременных агентов workflow
  (переопределяется `TASK_DELEGATOR_WORKER_WORKFLOW_AGENTS`).

Проверка: при Ultracode в событии `system/init` есть инструмент `Workflow`, в результате это поле
`workflow_available: true`. Переопределить умолчания можно переменными `TASK_DELEGATOR_WORKER_MODEL`,
`TASK_DELEGATOR_WORKER_EFFORT`, `TASK_DELEGATOR_WORKER_ULTRACODE=0`.

Лимиты второго аккаунта приходят бесплатно в каждом результате: `rate_limits.unifiedWindows`
(`five_hour`, `seven_day`, доля использования от 0 до 1 и время сброса). Это событие
`rate_limit_event` из потока `stream-json`. `result.cost_usd_list_price` — условная стоимость по
прайсу API, для подписки она справочная.

**Разовый вход во второй аккаунт** (в своём окне PowerShell, не через Claude):

```powershell
powershell -ExecutionPolicy Bypass -File E:\Development\AI\Claude\Projects\MCP\TaskDelegator\scripts\worker_login.ps1
```

Откроется Claude Code с отдельной папкой конфига: `/login`, вход вторым аккаунтом, `/exit`.
Проверка: `delegator_doctor` → `worker_b.auth.loggedIn: true`.

Отдельный пользователь Windows `claude-worker` с задачей Планировщика (обвязка в
`E:\Development\AI\Codex\Utils\Scritps\ClaudeWorker`) для этого не нужен.

## Модель и effort

Сервер проверяет `model` и `effort` по кэшу моделей самого Codex (`~/.codex/models_cache.json`,
или `%CODEX_HOME%`). Если модель неизвестна или не поддерживает такой effort, задача не стартует,
а в ошибке перечислено, что доступно. Если кэша нет, проверка пропускается. Полный список моделей
с описаниями и уровнями показывает `delegator_doctor`.

Какую модель и какой effort выбирать, решает Claude по правилам из `~/.claude/CLAUDE.md`
(раздел «Делегирование задач Codex»); краткая версия зашита в инструкции сервера. Если `model`
и `effort` не переданы, Codex возьмёт их из своего конфига пользователя.

## Отложенный запуск: `scripts/codex_job.py`

Ставит задачу через то же ядро, что и сервер (она видна в `codex_status`), но из командной
строки. Удобно, когда MCP-инструменты не загружены или задачу нужно запустить к определённому
времени:

```powershell
& "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" scripts\codex_job.py --task-file task.md `
  --cwd E:\Some\Project --mode write --effort medium --start-at 05:25 --wait-min 90
```

Перед стартом он проверяет лимиты. Если окно за порогом паузы, он ждёт его сброса (плюс
запас 120 с) и проверяет снова, а не падает. Пока ждёт и работает, не даёт Windows уснуть по
бездействию; от закрытой крышки или ручного «Сна» это не спасает. Остальные опции:
`--pause-5h` / `--pause-7d` (свои пороги), `--no-limits`, `--limits-script`, `--dry-run`.
Коды выхода: 0 — задача выполнена, 1 — ошибка, 3 — задача ещё идёт, а `--wait-min` истёк.

## Состав

```
server.py                    MCP-сервер (MCP Python SDK 2.x, MCPServer)
task_delegator/jobs.py              джобы на диске, запуск и отслеживание процессов (не зависит от агента)
task_delegator/codex.py             провайдер Codex: поиск exe, аргументы, разбор событий, limits_cost
task_delegator/limits.py            лимиты Codex: вызов скрипта usage, пороги, стоимость задачи
task_delegator/procs.py             Windows: проверка «жив ли PID», kill дерева процессов
scripts/codex_job.py         запуск задачи из командной строки, в том числе к заданному времени
docs/                        требования к скрипту лимитов Codex
tests/unit_test.py           офлайн-тесты: события, личность процесса, TOML, поиск exe, лимиты
tests/smoke_test.py          сквозной тест через настоящий MCP-клиент и настоящий Codex
```

## Установка с нуля

Python общий для всех инструментов Claude и лежит вне проекта: `E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe`
(портативный 3.15.2, NuGet-пакет `python`, с зависимостями). Если его нет:

1. Скачать `https://api.nuget.org/v3-flatcontainer/python/<версия>/python.<версия>.nupkg`,
   распаковать как zip (`Expand-Archive`) и скопировать содержимое `tools\` в `E:\Development\AI\Claude\Utils\Bin\Windows\Python3`.
2. `& "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" -m pip install -r requirements.txt`

Регистрация в Claude Code (для всех проектов):

```powershell
& "<claude.exe>" mcp add --scope user task-delegator -- `
  "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" `
  "E:\Development\AI\Claude\Projects\MCP\TaskDelegator\server.py"
```

`claude.exe` настольного приложения лежит в
`%LOCALAPPDATA%\Claude-Data\claude_*\claude-code\<версия>\<хеш>\claude.exe`.
Удалить регистрацию: `claude mcp remove task-delegator -s user`.

Новые инструменты появляются в сессиях Claude, **начатых после** регистрации.

## Проверка

```powershell
& "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" tests\unit_test.py           # офлайн, без Codex, секунды
& "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" tests\smoke_test.py          # сквозной (~3 мин, ~9 маленьких задач Codex)
& "E:\Development\AI\Claude\Utils\Bin\Windows\Python3\python.exe" tests\smoke_test.py --quick  # без write/add_dirs/cancel
```

## Настройки (переменные окружения сервера)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `TASK_DELEGATOR_CODEX_EXE` | ищется сам | явный путь к `codex.exe` |
| `TASK_DELEGATOR_STATE_DIR` | `%LOCALAPPDATA%\TaskDelegator` | где хранить джобы |
| `TASK_DELEGATOR_CODEX_MAX_JOBS` | `3` | сколько задач Codex может идти одновременно |
| `TASK_DELEGATOR_KEEP_DAYS` | `14` | через сколько дней удалять завершённые джобы |
| `TASK_DELEGATOR_CODEX_USAGE_SCRIPT` | `E:\Development\AI\Codex\Utils\Scritps\GetUsage\codex_usage.py` | скрипт лимитов Codex |
| `TASK_DELEGATOR_CODEX_WARN_5H` / `TASK_DELEGATOR_CODEX_PAUSE_5H` | `90` / `95` | пороги пятичасового окна, % |
| `TASK_DELEGATOR_CODEX_WARN_7D` / `TASK_DELEGATOR_CODEX_PAUSE_7D` | `95` / `98` | пороги недельного окна, % |

`codex.exe` ищется в `%LOCALAPPDATA%\OpenAI\Codex\bin\*\` (берётся самый свежий),
затем в `PATH`. Папка с хешем меняется при каждом обновлении Codex, поэтому путь
не зашит в код.

## Как добавить другого агента

1. Сделать `task_delegator/<agent>.py` по образцу `codex.py`: поиск exe, сборка argv, разбор вывода
   в сводку, `job_status` / `job_view`.
2. Джобы (`jobs.create_job`, `jobs.launch`, `jobs.cancel`) переиспользуются как есть.
3. Зарегистрировать в `server.py` инструменты `<agent>_start`, `<agent>_wait` и т.д.
   Новая регистрация в Claude не нужна: достаточно перезапустить сессию.
