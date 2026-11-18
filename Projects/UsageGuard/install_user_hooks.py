"""Wires usage_guard.py into every Claude account on this machine, at user level.

For each config dir (~/.claude, ~/.claude-worker-b): adds the three hooks to its
settings.json, appends the usage-budget section to its CLAUDE.md and links
skills/long-run to the single copy next to this script (a junction, no admin).
Idempotent: a step already done is reported and skipped.

All paths come from where this clone lives. The hooks run the clone's portable Python
(<clone>/Utils/Bin/Windows/Python3/python.exe, installed by python_setup.bat); without it,
the Python that runs this installer.

    <clone>\\Utils\\Bin\\Windows\\Python3\\python.exe <clone>\\Projects\\UsageGuard\\install_user_hooks.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLONE_ROOT = os.path.dirname(os.path.dirname(HERE))
_PORTABLE_PY = os.path.join(CLONE_ROOT, "Utils", "Bin", "Windows", "Python3", "python.exe")
PY = (_PORTABLE_PY if os.path.isfile(_PORTABLE_PY) else sys.executable).replace("\\", "/")
GUARD = os.path.join(HERE, "usage_guard.py").replace("\\", "/")
HOME = os.path.expanduser("~")
CONFIG_DIRS = (os.path.join(HOME, ".claude"),
               os.environ.get("TASK_DELEGATOR_WORKER_CONFIG_DIR") or os.path.join(HOME, ".claude-worker-b"))


def cmd(*mode):
    return {"type": "command", "command": PY, "args": [GUARD, *mode], "timeout": 15}


HOOKS = {
    "UserPromptSubmit": [{"hooks": [cmd("-mode", "prompt")]}],
    "PostToolUse": [{"matcher": "*", "hooks": [cmd("-mode", "watch")]}],
    "Stop": [{"hooks": [cmd("-mode", "stop", "-probe", "force")]}],
}

SECTION = """
## Бюджет использования (usage guard)

Окно 5h реальное: 429 обрывает ход посреди мысли, и до моего «продолжай» ничего не
возобновится. Опрашивать бюджет не нужно, хуки сообщают его сами: блок `Limits: …` на каждом моём
сообщении, то же после вызова инструмента начиная с 90 %% и итог после каждого хода. Цифры
запрашиваются у сервера для **этого** аккаунта. Вручную проверяй только для планирования —
сколько задачи влезет до сброса:

```
%(py)s %(guard)s -mode status
```

**Кончился бюджет — это пауза, а не конец; так по умолчанию на любой задаче.** На PAUSE: записать
чекпоинт, взвести ожидание (фоновый процесс, ожидание ничего не стоит), сказать одну строку,
остановиться. Когда оно разбудит — перепроверить `status`, перечитать чекпоинт и продолжать
**не спрашивая**. Полный протокол — навык **`long-run`**; цифры и устройство —
`%(doc)s`.

Отменить взведённое автопродолжение я могу словами «стоп», «останови» — тогда `TaskStop` монитора
и подтверждение. Молчание означает продолжать. Возобновлённая работа остаётся внутри исходной
задачи: всё необратимое или выходящее наружу по-прежнему требует меня.
""" % {"py": PY, "guard": GUARD,
       "doc": os.path.join(HERE, "usage-limits.md").replace("\\", "/")}


def wire(cfg):
    if not os.path.isdir(cfg):
        print("no such account dir, skipped:", cfg)
        return

    path = os.path.join(cfg, "settings.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    if "hooks" in data:
        print("hooks already present, left as is (merge by hand if needed):", path)
    else:
        data["hooks"] = HOOKS
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        print("hooks ->", path)

    link = os.path.join(cfg, "skills", "long-run")
    if os.path.exists(link):
        print("skill already present:", link)
    else:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", link,
                        os.path.join(HERE, "skills", "long-run")], check=True)

    md = os.path.join(cfg, "CLAUDE.md")
    existing = ""
    if os.path.exists(md):
        with open(md, encoding="utf-8") as fh:
            existing = fh.read()
    if "usage guard" in existing:
        print("CLAUDE.md already has the section:", md)
        return
    with open(md, "a", encoding="utf-8") as fh:
        fh.write(SECTION if existing.strip() else SECTION.lstrip("\n"))
    print("section ->", md)


if __name__ == "__main__":
    for cfg in CONFIG_DIRS:
        wire(cfg)
