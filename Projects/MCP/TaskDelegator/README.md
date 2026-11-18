# TaskDelegator

Локальный MCP-сервер `task-delegator`: через него Claude поручает работу другим ИИ-агентам на этом
ПК. **Агент — это вид плюс аккаунт:** `codex` (OpenAI Codex), `codex:<имя>` (Codex на другом
аккаунте), `claude:<имя>` («другой Claude» — Claude Code на другом аккаунте Claude, со своими
лимитами). Инструменты одни для всех агентов: `list_agents`, `task_start`, `task_wait`,
`task_status`, `task_reply`, `task_cancel`, `delegator_doctor`.

Ниже `<clone>` — папка клона этого репозитория, `<python>` — `<clone>\Utils\Bin\Windows\Python3\python.exe`.

- [Что нужно до установки](#что-нужно-до-установки) · [Установка](#установка) · [Проверка](#проверка)
- [Риски и ограничения](#риски-и-ограничения) — прочитать до первой задачи на запись
- [Пример сессии](docs/example-session.md) · [Пример правил делегирования для CLAUDE.md](docs/claude-md-example.md)
- [Миграция](#миграция) с AITools и с `worker_*`

## Как это работает

```
Claude Code ──MCP/stdio──► server.py: list_agents / task_start / task_wait / task_status / task_reply / task_cancel
                              │  общие правила: очередь на агента, лимиты ok/warn/pause, пересечение задач на запись
                              ▼
                        реестр агентов:  codex   codex:<имя>   claude:<имя> …     (task_delegator/agents.py)
                              │
                        адаптеры видов:  codex.py               claude.py        (новый вид = один файл)
                              ▼
                        фоновый процесс: codex exec --json  /  claude -p --output-format stream-json
                              └─► %LOCALAPPDATA%\TaskDelegator\jobs\<job_id>\   (всё по задаче)
```

Каждая задача — это «джоба» в отдельной папке:

| Файл | Что в нём |
|---|---|
| `job.json` | агент, параметры запуска, PID, флаг отмены |
| `prompt.txt` | точный текст задания (передаётся агенту через stdin) |
| `stdout.log` | поток событий агента (JSONL): команды, правки файлов, сообщения |
| `last_message.txt` | финальный ответ (Codex) |
| `stderr.log`, `exit.json` | диагностика и код выхода |

Статус задачи вычисляется из этих файлов. Поэтому джоба переживает перезапуск сервера: агент
продолжает работать, а `task_status` после рестарта по-прежнему её видит. Процесс опознаётся по
паре «PID + время создания». Если Windows отдаст тот же PID другому процессу, сервер не примет его
за свою задачу и не убьёт его через `task_cancel`. Задачу, завершившуюся без наблюдателя, сервер
один раз фиксирует в `exit.json` (`"unobserved": true`) и дальше её не перепроверяет.

## Агенты

Сервер находит агентов сам, настраивать ничего не нужно:

| Агент | Когда есть | Папка аккаунта |
|---|---|---|
| `codex` | найден `codex.exe` | обычная папка Codex (`~/.codex` или `CODEX_HOME`) |
| `codex:<имя>` | есть папка | `%USERPROFILE%\.codex-accounts\<имя>` (передаётся как `CODEX_HOME`) |
| `claude:<имя>` | есть папка | `%USERPROFILE%\.claude-accounts\<имя>` (передаётся как `CLAUDE_CONFIG_DIR`) |

Собственный аккаунт ведущего Claude (`~/.claude`) агентом не бывает: у него общие с ведущим лимиты.
Корень с папками `.claude-accounts` / `.codex-accounts` меняется переменной
`TASK_DELEGATOR_ACCOUNTS_ROOT`. Имя аккаунта: латиница, цифры, `.`, `_`, `-`, до 32 символов.

**Добавить другой аккаунт Claude** (в своём окне PowerShell, не через Claude):

```powershell
powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\claude_login.ps1 -Account <имя>
```

Откроется Claude Code с папкой этого аккаунта: `/login`, вход нужной учёткой, `/exit`. Агент
`claude:<имя>` сразу появится в `list_agents()`; перерегистрировать сервер и перезапускать сессию не
нужно. Ключ `-From <папка>` переносит уже существующую папку аккаунта вместе со входом. Аккаунт Codex
добавляется так же вручную: папка `.codex-accounts\<имя>` и `codex login` с `CODEX_HOME` на неё.

### Настройки агентов: `agents.json`

Необязательный файл `%LOCALAPPDATA%\TaskDelegator\agents.json` (или `TASK_DELEGATOR_AGENTS_FILE`):
умолчания и ограничения для каждого агента. Без него в коде нейтральные умолчания: модель и effort —
те, что заданы в самом аккаунте, Ultracode выключен.

```json
{
  "agents": {
    "claude:pro": {
      "name": "Second Claude (Pro)",
      "description": "Pro, для разработки",
      "model": "claude-opus-5-5",
      "effort": "xhigh",
      "options": {"ultracode": true},
      "max_jobs": 2,
      "deny": ["Bash(git push:*)", "Bash(avrdude:*)"]
    },
    "codex": {"model": "gpt-6.1-sol", "effort": "medium"},
    "claude:far": {"home": "D:\\Claude\\far"},
    "codex:old": {"disabled": true}
  }
}
```

| Ключ | Смысл |
|---|---|
| `name` | отображаемое имя, например `Second Claude (Pro)`; в отличие от id, может содержать пробелы |
| `description` | подсказка ведущему Claude, кому что поручать; видна в `list_agents()` |
| `model`, `effort` | умолчания, если в вызове не переданы |
| `options` | умолчания опций вида (см. ниже) |
| `max_jobs` | сколько задач этого агента может идти одновременно (Codex — 3, Claude — 2) |
| `deny` | Claude: правила разрешений, запрещённые в задачах на запись с shell (по умолчанию `["Bash(git push:*)"]`) |
| `workflow_agents` | Claude: сколько агентов workflow одновременно при Ultracode (по умолчанию 3) |
| `home` | папка аккаунта вне стандартного места |
| `disabled` | скрыть агента |

Ошибки в файле (в том числе значение не того типа) не роняют сервер: такой ключ пропускается, а
`list_agents()` и `delegator_doctor()` показывают причину в `problems`. Файл лежит вне репозитория,
поэтому имена аккаунтов и их настройки остаются только на вашей машине.

## Инструменты

| Инструмент | Что делает |
|---|---|
| `list_agents()` | кто есть, доступен ли, умолчания, опции, сколько задач идёт, текущие лимиты; бесплатно |
| `task_start(agent, task, cwd, mode, model?, effort?, add_dirs?, options?, ignore_limits?)` | проверяет лимиты агента, запускает задачу в фоне и сразу возвращает `job_id` |
| `task_wait(job_id, timeout_sec)` | ждёт до N секунд (максимум 600); по готовности возвращает ответ, правки, расход |
| `task_status(job_id?, agent?, limit?)` | без ожидания: прогресс задачи или список последних задач (можно по агенту) |
| `task_reply(job_id, message, mode?, options?, ignore_limits?)` | продолжает ту же сессию того же агента с полным контекстом; `add_dirs` и опции исходной задачи сохраняются |
| `task_cancel(job_id)` | останавливает задачу (убивает дерево процессов) |
| `delegator_doctor()` | где найдены исполняемые файлы, версии, входы в аккаунты, файлы настроек, пути, модели Codex |

Режимы `mode`: `read-only` (по умолчанию) и `write`. Что именно они разрешают, зависит от вида.

Если задача на запись стартует, пока идёт другая задача на запись (любого агента) в
пересекающейся папке, в ответе `task_start` / `task_reply` будет `warning` с `job_id` той задачи.

## Codex

- `read-only` — Codex читает файлы и запускает команды, но не пишет;
- `write` — правит файлы внутри `cwd` и `add_dirs` (песочница `workspace-write`, без сети).
- Опция `web_search` — живой поиск в интернете (работает и в `read-only`). У shell Codex сети нет
  в любом случае.

В `files_changed` попадают только успешно применённые правки, неудавшиеся идут отдельно в
`failed_file_changes`. Правки через shell-команды (например, `sed`) туда не попадают; для
git-папок в write-режиме есть ещё `git_status_after`.

Для запуска всегда ставится `approval_policy="never"`: в фоне некому отвечать на запросы
подтверждения, поэтому отказ сразу возвращается самому Codex. Переменные окружения
`CLAUDE*`, `ANTHROPIC*` и `MCP_*` в Codex не передаются.

Сервер проверяет `model` и `effort` по кэшу моделей самого Codex (`models_cache.json` в папке
аккаунта). Если модель неизвестна или не поддерживает такой effort, задача не стартует, а в
ошибке перечислено, что доступно. Если кэша нет, проверка пропускается. Полный список моделей с
описаниями и уровнями показывает `delegator_doctor`.

## Другой Claude (`claude:<имя>`)

`claude -p --output-format stream-json` на другом аккаунте Claude. Папка аккаунта — его
`CLAUDE_CONFIG_DIR`: там его вход, настройки, история и свой `CLAUDE.md`. `task-delegator` в нём не
зарегистрирован, так что другой Claude не может поручать задачи дальше. Переменные окружения
ведущей сессии (`CLAUDE*`, `ANTHROPIC*`) к нему не передаются.

| Режим | Права |
|---|---|
| `read-only` | `--permission-mode dontAsk`; разрешены Read, Grep, Glob, WebSearch, WebFetch; правки и shell запрещены |
| `write` | `--permission-mode acceptEdits`; правки внутри `cwd`/`add_dirs` и shell (с сетью); запреты из `deny` (по умолчанию `git push`) |
| `write` + `options.shell=false` | только правки файлов: Bash и PowerShell запрещены |

> **В режиме `write` другой Claude работает под вашим пользователем Windows, с его правами и с
> сетью.** Shell у него — обычный Bash, не песочница: он может запустить любую программу, поставить
> пакет, обратиться к любому устройству, подключённому к ПК. Ограничение «только `cwd`/`add_dirs`»
> касается инструментов правки, а не shell. Если shell задаче не нужен, давайте `shell=false`.
> `deny` в `agents.json` — защита от случайности, а не граница безопасности: правило по префиксу
> команды обходится, если очень постараться.

Опции: `shell` (по умолчанию `true`), `ultracode` (по умолчанию `false`), `max_turns`.

**Ultracode** — многоагентные workflow; только в задачах на запись (в задачах на чтение субагенты
могли бы получить право на правки). Дорого: у автора на подписке Pro тривиальный вопрос на Opus с
Ultracode стоил около 10 % окна 5h, 12-минутное исследование на Opus `xhigh` — около 39 %. Как он
включается в `claude -p` (строки проверены по бинарнику 2.1.325):
- ключевое слово «ultracode» в промпте `-p` **не работает**: оно срабатывает только на промпты,
  набранные человеком;
- на плане Pro workflows по умолчанию выключены; их включает `"enableWorkflows": true`, а запуск
  инструмента без вопросов — `"permissions": {"allow": ["Workflow"]}`. Обе настройки лежат в
  [task_delegator/worker_settings.json](task_delegator/worker_settings.json) и передаются в каждом
  запуске через `--settings`; собственный `settings.json` аккаунта не меняется;
- сам Ultracode включает `--effort ultracode` (подразумевает `xhigh`);
- `CLAUDE_CODE_WORKFLOW_MAX_CONCURRENT_AGENTS` ограничивает число одновременных агентов workflow
  (`workflow_agents` в `agents.json`, по умолчанию 3).

Проверка: при Ultracode в событии `system/init` есть инструмент `Workflow`, в результате это поле
`workflow_available: true`.

**Пауза по лимиту.** Если у другого Claude подходит к концу собственное окно 5h, он по навыку
`long-run` (UsageGuard) не ждёт сброса — в `claude -p` будить его некому, — а пишет чекпоинт и
заканчивает ответ строкой `PAUSED: 5h …, resets at …; checkpoint: …; next: …`. Поручивший дожидается
сброса (`list_agents()` показывает лимиты агента) и продолжает ту же сессию через `task_reply`.

Лимиты аккаунта приходят бесплатно в каждом результате: `rate_limits.unifiedWindows` (`five_hour`,
`seven_day`, доля использования от 0 до 1 и время сброса) — событие `rate_limit_event` из потока
`stream-json`. `result.cost_usd_list_price` — условная стоимость по прайсу API, для подписки она
справочная.

## Лимиты

У каждого агента два окна: пятичасовое (`5h`) и недельное (`7d`). Перед каждым стартом сервер
проверяет окна агента; `list_agents()` показывает их же.

| Состояние | Когда | Что делают `task_start` / `task_reply` |
|---|---|---|
| `ok` | ниже порогов | запускают задачу |
| `warn` | 5h ≥ 90 % или 7d ≥ 95 % | запускают, но в ответе предупреждение |
| `pause` | 5h ≥ 95 %, 7d ≥ 98 %, `limit reached` / последний запрос отклонён | отказывают и называют время сброса; обойти можно только `ignore_limits=true` |
| `unknown` | данных нет | запускают и честно пишут, что лимиты не проверены |

Пороги общие для всех агентов: `TASK_DELEGATOR_WARN_5H` / `TASK_DELEGATOR_PAUSE_5H`,
`TASK_DELEGATOR_WARN_7D` / `TASK_DELEGATOR_PAUSE_7D` (прежние имена `TASK_DELEGATOR_CODEX_*` тоже
читаются).

**Откуда цифры.**
- **Codex** — отдельный скрипт `codex_usage.py`: сервер вызывает его подпроцессом с `--json` и
  опирается только на контракт `schema: 1` из [docs/codex-usage-requirements.md](docs/codex-usage-requirements.md).
  Чтение бесплатное. **Скрипт — внешняя зависимость, в этом репозитории его нет.** Он общий с
  хуком самого Codex и живёт в соседнем рабочем каталоге автора:
  `<clone>\..\Codex\Utils\Scritps\GetUsage\codex_usage.py` (сервер ищет его там по умолчанию;
  `Scritps` — настоящее имя папки). В другом месте — задайте путь в
  `TASK_DELEGATOR_CODEX_USAGE_SCRIPT`. Без скрипта всё работает, но для Codex состояние всегда
  `unknown`, и `limits_cost` не считается.
- **Другой Claude** — последний `rate_limit_event` из задач этого аккаунта (бесплатно). Окно, время
  сброса которого уже прошло, считается начавшимся заново (0 %). Если задач у аккаунта ещё не было,
  состояние `unknown`.

**Стоимость задачи Codex.** Сервер сохраняет замер лимитов перед каждой задачей, а второй замер
снимает в момент, когда Codex завершился, и кладёт его в `limits_after.json`. В результате задачи
появляется `limits_cost`, например `{"5h": "+3%", "7d": "+1%"}`. Это прикидка: туда входит и любое
другое использование Codex за то же время. Если окно успело сброситься, вместо числа будет
`window reset during the job`. Если сервер в момент окончания не работал, а на задачу посмотрели
больше чем через 5 минут после её завершения, стоимость будет `unknown`. У другого Claude вместо
`limits_cost` — его `rate_limits` после задачи.

## Модель и effort

Какую модель и какой effort выбирать, решает Claude. В инструкциях сервера — только общее правило
(самая дешёвая пара, которая справится; `max`/`ultra` — по просьбе пользователя). Конкретную
политику — каких агентов, какие модели, когда, сколько можно тратить — задаёт пользователь в своём
`~/.claude/CLAUDE.md`; образец раздела — [docs/claude-md-example.md](docs/claude-md-example.md).
Постоянные умолчания агента удобно держать в `agents.json`. Если `model` и `effort` не переданы и
не заданы там, действуют настройки самого аккаунта (у Codex — `config.toml`), а это часто не то,
что нужно для мелкой задачи.

## Отложенный запуск: `scripts/codex_job.py`

Ставит задачу Codex через то же ядро, что и сервер (она видна в `task_status`), но из командной
строки. Удобно, когда MCP-инструменты не загружены или задачу нужно запустить к определённому
времени:

```powershell
& "<python>" scripts\codex_job.py --task-file task.md `
  --cwd D:\Some\Project --mode write --model <модель> --effort medium --start-at 05:25 --wait-min 90
```

Перед стартом он проверяет лимиты. Если окно за порогом паузы, он ждёт его сброса (плюс
запас 120 с) и проверяет снова, а не падает. Пока ждёт и работает, не даёт Windows уснуть по
бездействию; от закрытой крышки или ручного «Сна» это не спасает. Остальные опции:
`--agent codex:<имя>`, `--pause-5h` / `--pause-7d` (свои пороги), `--no-limits`, `--limits-script`,
`--web-search`, `--dry-run`. Коды выхода: 0 — задача выполнена, 1 — ошибка, 3 — задача ещё идёт,
а `--wait-min` истёк.

## Состав

```
server.py                         MCP-сервер (MCP Python SDK 2.x, MCPServer): инструменты и общие правила
task_delegator/agents.py          агенты: поиск аккаунтов, agents.json, интерфейс адаптера
task_delegator/codex.py           вид codex: поиск exe, аргументы, разбор событий, limits_cost
task_delegator/claude.py          вид claude: поиск claude.exe, права, разбор stream-json, лимиты аккаунта
task_delegator/jobs.py            джобы на диске, запуск и отслеживание процессов (не зависит от агента)
task_delegator/limits.py          пороги и вердикт ok/warn/pause, вызов скрипта лимитов Codex
task_delegator/worker_settings.json  настройки, включающие workflows для Ultracode (передаются через --settings)
task_delegator/procs.py           Windows: проверка «жив ли PID», kill дерева процессов
scripts/install_task_delegator.ps1  регистрация сервера в Claude Code с путями этого клона
scripts/claude_login.ps1          вход в другой аккаунт Claude (агент claude:<имя>)
scripts/doctor.py                 проверка сервера через настоящий MCP-клиент (delegator_doctor), бесплатно
scripts/codex_job.py              запуск задачи Codex из командной строки, в том числе к заданному времени
docs/                             контракт скрипта лимитов, пример сессии, пример раздела CLAUDE.md
tests/unit_test.py                офлайн-тесты, без настоящих агентов
tests/smoke_test.py               сквозной тест через настоящий MCP-клиент и настоящего агента (тратит квоту)
```

## Что нужно до установки

- **Windows 10/11.** Код процессов (`procs.py`), пути и скрипты рассчитаны только на Windows.
- **Python** ставит `<clone>\python_setup.bat`: портативный 3.15.2 из NuGet (нужен доступ к
  `api.nuget.org`) и пакеты из `python-requirements.txt`, в том числе `mcp==2.3.0`. Системный
  Python не нужен и не используется.
- **Claude Code** — настольное приложение или CLI. Сервер регистрируется на уровне пользователя.
- **Для Codex:** Codex CLI или приложение Codex (`codex.exe` ищется сам), выполненный вход
  (`codex login`) и подписка с лимитами Codex. Скрипт лимитов `codex_usage.py` —
  [желателен, но не обязателен](#лимиты).
- **Для другого Claude:** ещё один аккаунт Claude со своей подпиской (у него свои лимиты) и разовый
  вход через `scripts\claude_login.ps1 -Account <имя>`. Аккаунтов может быть несколько.

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
   сохраняются); `-ServerEnv "K=V;K2=V2"` — [переменные сервера](#переменные-окружения-сервера);
   `-Claude <путь>` — свой `claude.exe`.
3. Другие аккаунты Claude — [`claude_login.ps1 -Account <имя>`](#агенты); их умолчания — в
   [`agents.json`](#настройки-агентов-agentsjson).
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
& "<python>" scripts\doctor.py              # бесплатно: сервер отвечает, кого он нашёл
& "<python>" tests\unit_test.py             # офлайн, без агентов, около минуты
```

**Сквозной тест тратит настоящую квоту** агента, которого проверяет:

```powershell
& "<python>" tests\smoke_test.py                              # codex: ~9 задач, ~3 мин, у автора ~1–3 % окна 5h
& "<python>" tests\smoke_test.py --quick                      # codex: 3 задачи, без write/add_dirs/cancel
& "<python>" tests\smoke_test.py --agent claude:<имя>         # другой Claude: ~5 задач на haiku
& "<python>" tests\smoke_test.py --agent claude:<имя> --quick # 3 задачи
```

Задачи Codex в смоук-тесте идут с effort `low` на модели из `agents.json` или из настроек аккаунта.
Если лимит агента исчерпан, тест останавливается на первой задаче и говорит, когда сброс.

Офлайн-тесты гоняет CI (`.github/workflows/tests.yml`, `windows-latest`): `unit_test.py` и
самотест UsageGuard.

## Переменные окружения сервера

Задаются при регистрации: `install_task_delegator.ps1 -ServerEnv "…"` или `claude mcp add -e K=V`.
Настройки отдельных агентов — в [`agents.json`](#настройки-агентов-agentsjson).

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `TASK_DELEGATOR_STATE_DIR` | `%LOCALAPPDATA%\TaskDelegator` | где хранить джобы (и `agents.json`) |
| `TASK_DELEGATOR_AGENTS_FILE` | `<STATE_DIR>\agents.json` | файл настроек агентов |
| `TASK_DELEGATOR_ACCOUNTS_ROOT` | `%USERPROFILE%` | где искать `.claude-accounts` и `.codex-accounts` |
| `TASK_DELEGATOR_KEEP_DAYS` | `14` | через сколько дней удалять завершённые джобы |
| `TASK_DELEGATOR_CODEX_EXE` | ищется сам | явный путь к `codex.exe` |
| `TASK_DELEGATOR_CLAUDE_EXE` | ищется сам | явный путь к `claude.exe` для других аккаунтов Claude |
| `TASK_DELEGATOR_CODEX_USAGE_SCRIPT` | `<clone>\..\Codex\Utils\Scritps\GetUsage\codex_usage.py` | скрипт лимитов Codex |
| `TASK_DELEGATOR_WARN_5H` / `TASK_DELEGATOR_PAUSE_5H` | `90` / `95` | пороги пятичасового окна, % |
| `TASK_DELEGATOR_WARN_7D` / `TASK_DELEGATOR_PAUSE_7D` | `95` / `98` | пороги недельного окна, % |

`codex.exe` ищется в `%LOCALAPPDATA%\OpenAI\Codex\bin\*\` (берётся самый свежий), затем в `PATH`.
Папка с хешем меняется при каждом обновлении Codex, поэтому путь не зашит в код.

## Риски и ограничения

- **Codex всегда запускается с `approval_policy="never"`.** В фоне некому отвечать на запросы
  подтверждения, поэтому всё, что песочница не разрешила, сразу отклоняется. Границу задаёт
  песочница Codex: в `read-only` — только чтение, в `write` — запись внутри `cwd`/`add_dirs`
  (и временных папок), сеть в shell выключена.
- **Другой Claude в `write` — не песочница**: shell с сетью и полными правами вашего пользователя
  Windows (подробнее — [в его разделе](#другой-claude-claudeимя)). Давайте ему узкий `cwd`,
  `shell=false`, когда shell не нужен, и свой `deny`, если на машине есть опасные утилиты
  (программаторы, деплой, отправка почты).
- **Задачи живут дольше сессии.** Агенты — отдельные фоновые процессы: они продолжают работать
  после закрытия сессии Claude, перезапуска сервера или приложения и даже после того, как их никто
  не ждёт. Остановить — `task_cancel` (из любой новой сессии: история на диске), либо завершить
  процесс `codex.exe` / `claude.exe` вручную.
- **Одновременная правка одних файлов не блокируется.** При старте задачи на запись сервер ищет
  другие идущие задачи на запись (любых агентов), чьи `cwd`/`add_dirs` пересекаются с новой (та же
  папка, вложенная или родительская), и добавляет в ответ `warning` с их `job_id`. Запуск при этом
  не запрещается: две задачи в одном репозитории нормальны, если они правят разные файлы. Правки
  самого ведущего Claude сервер не видит.
- **Квота тратится в фоне.** Лимиты проверяются перед каждым стартом; лимиты другого Claude
  известны только по его последней задаче, так что первая задача нового аккаунта идёт без проверки.
- **Переменные окружения** `CLAUDE*`, `ANTHROPIC*` и `MCP_*` ведущей сессии к агентам не
  передаются; всё остальное окружение пользователя — передаётся.

## Где хранится история задач

Каждая задача — папка `%LOCALAPPDATA%\TaskDelegator\jobs\<job_id>\` (или
`TASK_DELEGATOR_STATE_DIR`): задание, поток событий, ответ, коды выхода. Обычно это от десятков
килобайт до нескольких мегабайт на задачу (у автора: 38 задач — 21 МБ). Завершённые задачи старше
`TASK_DELEGATOR_KEEP_DAYS` дней (по умолчанию 14) сервер удаляет сам при каждом запуске. Удалить
вручную можно любую папку завершённой задачи или всю папку `jobs`, когда задач в работе нет:
`task_status` просто перестанет их показывать.

## Миграция

**С AITools (до 2026-11-18).** Сервер `aitools` стал `task-delegator`, пакет — `task_delegator`,
переменные — `TASK_DELEGATOR_*`, история задач — `%LOCALAPPDATA%\TaskDelegator`.
`scripts\install_task_delegator.ps1` удаляет старую регистрацию `aitools` сам (вручную:
`claude mcp remove aitools -s user`). Старую историю `%LOCALAPPDATA%\AITools` можно удалить.

**С `codex_*` / `worker_*` (тоже 2026-11-18, слой агентов).** Инструменты стали общими:

| Было | Стало |
|---|---|
| `codex_start(...)` | `task_start(agent="codex", ...)`; `web_search` → `options={"web_search": true}` |
| `worker_start(...)` | `task_start(agent="claude:<имя>", ...)`; `ultracode`, `shell`, `max_turns` → `options` |
| `codex_wait` / `worker_wait`, `…_status`, `…_reply`, `…_cancel` | `task_wait`, `task_status`, `task_reply`, `task_cancel` |
| `codex_limits()` | `list_agents()` (поле `limits` у каждого агента) |
| `codex_session_id` в результате | `session_id` |
| папка `%USERPROFILE%\.claude-worker-b` | `%USERPROFILE%\.claude-accounts\<имя>` |
| `TASK_DELEGATOR_WORKER_*` | ключи агента в `agents.json` |
| `TASK_DELEGATOR_CODEX_MAX_JOBS` | `max_jobs` в `agents.json` |

Что сделать:
1. Перенести папку второго аккаунта (вход и токен UsageGuard переедут вместе с ней; сессии и
   задачи этого аккаунта должны быть закрыты):
   ```powershell
   powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\claude_login.ps1 -Account <имя> -From "$env:USERPROFILE\.claude-worker-b"
   ```
2. Прежние умолчания worker B (Opus 5.5, `xhigh`, Ultracode) задать в `agents.json`, если они нужны
   (пример — [выше](#настройки-агентов-agentsjson)): в коде теперь нейтральные умолчания.
3. Поправить имена инструментов в своём `~/.claude/CLAUDE.md`.
4. Перезапустить сессии Claude: новые инструменты появятся в новых сессиях.

Старые задачи `worker-…` видны в `task_status`, но продолжить их через `task_reply` нельзя: аккаунт
в них не записан. Старые задачи Codex продолжаются как задачи агента `codex`.

## Как добавить другой вид агента

1. Сделать `task_delegator/<вид>.py` по образцу `codex.py` / `claude.py`: поиск exe, сборка argv,
   разбор вывода, `job_status` / `job_view`, лимиты, и в конце класс адаптера с `agents.register(...)`
   (интерфейс — `Adapter` в `agents.py`).
2. Добавить импорт модуля в `agents.load_kinds()`.
3. Джобы (`jobs.create_job`, `jobs.launch`, `jobs.cancel`) и инструменты `task_*` переиспользуются
   как есть. Новая регистрация в Claude не нужна: достаточно перезапустить сервер.
