# AI-Claude-Tools

Локальные инструменты для работы с Claude Code на Windows: делегирование задач другим
агентам и контроль лимитов подписки. Не привязаны ни к одному проекту — работают на уровне
машины и аккаунтов Claude.

| Проект | Что делает |
|---|---|
| [`Projects/MCP/TaskDelegator`](Projects/MCP/TaskDelegator/README.md) | MCP-сервер `task-delegator`: Claude поручает задачи агентам — Codex и Claude Code на других аккаунтах, — следит за ними и за их лимитами |
| [`Projects/UsageGuard`](Projects/UsageGuard/README.md) | хуки Claude Code: бюджет 5h/7d в каждой сессии, пауза и автопродолжение вместо 429 |

## Что нужно до установки

- **Windows 10/11** — другие ОС не поддерживаются.
- **Доступ к `api.nuget.org`** при первой установке: оттуда скачивается портативный Python
  3.15.2 (SHA-256 закреплён), пакеты — из PyPI (`mcp==2.3.0` и зависимости). Системный Python
  не нужен.
- **Claude Code** — настольное приложение или CLI.
- **TaskDelegator:** для Codex — Codex CLI/приложение с выполненным входом; для «другого
  Claude» — ещё один аккаунт Claude со своей подпиской (их может быть несколько). Скрипт лимитов Codex `codex_usage.py` — внешняя
  зависимость, без него лимиты Codex не проверяются
  ([подробнее](Projects/MCP/TaskDelegator/README.md#лимиты-codex)).
- **UsageGuard:** по желанию — токен `claude setup-token` для каждого аккаунта (точнее цифры).

## Начало работы

Клон можно положить в любую папку; ниже она — `<clone>`. Скрипты находят пути от своего
расположения.

1. **Python.** Двойной клик по `python_setup.bat` (или `python_setup.ps1` из PowerShell).
   Скачивает портативный Python закреплённой версии с NuGet, проверяет SHA-256 и ставит его
   с пакетами из `python-requirements.txt` в `Utils\Bin\Windows\Python3`. В git он не хранится.
   Повторный запуск безопасен; `-Force` — переустановить.
2. **UsageGuard.** Токен для каждого аккаунта и подключение хуков (`install_user_hooks.py`) —
   [`Projects/UsageGuard/README.md`](Projects/UsageGuard/README.md).
3. **TaskDelegator.** Регистрация MCP-сервера (`scripts\install_task_delegator.ps1`) и вход в
   другие аккаунты Claude (`scripts\claude_login.ps1 -Account <имя>`) —
   [`Projects/MCP/TaskDelegator/README.md`](Projects/MCP/TaskDelegator/README.md).
   Перед первой задачей на запись прочитайте
   [риски и ограничения](Projects/MCP/TaskDelegator/README.md#риски-и-ограничения): агенты
   работают в фоне с правами вашего пользователя Windows.

После переноса клона в другую папку: снова `install_user_hooks.py` (пути в хуках) и
`install_task_delegator.ps1 -Force` (пути в регистрации сервера).

## Проверка

```powershell
& "<clone>\Utils\Bin\Windows\Python3\python.exe" "<clone>\Projects\MCP\TaskDelegator\tests\unit_test.py"
& "<clone>\Utils\Bin\Windows\Python3\python.exe" "<clone>\Projects\UsageGuard\verify_usage_guard.py"
```

Оба офлайновые и бесплатные; их же гоняет CI (`.github/workflows/tests.yml`). Сквозные
тесты TaskDelegator тратят настоящую квоту — см. его README.

## Структура

```
python_setup.bat            запуск установки Python двойным кликом
python_setup.ps1            установка Python: версия и SHA-256 закреплены в начале файла
python-requirements.txt     точный набор пакетов (pip freeze)
Projects/
  MCP/TaskDelegator/        MCP-сервер делегирования задач
  UsageGuard/               контроль лимитов, навык long-run
Utils/Bin/Windows/Python3/  портативный Python (ставится скриптом, в .gitignore)
.github/workflows/tests.yml CI: офлайн-тесты на windows-latest
```

## Обновить Python или пакеты

- **Версия Python:** поменять `$Version` и `$Sha256` в `python_setup.ps1` (хэш сверить с
  SHA-512 из каталога NuGet), запустить `python_setup.bat -Force`.
- **Пакеты:** поставить нужное через `Utils\Bin\Windows\Python3\python.exe -m pip install …`,
  прогнать тесты проектов и записать набор: `python.exe -m pip freeze` в `python-requirements.txt`.

Python, которым пользуется работающая сессия Claude или MCP-сервер, заменить нельзя: скрипт
откажется и покажет, какие процессы и какие приложения его держат.

---

## In English

Local tools for Claude Code on Windows. The documentation is in Russian; this is a short overview.

- **[TaskDelegator](Projects/MCP/TaskDelegator/README.md)** — a local MCP server (`task-delegator`)
  that lets Claude hand work to other agents in the background. An agent is a kind plus an account:
  `codex` (OpenAI Codex, `codex exec`), `codex:<name>`, and `claude:<name>` — "another Claude",
  headless Claude Code on another Claude account (`claude -p`), as many accounts as you like. One
  set of tools for all of them: `list_agents`, `task_start/_wait/_status/_reply/_cancel`,
  `delegator_doctor`. Per-agent defaults live in an optional `agents.json`. Jobs live on disk and
  survive a server restart; every agent's rate limits are checked before each start.
- **[UsageGuard](Projects/UsageGuard/README.md)** — Claude Code hooks that show the 5-hour / 7-day
  subscription budget in every session and, near the limit, make Claude checkpoint and pause
  (skill `long-run`) instead of hitting a 429, then resume after the reset.

**Requirements:** Windows 10/11; access to api.nuget.org on first setup; Claude Code; for Codex jobs
the Codex CLI signed in; for another Claude, one more Claude account (or several). The Codex limits script
(`codex_usage.py`) is an external dependency — without it limit checks report `unknown`.

**Setup** (the clone can live anywhere, `<clone>` below):

1. `<clone>\python_setup.bat` — portable Python 3.15.2 (pinned SHA-256) and packages.
2. `<clone>\Utils\Bin\Windows\Python3\python.exe <clone>\Projects\UsageGuard\install_user_hooks.py`
   — hooks, the `long-run` skill and a CLAUDE.md section for each Claude account on the machine.
3. `powershell -ExecutionPolicy Bypass -File <clone>\Projects\MCP\TaskDelegator\scripts\install_task_delegator.ps1`
   — registers the MCP server and runs a free health check.
4. Another Claude account: `scripts\claude_login.ps1 -Account <name>`, then `/login` with that account.

**Risks.** Codex always runs with `approval_policy="never"` inside its sandbox (no network in its
shell). Another Claude in `write` mode is *not* sandboxed: it runs shell commands with network access and
your full Windows user rights (`shell=false` limits it to file edits; `git push` is denied by default).
Background jobs keep running after the Claude session closes. Two write jobs in overlapping folders
are not blocked, only flagged with a `warning`. The end-to-end smoke tests spend real quota; the
offline tests (`unit_test.py`, `verify_usage_guard.py`) do not and run in CI.
