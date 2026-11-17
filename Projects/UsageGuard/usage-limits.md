---
kind: context
status: active
scope: "Claude account usage windows, telemetry, and guard protocol"
evidence: "account telemetry + observed calibration"
source: "api.anthropic.com response headers; <Claude application data>/plan-usage-history.json; <Claude session storage>/projects/<slug>/*.jsonl; usage_guard.py"
revision: "5dd155f (branch feature/dev_2.00_hrymalskyi)"
verified-at: 2026-08-17
last-reviewed: 2026-11-18
recheck-when: "Claude app/account policy, storage schema, or guard changes"
---

# Claude usage limits: where the numbers actually are

Everything here is about the *account*, not the project. It lives in the repo
because the tooling does, and because "the assistant went silent at 3 a.m." is a
team problem, not a personal one.

## The two windows

| Window | Key | Resets |
|---|---|---|
| 5-hour session | `fh` | 5 hours after the **first message** of the window |
| 7-day | `sd` | rolling, over days |

The 5-hour window is **not** a fixed grid. It opens with the first message sent
after the previous one expired, so its reset time drifts every day. Nothing is
consumed while nobody is working.

## Source 0 — спросить сервер напрямую (2026-08-17)

**Обе цифры и оба времени сброса отдаёт сервер, по запросу, точно.** Всё, что
описано ниже про интерполяцию и предсказание сброса, — теперь запасной путь на
случай, когда сети или токена нет.

Механизм: любой ответ `POST https://api.anthropic.com/v1/messages` несёт заголовки

```
anthropic-ratelimit-unified-5h-utilization: 0.33      # доля, не проценты
anthropic-ratelimit-unified-5h-reset:       1786980000 # epoch SECONDS
anthropic-ratelimit-unified-5h-status:      allowed
anthropic-ratelimit-unified-7d-utilization: 0.39
anthropic-ratelimit-unified-7d-reset:       1787454000
anthropic-ratelimit-unified-7d-status:      allowed
anthropic-ratelimit-unified-representative-claim: five_hour   # какое окно связывает
anthropic-ratelimit-unified-overage-status: rejected
anthropic-ratelimit-unified-overage-disabled-reason: org_level_disabled
```

Авторизация — `Bearer` с долгоживущим токеном `sk-ant-oat01-…`, который выдаёт
`claude setup-token`. Токен привязан к аккаунту, под которым выпущен, и
лежит в каталоге этого аккаунта: `<Claude config dir>/usage-limits-token`, где
`<Claude config dir>` — каталог из `CLAUDE_CONFIG_DIR`, без неё — `.claude` в профиле пользователя
(в репозиторий не попадает). Переменная окружения `CLAUDE_USAGE_LIMITS_CHECK_TOKEN`
учитывается только для аккаунта по умолчанию и только без файла: она
пользовательская, её наследует и второй аккаунт на той же машине — до
2026-11-18 оба аккаунта так мерили лимиты одного. По аккаунту разведены и кэш
пробы, и состояние watch (`<temp dir>/claude-usage-guard-*-<hash>.json`; у аккаунта
по умолчанию имена прежние), и стенограммы (`<config dir>/projects`). Файл
десктоп-приложения `plan-usage-history.json` относится к аккаунту по умолчанию,
у остальных этот фолбэк выключен. Stop-файл `claude-usage-guard-stop` общий
намеренно: это выключатель всех автовозобновлений на машине. Обязателен
заголовок `anthropic-beta: oauth-2025-04-20`.

**Как выпустить и прописать токен на новой машине —
[`README.md`](README.md).** Там же грабли с MSIX-путём,
на которых спотыкаются все, и таблица диагностики. Токен личный: каждый
разработчик выпускает свой, передавать его друг другу незачем и небезопасно.

**Есть и выделенный эндпоинт `GET api.anthropic.com/api/oauth/usage`, но он нам
недоступен:**
отвечает `403 permission_error — OAuth token does not meet scope requirement
user:profile`. `setup-token` выдаёт scope `user:inference`, а `user:profile`
есть только у сессионного токена самого приложения. Обходить это не нужно:
inference-заголовки несут ровно тот же набор данных.

Цена пробы — один запрос к Haiku с `max_tokens: 1`, порядка 11 взвешенных
токенов при 160 000 на процент. Это 0,00007 % окна, то есть измерение дешевле
собственной погрешности на четыре порядка.

Сверка 2026-08-17 16:03, живое окно:

| | сервер | guard |
|---|---|---|
| 5h | 33 % | 33,9 % оценка / 31 % confirmed |
| 5h reset | 18:20:00 | 18:23 |
| 7d | 39 % | 39 % |
| 7d reset | 23.08 06:00:00 | не считался вовсе |

Заметить: **`utilization` — доля (0.33), а не проценты**, а `reset` — epoch в
**секундах**, тогда как `t` в истории — в миллисекундах. Перепутать легко, и
ошибка в 1000× по времени сброса выглядит правдоподобно.

### Как это встроено в guard

Дробить надо **время, а не количество действий**. Между двумя соседними
вызовами инструмента проходят секунды, а счётчик за секунды не двигается:
обычная скорость 0,1–0,3 %/мин, худший всплеск в истории — 3,8 %/мин. Проба на
каждый вызов платила бы секунду за заведомо не изменившееся число.

Поэтому проба кэшируется с адаптивным TTL — чем ближе потолок, тем чаще:

| Утилизация | TTL | Худший дрейф между пробами |
|---|---|---|
| ≥ 85 % | 20 с | 1,3 % |
| 70–85 % | 45 с | 2,9 % |
| < 70 % | 90 с | 5,7 % (там это ничего не решает) |

Замерено на живом хуке `PreToolUse`, полный цикл процесса:

```
-probe off   (только файлы)   198 / 202 / 202 мс
-probe auto  (кэш свежий)     219 / 219 / 220 / 222 мс     <- +20 мс
-probe force (всегда по сети) 996 / 1013 мс
```

То есть подавляющее большинство вызовов стоит **+20 мс**, и лишь один раз в TTL
кто-то платит ~800 мс.

Лестница отказа, сверху вниз: свежий кэш → запрос по сети → кэш возрастом до
10 минут → локальный файл → оценка по стенограммам. Нет токена, нет сети,
`-probe off` — всё это ведёт к прежнему поведению без единого изменения.

Три вещи, каждая из которых была бы тихим дефектом:

- **429 несёт те же заголовки.** `HTTPError` разбирается наравне с успешным
  ответом — иначе ровно в момент исчерпания лимита проба слепнет.
- **Кэш с истёкшим собственным `reset` выбрасывается.** Иначе повторяется та же
  ловушка, что держала паузу на пустом окне часами.
- **Запись кэша атомарная** (через временный файл и `os.replace`): `PreToolUse`
  срабатывает во всех открытых диалогах разом, и они делят один файл.

Самотест `verify_usage_guard.py` — 8 случаев. Пять прежних прогоняются с
`-probe off` и остались нетронутыми; три новых (`F`, `G`, `H`) покрывают
перекрытие пробой, выброс протухшего кэша и спасение пробой окна, которое файл
уже считает истёкшим. Токен из окружения самотеста **вычищается**: иначе
отвергнутый фикстурный кэш уходил бы в сеть и тест начинал бы проверять живой
аккаунт вместо фикстуры.

## Source 1 — the authoritative numbers

`<Claude application data>/plan-usage-history.json`

```json
{"version": 2,
 "samples": [{"t": 1785925945467, "org": "<uuid>", "u": {"fh": 98, "sd": 36}}]}
```

- `t` — epoch ms, `fh` / `sd` — integer percent, 0..100.
- The desktop app polls the server and appends one sample. This is the same data
  `usage` displays.
- **[FIX 2026-08-17] Опрос стал 15-минутным, был 5-минутный.** По всей истории
  (4684 сэмпла) 5 мин — 91,9 % интервалов, но последний 5-минутный интервал
  датирован **2026-08-11 15:59**; после него все 122 сэмпла идут с шагом 15 мин
  и больше. Слепая зона между пробами выросла втрое, а на записанных всплесках
  +19 пунктов за интервал это уже до полусотни пунктов вслепую. Отсюда же
  ежедневная «STALE telemetry» в строке хука: порог устаревания считался от
  прежней частоты.
- Polling keeps working while the account is rate-limited, so the file is also
  how you learn that the window reopened.
- It only advances **while the desktop app is running**. A frozen file means
  stale numbers, not idle time — the guard reports that case separately.

Verified against every 429 in the local transcripts: each one sits exactly on a
sample with `fh: 100`.

## Source 2 — the transcripts, for the last five minutes

`<Claude session storage>/projects/<slug>/*.jsonl` — one line per event. Assistant lines carry
their own usage, written the moment the message ends:

```json
{"type": "assistant", "timestamp": "...", "requestId": "...",
 "message": {"model": "claude-opus-5",
             "usage": {"input_tokens": 2, "cache_creation_input_tokens": 16811,
                       "cache_read_input_tokens": 33960, "output_tokens": 1748}}}
```

Subagent and workflow transcripts live in `<session-uuid>/subagents/**` and count
against the same budget — a fan-out is exactly as expensive as it looks.

This matters because the 5-minute grid is too coarse near the ceiling: bursts of
**+19 percentage points inside a single interval** are on record. Between two
samples you are flying blind, and blind at 90 % ends in a 429.

**Do not promote this source back to being the main one.** It was measured against
the confirmed numbers on 2026-08-17 over 304 intervals
([`calibrate_usage_estimate.py`](calibrate_usage_estimate.py)):
it reported **0.42 of the real spend** — 733 points against 1749 — and in **203 of
those 304 intervals it saw nothing at all** while the real figure rose. The fault
is structural, not a constant to be tuned: it reads Claude Code transcripts only,
and the same window is also spent by ordinary desktop-app conversations, which it
cannot see. Inside its own project it overshoots instead. Wrong in both
directions — which is exactly why it now only fills the seconds since the last
probe, and never carries the verdict on its own.

## Окна перекрываются, и от этого ломается предсказание сброса

**Момент сброса надёжен только тогда, когда счётчик падает в ноль.**

Проверено 2026-08-10 на 36 падениях в истории. Там, где счётчик уходил в 0,
правило «падение + 5 часов» предсказывало следующий сброс с ошибкой меньше
минуты — четыре интервала подряд по 5.00, 5.01, 5.00, 5.00 ч.

Но 2026-08-10 в 19:54 счётчик упал с 74 % не в ноль, а в **12 %**. Значит второе
окно к тому моменту уже шло и стало видно только когда истекло первое. Скрипт
принял падение за начало и насчитал сброс на 00:59 — на деле окно кончилось в
**22:54**, на два часа раньше. Интервалы между последними сбросами: 13.02, 66.08,
7.25, **3.00** ч — пятичасовой ритм держится не всегда.

Поправка «отнять остаток, делённый на скорость» **не работает**: на падениях в
5 % и 3 % она превращала нулевую ошибку в 55 и 28 минут. Одного контрпримера
мало, чтобы построить модель, поэтому `usage_guard.py` теперь просто честно
говорит, что время сброса — верхняя граница, когда падение легло выше 2 %.

## Читать вывод `-mode status` целиком, а не хвостом

Скрипт **разделяет** подтверждённое и оценку:

```
5-hour window :  18.1%
  confirmed   : 2% (sample 9.7 min old)
  estimated + : 16.1% (2 582 024 weighted tokens since the sample)
```

Итоговая строка `verdict` этого разделения не несёт. 2026-08-10 я дважды
останавливал работу раньше времени, потому что читал вывод через `tail -4` и
видел только итог: 90 % там были почти целиком **оценкой поверх старого окна**,
которое уже успело закрыться. Оценка стоит ±40 %, подтверждённое — нет.

## Две ошибки чтения, обе мои, обе 2026-08-11

**`headroom` — это не время до сброса.** Строка хука `~17 min of headroom`
означает минуты до **порога паузы** (95 %) при текущей скорости сгорания. Время
до сброса — отдельное поле, `resets HH:MM`. Я прочитал одно как другое и трижды
сказал пользователю «до сброса двадцать минут», когда до сброса было три часа.
Решения «начинать шаг или нет» принимались по неверной величине.

**Поправку к оценке нельзя делать постоянной.** Ниже измерено, что на одном
окне оценка завысила вдвое. Я превратил это в правило «делить на два» — и на
следующем окне оно дало 75 % при настоящих 84 %, то есть **занижение**, что
опаснее завышения. Ошибка оценки непостоянна: 2,0× в одном режиме, 1,15× в
другом.

**Правило, которое действительно работает** (и было записано ниже ещё
2026-08-10, я просто перестал ему следовать): называть `confirmed`, `estimated +`
считать **верхней границей**, а когда от числа зависит решение — брать свежую
пробу, а не пересчитывать старую.

## Оценка завышает вдвое в режиме «длинный контекст, всё из кэша»

Измерено 2026-08-11 на живом окне, 210 минут:

```
fh 32% -> 76%                      44 пункта
cache_read  123 454 331 токенов    85 % всего взвешенного объёма
output          308 901
cache_write     504 550
input               625
                                   1 % = 330 028 взвешенных
скрипт закладывает                 1 % = 160 000
```

Ровно вдвое. Причём **оценка практически целиком считает перечитывание кэша, а
не работу**: при 98 % попаданий и полутора сотнях тысяч контекста чтение кэша с
весом 0,1 забивает и вывод, и запись кэша, и вход вместе взятые.

Прежняя калибровка (238 интервалов, 162 000 агрегатно) снималась на смешанном
трафике. Обе цифры верны каждая для своего режима — значит **неверен не
`tokens_per_pct`, а вес чтения кэша**: при 0,02 то же окно даёт 105 000 на
процент, что уже сопоставимо со смешанным замером. Одного окна мало, чтобы
разделить вес и константу: неизвестных два, уравнение одно. Нужен разбор по
составляющим на интервалах прежней калибровки — тогда решается система.

**Пока это не сделано, правило простое и оно уже написано ниже: называть
`confirmed`, а `estimated +` считать верхней границей.** 2026-08-11 я нарушил
собственное правило дважды за день — называл пользователю сумму как одно число и
принимал по ней решения «начинать или не начинать». Пользователь заметил
расхождение со своим экраном; экран был прав.

## Проба относится к своему окну — и протухает вместе с ним

`fh` описывает **то окно, в котором проба взята**, и больше ничего. Пока
десктопное приложение пишет историю каждые 5 минут, это незаметно. Как только
оно замолкает, последняя проба остаётся самой свежей сколько угодно долго — и
исчерпанное окно продолжает читаться как исчерпанное часами после сброса.

2026-08-11 это стоило запрета всех инструментов на пустом бюджете:

```
5-hour window : 100.0%
  confirmed   : 100% (sample 348.2 min old, STALE)
resets at     : 2026-08-11 03:58:17  (in -270 min)
verdict       : PAUSE
```

`in -270 min` — отрицательное время до сброса — и есть подпись дефекта: скрипт
сам посчитал, что окно кончилось четыре с половиной часа назад, и всё равно
держал паузу.

Правило, по которому это разбирается теперь:

- **Свежая проба всегда главнее любого вывода.** Если приложение пишет, а мы
  прошли предсказанный сброс, значит ошиблось предсказание, а не проба.
- Молчит приложение **и** мы прошли предсказанный сброс с запасом 15 мин →
  проба объявляется протухшей. Предсказание — верхняя граница (ошибается только
  в раннюю сторону, см. «Окна перекрываются»), поэтому пройти его — надёжный
  признак, что окно действительно закрылось.
- От протухшей пробы не остаётся подтверждённой части вовсе: счётчик обнулился,
  и всё, что известно о новом окне, берётся из стенограмм — считая **от сброса**,
  а не от пробы. Расход недельного окна при этом сохраняется: оно за пять часов
  не сбрасывается и может только вырасти.
- Скорость сгорания, измеренная внутри закрытого окна, обнуляется: о текущем она
  не говорит ничего.

Проверка — `verify_usage_guard.py`, пять случаев прогоняются через
настоящий скрипт. Два из них регрессионные и важнее остальных: «протухшая, но
окно ещё идёт» (устаревание само по себе не снимает паузу) и «свежая проба за
предсказанным сбросом» (без него условие свежести можно было бы выбросить
незамеченным — случай «свежая и исчерпанная» проходит сразу по двум причинам).
Заодно проверяется, что текст запрета собирается без незаполненных подстановок:
он форматируется вне `try`, и опечатка в имени поля уронила бы `PreToolUse` и
заблокировала бы вообще все инструменты.

## Calibration: what 1 % is worth

Measured over 144 five-minute intervals where `fh` rose and had not yet clipped
at 100, regressing token spend against Δ`fh`:

```
weighted = input + 1.25*cache_write + 0.1*cache_read + 5*output      (r = 0.83)

1 %   ~= 160 000 weighted tokens
100 % ~=  16 000 000 weighted tokens
```

**Пересчитано 2026-08-10** на 238 интервалах всей накопленной истории:
**162 000** в совокупности, 157 000 медиана — было заложено 140 000, то есть
скрипт завышал расход примерно на 14 %. Форма взвешивания подтвердилась: разброс
по интервалам 57× против 178× у сырых токенов и 220× у одних выходных, так что
менять надо было множитель, а не формулу.

Two sanity checks on the same data:

- **~435 000 output tokens** per full window. This is probably the number people
  mean when they guess "the limit is about 500k" — it is the output column only.
- Raw token count (everything summed unweighted) correlates far worse
  (r = 0.56) because cache reads dominate it: 759 M raw tokens over the same
  period against 140 M weighted. Do not budget on raw totals.

Per-model weights (Opus 1.0, Sonnet 0.2) did not improve the fit here — the
sample is 82 % Opus — but they are applied anyway, since a Sonnet-heavy day
would otherwise read four times too expensive.

**Spread: p25 = 96 k, p75 = 212 k weighted tokens per 1 %.** So the estimate is
good to roughly ±40 %. It is an interpolation for the five minutes since the
last sample, and nothing more. The authoritative number is always `fh`.

## Predicting the reset

`reset = first assistant message after the last reset + 5 h`

Checked against 22 observed resets: error within ±9 minutes, and most of that is
the 5-minute sampling grid. Two outliers of several hours were windows opened
from claude.ai in a browser — see the caveat below.

**Вытеснено «Source 0»: сервер присылает `5h-reset` и `7d-reset` готовыми.**
Правило остаётся только как фолбэк. Насколько оно врёт, когда якорь потерян,
измерено 2026-08-17: приложение было закрыто с 02:37 до 09:07, реальный сброс
попал внутрь этой дыры, первый сэмпл после неё показал `fh=31` — окно уже шло
45 минут. Скрипт принял этот момент за старт и назвал сброс на **15:26** при
настоящих **13:20**, промах два часа. Флаг `window_start_certain` при этом
взводился правильно (`reset_residual` 31 ≫ 2) — но строка хука его не
показывала, и наружу уходило уверенное «resets 15:26». Недельный сброс правило
не предсказывало вообще.

## When the limit is hit

The turn dies with a 429 and one line in the transcript:

```
apiErrorStatus: 429, error: "rate_limit"
"You've hit your session limit · resets 3:40am (Europe/Kiev)"
```

**Nothing resumes by itself.** On 2026-08-03 the window reopened at 3:40 and the
session sat idle until 8:30, when the user came back and typed "continue". That
is the failure this tooling exists to prevent — not the limit itself.

## Caveats

- Usage from claude.ai in a browser, or from another machine on the same
  account, moves `fh` but leaves no local transcript. The interpolation misses
  it; `fh` still sees it.
- `fh` is an integer, so anything under 1 % is invisible in the confirmed value.
- The hooks are on disk (user-level `settings.json` of each account), so every
  new session gets them, in any project. **The armed waiter is not**: it belongs to the session that
  armed it, fires there, and dies with it. Opening a new dialog does not move a
  pending auto-resume — and does not cancel it either.
- Closing the desktop app discards the waiter.

## Parallel dialogs

Everything per-dialog is keyed by session id, because the alternative is silent
corruption: a shared checkpoint file means dialog B overwrites A's plan and both
resume doing B's work.

| Shared across dialogs | Per dialog |
|---|---|
| the account budget itself (`fh` / `sd`) | the armed waiter |
| the history file | `CHECKPOINT-<session>.md` |
| the global stop flag | the `-<session>` stop flag, the `watch` throttle |

The percentages are account-wide and the interpolation scans every transcript,
so two dialogs are accounted correctly rather than each seeing only its own
spend. The consequence is that two dialogs resuming at the same reset burn the
new window about twice as fast.

The stop flags are judged by mtime against the waiter's own start time, never
consumed on read. Consume-on-read looks simpler and is wrong: the first waiter
to poll would eat a global flag meant for all of them.
- The 7-day window is the one that ruins an unattended run: it unwinds over
  days, so hitting it turns a five-hour pause into a stall of days. The guard
  judges it separately (warn 95 %, pause 98 %) and, when it is the blocker, says
  explicitly *not* to arm the waiter. Observed peak on this account: 73 %.

## Tooling

[`usage_guard.py`](usage_guard.py) — reads all three sources.

### Единый формат вывода (2026-08-17)

Числа всплывают в шести местах — два хука, `status`, `probe`, текст запрета и
`json`. Раньше каждое печатало по-своему, и одно и то же окно читалось то как
`5h 52%`, то как `5-hour window : 52.0%`. Теперь есть одна функция
`limits_block()`, и все пути печатают её дословно:

```
Limits:
5h: 59.0%, R: 2026-08-17 18:20:00
7d: 42.0%, R: 2026-08-23 06:00:00
(burn 0.7%/min)
```

- **Десятая доля печатается всегда, включая `.0`.** Поле, меняющее ширину от
  чтения к чтению, хуже сравнивать взглядом, а у самого потолка двигается именно
  десятая.
- **В заголовке — пессимистичная величина** (`live_pct`), потому что именно по
  ней судятся все пороги. Разбивка «измерено + оценка» уходит в скобки строкой
  ниже, вместе с `burn`, `headroom`, `STALE` и признаком, что данные локальные,
  а не измеренные.
- **`R: unknown` — честный ответ, а не дефект.** Без пробы недельный сброс
  неизвестен: правило «падение + 5 часов» его никогда и не предсказывало.

Текст запрета начинается тем же блоком, так что числа в нём не могут разойтись
с числами, которые к нему привели.

**Про `Stop says:` в выводе `-mode stop`.** Клиент приписывает этот префикс к
**каждой строке** `systemMessage`, так что блок приезжает с четырьмя префиксами.
Из хука это не отключается, и свести к одному префиксу сверху нельзя — скрипт
отдаёт только текст. Однострочный вариант пробовали 2026-08-17 и **отказались**:
префикс появляется один раз, но строка получается длинная и переносится, читать
хуже, чем блок с повторами. Формат везде остался один.

### Ожидание сброса: запас и подтверждение (2026-08-17)

Раньше `-mode wait` сторожил **падение счётчика в файле** — то есть узнавал о
сбросе с задержкой до 15 минут и целиком зависел от того, пишет ли приложение.
Теперь, когда сервер называет время сброса, waiter спит **до этого времени плюс
запас**, а затем подтверждает живой пробой.

| Параметр | По умолчанию | Ключ |
|---|---|---|
| запас после серверного сброса | 120 с | `-reset-margin-sec` |
| период перепроверки, если не обнулилось | 30 с | `-reset-recheck-sec` |

Запас нужен потому, что сервер катит окно по своим часам: проба, выпущенная
ровно в секунду сброса, может вернуться ещё на старом окне — и разбудить
ассистента в непустое окно, потратив на это checkpoint.

Подтверждение обязательно, а не «на доверии к часам». Если после запаса счётчик
**не** упал, waiter не объявляет сброс: он перевзводится на то время, которое
сервер называет теперь, и перепроверяет каждые 30 с, пока не увидит падение.

Проверено 2026-08-17 подстановкой фикстуры кэша:

- фикстура с `fh 60` и сбросом через секунду → цель наступила, waiter сходил на
  сервер, подменил фикстуру реальными `fh 59` и настоящим сбросом `18:20`,
  **ложного RESET не объявил** и перевзвёлся;
- фикстура с `fh 95` → падение до реальных 59 распознано как сброс, сообщение о
  пробуждении несёт свежий измеренный блок `Limits:`.

Ещё две мелочи, каждая из которых иначе стала бы тихим дефектом:

- **Общий таймаут переехал в начало цикла** — иначе он не ограничивал бы отрезок,
  проведённый во сне до серверного сброса.
- **Отказ по «файл замер» отключён, когда есть серверная цель.** Иначе waiter,
  спящий до 18:20, бросил бы работу через 25 минут только потому, что
  приложение не писало в файл, — при том что файл в этом режиме ничего не решает.

| Mode | Use |
|---|---|
| `status` | human-readable: live %, reset time, burn rate, headroom |
| `probe` | один запрос к серверу, сырыми числами. Кэш **не читает никогда** — смысл ручного запуска в том, чтобы узнать, живы ли токен и эндпоинт. Кэш при этом **пишет**, чтобы измерение не пропало зря |
| `json` | the same, for scripts |
| `prompt` | `UserPromptSubmit`: бюджет в контекст ассистента на каждом сообщении пользователя |
| `stop` | `Stop`: бюджет **пользователю** после окончания хода. Вывод обязан быть JSON с полем `systemMessage` — обычный stdout из `Stop` доходит только до транскрипта (ctrl+o), но не до разговора. Прописан с `-probe force` |
| `watch` | `PostToolUse`: the same line, but only from 90 %, throttled to one per 10 min or per 5-point band |
| `hook` | `PreToolUse`: denies budget-spending tools at 95 % of the 5-hour window or 98 % of the 7-day one, ~220 ms per call |
| `wait` | blocks until the window resets, prints one line, exits — run detached so that waiting costs nothing |

**The two thresholds are judged on different numbers, deliberately.** The warning
at 90 % looks at the *confirmed* percentage only; the denial at 95 % looks at
confirmed + estimate, the pessimistic one. The asymmetry follows from what each
mistake costs: warning too early wastes a step, denying too late loses the whole
turn together with the checkpoint. It was put in on 2026-08-17 after the estimate
raised 80, 85 and 91 % over one night against a confirmed 71 % and forced three
pointless wind-downs. Watch out when touching this: the confirmed value must be
read from `state["five_hour_pct"]`, never from a local copy — once a sample
expires the first is `None` while the second still holds last window's number,
and the guard starts warning on an empty window. `verify_usage_guard.py` caught
exactly that; run it after every edit.

**Пороги подняты 2026-08-17 с 88/92 и 93.** Прежний зазор оплачивал погрешность
**оценки**: числом могла оказаться проба пятнадцатиминутной давности плюс
интерполяция точностью около ±40 %. С появлением серверной пробы это измерение
свежестью в секунды, и та же безопасность помещается в меньший зазор. На худшей
записанной скорости сгорания (3,8 %/мин) пять пунктов между паузой и потолком —
около 80 секунд; checkpoint в белом списке и стоит заметно меньше процента, так
что запас остаётся.

Недельная пауза осталась на 98 %: там зазор меряется не минутами, а сутками, и
поднимать его незачем.

Since 2026-11-18 the hooks are wired in the user-level `settings.json` of each
account (README.md); `PreToolUse` (`hook`) has been unwired since 2026-09-09.
The four modes as designed: The split matters:
`prompt` covers interactive work, `watch` covers a long unattended stretch where
no user message arrives for hours, `hook` is the hard stop under both, and `stop`
closes the gap the other three leave open — **прирост за сам ход**. `prompt`
сообщает бюджет на момент, когда пользователь нажал ввод; к концу ответа ход уже
потратил часть, и до появления `stop` этого приращения не видел никто.

`stop` — единственный режим, который целится в **пользователя**, а не в
ассистента, отсюда и `-probe force`: устаревшее на 90 секунд число нормально
перед вызовом инструмента и неверно здесь, потому что оно обязано включать
только что закончившийся ход. Один запрос на ход, около секунды, в единственный
момент цикла, когда его никто не ждёт.

Ни один из четырёх не зависит от того, вспомнит ли ассистент посмотреть.

Hook stdout is decoded with the console codepage, so the injected line is
**ASCII only** — a `·` or an em dash arrives as replacement characters.

The pause/resume protocol built on top of it is the `long-run` skill.

## Корзины по моделям (2026-09-16)

Приложение показывает отдельную недельную цифру по модели («Fable 66 %» при
«всех моделях 98 %»). Откуда она и почему guard её не видит — проверено пробами:

- Заголовки `anthropic-ratelimit-unified-{5h,7d}-*` в ответе `v1/messages` — это
  **корзины «все модели»**, по ним и считает guard. Заголовков по моделям в ответе
  нет; есть только `…-representative-claim` (`seven_day`), `…-surpassed-threshold`
  и `…-overage-*`.
- Проба с `model: claude-fable-5-1` или `claude-opus-5` по нашему токену получает
  голый **429 `rate_limit_error` без единого unified-заголовка** (`x-should-retry:
  true`); проба на Haiku в ту же секунду отвечает 200 с цифрами. Так что через
  inference-токен корзину конкретной модели не прочитать.
- Цифру по модели приложение берёт с `claude.ai/api/organizations/<org>/…` — в его
  старом журнале ошибок (`logs/claude.ai-web.log`) виден ответ такого вида:
  `{"type":"exceeded_limit","perModelLimit":false,"representativeClaim":"five_hour",
  "windows":{"5h":{…},…}}`. Этот адрес требует сессию самого приложения
  (`user:profile`), которой у `setup-token` нет. Живого кэша этих ответов в данных
  приложения нет: `plan-usage-history.json` — это записи самого guard (`fh`, `sd`).
- Вывод: пока у guard только inference-токен, он честно меряет корзину «все
  модели» и **не может** мерить корзину Fable. Переключение модели меняет только
  то, что показывает приложение, а не то, что видит guard. Если по-настоящему
  нужна корзина модели — это либо токен с `user:profile`, либо чтение того, что
  показывает `usage` в самом Claude Code (его учётные данные трогать нельзя).
