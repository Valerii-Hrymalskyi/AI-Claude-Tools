# AI-Claude-Tools

Локальные инструменты для работы с Claude Code на Windows: делегирование задач другим
агентам и контроль лимитов подписки. Не привязаны ни к одному проекту — работают на уровне
машины и аккаунтов Claude.

| Проект | Что делает |
|---|---|
| [`Projects/MCP/TaskDelegator`](Projects/MCP/TaskDelegator/README.md) | MCP-сервер `task-delegator`: Claude поручает задачи Codex и второму аккаунту Claude, следит за ними и за их лимитами |
| [`Projects/UsageGuard`](Projects/UsageGuard/README.md) | хуки Claude Code: бюджет 5h/7d в каждой сессии, пауза и автопродолжение вместо 429 |

## Начало работы

1. **Python.** Двойной клик по `python_setup.bat` (или `python_setup.ps1` из PowerShell).
   Скачивает портативный Python закреплённой версии с NuGet, проверяет SHA-256 и ставит его
   с пакетами из `python-requirements.txt` в `Utils\Bin\Windows\Python3`. В git он не хранится.
   Повторный запуск безопасен; `-Force` — переустановить.
2. **UsageGuard.** Токен для каждого аккаунта и подключение хуков —
   [`Projects/UsageGuard/README.md`](Projects/UsageGuard/README.md).
3. **TaskDelegator.** Регистрация MCP-сервера и вход второго аккаунта —
   [`Projects/MCP/TaskDelegator/README.md`](Projects/MCP/TaskDelegator/README.md).

Пути в настройках и документации рассчитаны на клон в `E:\Development\AI\Claude`.

## Структура

```
python_setup.bat            запуск установки Python двойным кликом
python_setup.ps1            установка Python: версия и SHA-256 закреплены в начале файла
python-requirements.txt     точный набор пакетов (pip freeze)
Projects/
  MCP/TaskDelegator/        MCP-сервер делегирования задач
  UsageGuard/               контроль лимитов, навык long-run
Utils/Bin/Windows/Python3/  портативный Python (ставится скриптом, в .gitignore)
```

## Обновить Python или пакеты

- **Версия Python:** поменять `$Version` и `$Sha256` в `python_setup.ps1` (хэш сверить с
  SHA-512 из каталога NuGet), запустить `python_setup.bat -Force`.
- **Пакеты:** поставить нужное через `Utils\Bin\Windows\Python3\python.exe -m pip install …`,
  прогнать тесты проектов и записать набор: `python.exe -m pip freeze` в `python-requirements.txt`.

Python, которым пользуется работающая сессия Claude или MCP-сервер, заменить нельзя: скрипт
откажется и покажет, какие процессы и какие приложения его держат.
