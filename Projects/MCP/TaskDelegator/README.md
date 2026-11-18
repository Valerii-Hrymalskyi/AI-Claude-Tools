# TaskDelegator

Локальный MCP-сервер `task-delegator`: через него Claude поручает работу другим ИИ-агентам
на этом ПК — **OpenAI Codex** (через `codex exec`) и **worker B**, Claude Code на втором аккаунте
Claude (через `claude -p`). До 2026-11-18 проект назывался AITools — см.
[миграцию](#миграция-с-aitools).

Ниже `<clone>` — папка клона этого репозитория, `<python>` — `<clone>\Utils\Bin\Windows\Python3\python.exe`.

- [Что нужно до установки](#что-нужно-до-установки) · [Установка](#установка) · [Проверка](#проверка)
- [Риски и ограничения](#риски-и-ограничения) — прочитать до первой задачи на запись
- [Пример сессии](docs/example-session.md) · [Пример правил делегирования для CLAUDE.md](docs/claude-md-example.md)

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
`CLAUDE*`, `ANTHROPIC*` и `MCP_*` в Codex не передаются. Что из этого следует —
[риски и ограничения](#риски-и-ограничения).

Если задача на запись стартует, пока идёт другая задача на запись в пересекающейся папке, в ответе
`codex_start` / `worker_start` будет `warning` с `job_id` той задачи.

## Лимиты Codex

У Codex два окна лимитов: пятичасовое (`5h`, в данных `primary`) и недельное (`7d`, `secondary`).
Их читает отдельный скрипт `codex_usage.py`. Сервер вызывает его подпроцессом с `--json` и
опирается только на контракт `schema: 1`, описанный в
[docs/codex-usage-requirements.md](docs/codex-usage-requirements.md). Чтение бесплатное:
квота не тратится.

**Скрипт — внешняя зависимость, в этом репозитории его нет.** Он общий с хуком самого Codex и
живёт в соседнем рабочем каталоге автора: `<clone>\..\Codex\Utils\Scritps\GetUsage\codex_usage.py`
(сервер ищет его там по умолчанию; `Scritps` — настоящее имя папки). В другом месте — задайте
путь в `TASK_DELEGATOR_CODEX_USAGE_SCRIPT`. Без скрипта всё работает, но каждая проверка лимитов
даёт `unknown`: задачи запускаются без проверки, и `limits_cost` не считается. Свой скрипт
можно написать по контракту из `docs/codex-usage-requirements.md`. Найден ли скрипт, показывает
`delegator_doctor` (поле `codex_usage.exists`) и `scripts\doctor.py`.

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
| `write` | `--permission-mode acceptEdits`; правки внутри `cwd`/`add_dirs` и shell (с сетью); `git push` запрещён |
| `write` + `shell=false` | только правки файлов: Bash и PowerShell запрещены |

> **B в режиме `write` работает под вашим пользователем Windows, с его правами и с сетью.** Shell у
> него — обычный Bash, не песочница: он может запустить любую программу, поставить пакет,
> обратиться к любому устройству, подключённому к ПК. Ограничение «только `cwd`/`add_dirs`»
> касается инструментов правки, а не shell. Если shell задаче не нужен, давайте `shell=false`.
> Запрет команд задаётся правилами разрешений Claude Code в `TASK_DELEGATOR_WORKER_DENY`
> (через `;`, по умолчанию `Bash(git push:*)`; например,
> `Bash(git push:*);Bash(avrdude:*);Bash(STM32_Programmer_CLI:*)`). Это защита от случайности,
> а не граница безопасности: правило по префиксу команды обходится, если очень постараться.

**Умолчания B — выбор автора:** `claude-opus-5-5`, effort `xhigh`; в задачах на запись — Ultracode
(`ultracode=true`), в задачах на чтение — никогда (субагенты workflow могли бы получить право на
правки). На подписке Pro это быстро расходует окно 5h: у автора тривиальный вопрос на Opus с
Ultracode стоил около 10 % окна, 12-минутное исследование на Opus `xhigh` — около 39 %. На другом
плане задайте свои умолчания переменными сервера (см. [настройки](#настройки-переменные-окружения-сервера)),
например `TASK_DELEGATOR_WORKER_MODEL=sonnet`, `TASK_DELEGATOR_WORKER_EFFORT=medium`,
`TASK_DELEGATOR_WORKER_ULTRACODE=0`. Действующие умолчания видны в описании параметров `worker_start`.

Как Ultracode включается в `claude -p` (исследовал сам B, строки проверены по бинарнику 2.1.325):
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

Сервер лимиты B не проверяет (в отличие от Codex): за ними следит ведущий Claude по
`rate_limits` из прошлых результатов. Если окно исчерпано, задача B сразу завершается со статусом
`failed` и сообщением вида «You've hit your 5-hour limit», а `rate_limits.status` = `rejected`.

**Разовый вход во второй аккаунт** (в своём окне PowerShell, не через Claude):

```powershell
powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\worker_login.ps1
```

Откроется Claude Code с отдельной папкой конфига: `/login`, вход вторым аккаунтом, `/exit`.
Проверка: `delegator_doctor` → `worker_b.auth.loggedIn: true`.

Отдельный пользователь Windows с задачей Планировщика для этого не нужен.

## Модель и effort

Сервер проверяет `model` и `effort` по кэшу моделей самого Codex (`~/.codex/models_cache.json`,
или `%CODEX_HOME%`). Если модель неизвестна или не поддерживает такой effort, задача не стартует,
а в ошибке перечислено, что доступно. Если кэша нет, проверка пропускается. Полный список моделей
с описаниями и уровнями показывает `delegator_doctor`.

Какую модель и какой effort выбирать, решает Claude. В инструкциях сервера — только общее
правило (самая дешёвая пара, которая справится; `max`/`ultra` — по просьбе пользователя).
Конкретную политику — какие модели, когда, сколько можно тратить — задаёт пользователь в своём
`~/.claude/CLAUDE.md`; образец раздела — [docs/claude-md-example.md](docs/claude-md-example.md).
Если `model` и `effort` не переданы, Codex возьмёт их из своего конфига пользователя
(`~/.codex/config.toml`) — часто это не то, что нужно для мелкой задачи.

## Отложенный запуск: `scripts/codex_job.py`

Ставит задачу через то же ядро, что и сервер (она видна в `codex_status`), но из командной
строки. Удобно, когда MCP-инструменты не загружены или задачу нужно запустить к определённому
времени:

```powershell
& "<python>" scripts\codex_job.py --task-file task.md `
  --cwd D:\Some\Project --mode write --model <модель> --effort medium --start-at 05:25 --wait-min 90
```

Перед стартом он проверяет лимиты. Если окно за порогом паузы, он ждёт его сброса (плюс
запас 120 с) и проверяет снова, а не падает. Пока ждёт и работает, не даёт Windows уснуть по
бездействию; от закрытой крышки или ручного «Сна» это не спасает. Остальные опции:
`--pause-5h` / `--pause-7d` (свои пороги), `--no-limits`, `--limits-script`, `--dry-run`.
Коды выхода: 0 — задача выполнена, 1 — ошибка, 3 — задача ещё идёт, а `--wait-min` истёк.

## Состав

```
server.py                         MCP-сервер (MCP Python SDK 2.x, MCPServer)
task_delegator/jobs.py            джобы на диске, запуск и отслеживание процессов (не зависит от агента)
task_delegator/codex.py           провайдер Codex: поиск exe, аргументы, разбор событий, limits_cost
task_delegator/limits.py          лимиты Codex: вызов скрипта usage, пороги, стоимость задачи
task_delegator/claude_worker.py   провайдер worker B: поиск claude.exe, права, разбор stream-json
task_delegator/worker_settings.json  настройки, включающие workflows для Ultracode (передаются через --settings)
task_delegator/procs.py           Windows: проверка «жив ли PID», kill дерева процессов
scripts/install_task_delegator.ps1  регистрация сервера в Claude Code с путями этого клона
scripts/doctor.py                 проверка сервера через настоящий MCP-клиент (delegator_doctor), бесплатно
scripts/worker_login.ps1          разовый вход во второй аккаунт Claude
scripts/codex_job.py              запуск задачи из командной строки, в том числе к заданному времени
docs/                             контракт скрипта лимитов, пример сессии, пример раздела CLAUDE.md
tests/unit_test.py                офлайн-тесты: события, личность процесса, TOML, поиск exe, лимиты, worker B
tests/smoke_test.py               сквозной тест через настоящий MCP-клиент и настоящих агентов (тратит квоту)
```

## Что нужно до установки

- **Windows 10/11.** Код процессов (`procs.py`), пути и скрипты рассчитаны только на Windows.
- **Python** ставит `<clone>\python_setup.bat`: портативный 3.15.2 из NuGet (нужен доступ к
  `api.nuget.org`) и пакеты из `python-requirements.txt`, в том числе `mcp==2.3.0`. Системный
  Python не нужен и не используется.
- **Claude Code** — настольное приложение или CLI. Сервер регистрируется на уровне пользователя.
- **Для Codex:** Codex CLI или приложение Codex (`codex.exe` ищется сам), выполненный вход
  (`codex login`) и подписка с лимитами Codex. Скрипт лимитов `codex_usage.py` —
  [желателен, но не обязателен](#лимиты-codex).
- **Для worker B:** второй аккаунт Claude с собственной подпиской (у B свои лимиты) и разовый
  вход через `scripts\worker_login.ps1`. Без него работает всё, кроме `worker_*`.

## Установка

1. `<clone>\python_setup.bat` — Python и пакеты (повторный запуск безопасен).
2. Регистрация в Claude Code, в своём окне PowerShell:

   ```powershell
   powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\install_task_delegator.ps1
   ```

   Скрипт находит `claude.exe`, регистрирует `task-delegator` для всех проектов с путями этого
   клона, убирает старую регистрацию `aitools`, если она есть, и проверяет результат:
   `claude mcp get task-delegator` и `scripts\doctor.py` (что найдено, где вход, где скрипт
   лимитов; строки с `!!` требуют внимания). Ключи: `-DryRun` — только показать команды;
   `-Force` — перерегистрировать (например, после переноса клона; прежние переменные сервера
   сохраняются); `-ServerEnv "K=V;K2=V2"` — [переменные сервера](#настройки-переменные-окружения-сервера);
   `-Claude <путь>` — свой `claude.exe`.
3. Для worker B — разовый вход (см. [выше](#worker-b-второй-аккаунт-claude-code)).
4. Новые инструменты появляются в сессиях Claude, **начатых после** регистрации.

Вручную то же самое:

```powershell
& "<claude.exe>" mcp add --scope user task-delegator -- "<python>" "<clone>\Projects\MCP\TaskDelegator\server.py"
```

`claude.exe` настольного приложения лежит в
`%LOCALAPPDATA%\Claude-Data\claude_*\claude-code\<версия>\<хеш>\claude.exe`.
Удалить регистрацию: `claude mcp remove task-delegator -s user`.

## Проверка

```powershell
& "<python>" scripts\doctor.py              # бесплатно: сервер отвечает, что он нашёл
& "<python>" tests\unit_test.py             # офлайн, без агентов, около минуты
```

**Сквозной тест тратит настоящую квоту.** Он запускает реальные задачи, хоть и маленькие:

```powershell
& "<python>" tests\smoke_test.py                    # Codex: ~9 задач, ~3 мин, у автора ~1–3 % окна 5h
& "<python>" tests\smoke_test.py --quick            # Codex: 3 задачи, без write/add_dirs/cancel
& "<python>" tests\smoke_test.py --worker           # worker B вместо Codex: ~5 задач на haiku
& "<python>" tests\smoke_test.py --worker --quick   # worker B: 3 задачи
```

Задачи Codex в смоук-тесте идут без `model`, то есть на модели из вашего `~/.codex/config.toml`
с effort `low`. Если исчерпан лимит B, тест `--worker` останавливается на первой задаче и
говорит, когда сброс.

Офлайн-тесты гоняет CI (`.github/workflows/tests.yml`, `windows-latest`): `unit_test.py` и
самотест UsageGuard.

## Настройки (переменные окружения сервера)

Задаются при регистрации: `install_task_delegator.ps1 -ServerEnv "…"` или `claude mcp add -e K=V`.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `TASK_DELEGATOR_CODEX_EXE` | ищется сам | явный путь к `codex.exe` |
| `TASK_DELEGATOR_STATE_DIR` | `%LOCALAPPDATA%\TaskDelegator` | где хранить джобы |
| `TASK_DELEGATOR_CODEX_MAX_JOBS` | `3` | сколько задач Codex может идти одновременно |
| `TASK_DELEGATOR_KEEP_DAYS` | `14` | через сколько дней удалять завершённые джобы |
| `TASK_DELEGATOR_CODEX_USAGE_SCRIPT` | `<clone>\..\Codex\Utils\Scritps\GetUsage\codex_usage.py` | скрипт лимитов Codex |
| `TASK_DELEGATOR_CODEX_WARN_5H` / `TASK_DELEGATOR_CODEX_PAUSE_5H` | `90` / `95` | пороги пятичасового окна, % |
| `TASK_DELEGATOR_CODEX_WARN_7D` / `TASK_DELEGATOR_CODEX_PAUSE_7D` | `95` / `98` | пороги недельного окна, % |
| `TASK_DELEGATOR_CLAUDE_EXE` | ищется сам | явный путь к `claude.exe` для worker B |
| `TASK_DELEGATOR_WORKER_CONFIG_DIR` | `%USERPROFILE%\.claude-worker-b` | папка конфига второго аккаунта |
| `TASK_DELEGATOR_WORKER_MAX_JOBS` | `2` | сколько задач B может идти одновременно |
| `TASK_DELEGATOR_WORKER_MODEL` | `claude-opus-5-5` | модель B по умолчанию |
| `TASK_DELEGATOR_WORKER_EFFORT` | `xhigh` | effort B по умолчанию |
| `TASK_DELEGATOR_WORKER_ULTRACODE` | `1` | Ultracode в задачах на запись по умолчанию (`0` — выключить) |
| `TASK_DELEGATOR_WORKER_WORKFLOW_AGENTS` | `3` | сколько агентов workflow одновременно при Ultracode |
| `TASK_DELEGATOR_WORKER_DENY` | `Bash(git push:*)` | что запрещено B в задачах на запись с shell, правила через `;` |

`codex.exe` ищется в `%LOCALAPPDATA%\OpenAI\Codex\bin\*\` (берётся самый свежий),
затем в `PATH`. Папка с хешем меняется при каждом обновлении Codex, поэтому путь
не зашит в код.

## Риски и ограничения

- **Codex всегда запускается с `approval_policy="never"`.** В фоне некому отвечать на запросы
  подтверждения, поэтому всё, что песочница не разрешила, сразу отклоняется. Границу задаёт
  песочница Codex: в `read-only` — только чтение, в `write` — запись внутри `cwd`/`add_dirs`
  (и временных папок), сеть в shell выключена.
- **Worker B в `write` — не песочница**: shell с сетью и полными правами вашего пользователя
  Windows (подробнее — [в разделе B](#worker-b-второй-аккаунт-claude-code)). Давайте ему узкий
  `cwd`, `shell=false`, когда shell не нужен, и свой `TASK_DELEGATOR_WORKER_DENY`, если на
  машине есть опасные утилиты (программаторы, деплой, отправка почты).
- **Задачи живут дольше сессии.** Codex и B — отдельные фоновые процессы: они продолжают
  работать после закрытия сессии Claude, перезапуска сервера или приложения и даже после
  того, как их никто не ждёт. Остановить — `codex_cancel` / `worker_cancel` (из любой новой
  сессии: история на диске), либо завершить процесс `codex.exe` / `claude.exe` вручную.
- **Одновременная правка одних файлов не блокируется.** При старте задачи на запись сервер
  ищет другие идущие задачи на запись (Codex и B), чьи `cwd`/`add_dirs` пересекаются с новой
  (та же папка, вложенная или родительская), и добавляет в ответ `warning` с их `job_id`.
  Запуск при этом не запрещается: две задачи в одном репозитории нормальны, если они правят
  разные файлы. Правки самого ведущего Claude сервер не видит.
- **Квота тратится в фоне.** Лимиты Codex сервер проверяет перед каждым стартом (при наличии
  скрипта лимитов), лимиты B — нет.
- **Переменные окружения** `CLAUDE*`, `ANTHROPIC*` и `MCP_*` ведущей сессии к агентам не
  передаются; всё остальное окружение пользователя — передаётся.

## Где хранится история задач

Каждая задача — папка `%LOCALAPPDATA%\TaskDelegator\jobs\<job_id>\` (или
`TASK_DELEGATOR_STATE_DIR`): задание, поток событий, ответ, коды выхода. Обычно это от
десятков килобайт до нескольких мегабайт на задачу (у автора: 38 задач — 21 МБ). Завершённые
задачи старше `TASK_DELEGATOR_KEEP_DAYS` дней (по умолчанию 14) сервер удаляет сам при каждом
запуске. Удалить вручную можно любую папку завершённой задачи или всю папку `jobs`, когда
задач в работе нет: `codex_status` / `worker_status` просто перестанут их показывать.

## Миграция с AITools

До 2026-11-18 проект назывался AITools. Теперь: сервер `task-delegator`, инструменты
`mcp__task-delegator__*` (`aitools_doctor` стал `delegator_doctor`), пакет `task_delegator`,
переменные `TASK_DELEGATOR_*`, история задач — `%LOCALAPPDATA%\TaskDelegator`. Что сделать:

1. Запустить `scripts\install_task_delegator.ps1`: он удалит старую регистрацию `aitools` и
   зарегистрирует `task-delegator`. Вручную: `claude mcp remove aitools -s user`.
2. Если при регистрации вы задавали серверу переменные со старым префиксом — переименовать их
   в `TASK_DELEGATOR_*` (`-ServerEnv`).
3. Старую историю `%LOCALAPPDATA%\AITools` удалить или перенести её `jobs\*` в
   `%LOCALAPPDATA%\TaskDelegator\jobs`, если старые задачи нужны в `codex_status`.
4. Поправить в своём `CLAUDE.md` имена инструментов и сервера, если они там упоминаются.

## Как добавить другого агента

1. Сделать `task_delegator/<agent>.py` по образцу `codex.py`: поиск exe, сборка argv, разбор вывода
   в сводку, `job_status` / `job_view`.
2. Джобы (`jobs.create_job`, `jobs.launch`, `jobs.cancel`) переиспользуются как есть.
3. Зарегистрировать в `server.py` инструменты `<agent>_start`, `<agent>_wait` и т.д.
   Новая регистрация в Claude не нужна: достаточно перезапустить сессию.
