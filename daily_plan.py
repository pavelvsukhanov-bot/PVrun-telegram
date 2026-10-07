"""
Training plan for today → Telegram, on demand: the user sends "?" to the bot,
a Cloudflare Worker (telegram_worker/worker.js) starts this workflow.

Recovery data comes from Garmin, or from Tredict if Garmin is unavailable
(plan_sources.py). Readiness is decided by fixed rules; the LLM only writes the
session within the limits those rules allow, so recovery always wins a conflict.

Usage:
    python daily_plan.py                    # build and send to Telegram
    python daily_plan.py --dry-run          # print the message instead of sending
    python daily_plan.py --source tredict   # force a source (testing)
"""

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import watch_workout
from llm import ask_groq
from plan_sources import TZ, Snapshot, garmin_snapshot, hr_zones, tredict_snapshot
from telegram import tg_send

GOAL = "марафон быстрее 3:00 (темп 4:15/км)"

# Classification of past sessions, calibrated on Aug-Oct 2026 runs. Garmin's
# own training-effect labels call easy runs in the heat TEMPO/THRESHOLD, so
# they are not used. Tredict effort per hour (when available) separates
# intervals (>=120) from easy runs (<=108); effort alone grows with duration.
HARD_EFFORT_PER_H = 120
HARD_AVG_HR = 152
HARD_TITLE = re.compile(r"интенсив|интервал|темп|порог|фартлек|\d+\s*[xх×]\s*\d", re.I)
LONG_KM = 16
LONG_MIN = 90
RACE_KM = 30            # race-length run → protected recovery window
RACE_RECOVERY_DAYS = 14
MAX_RUN_STREAK = 6      # consecutive running days before a forced rest
REST_WEEKDAY = 5        # Saturday: always a day off
LONG_WEEKDAY = 6        # Sunday: the only day for the long run

LEVELS = {"green": 0, "yellow": 1, "red": 2}
LEVEL_LABEL = {
    "green": "🟢 <b>можно работать</b>",
    "yellow": "🟡 <b>лёгкий день</b>",
    "red": "🔴 <b>отдых</b>",
}
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


# ── Readiness rules ──────────────────────────────────────────────────────────

@dataclass
class Readiness:
    level: str = "green"
    reasons: list[str] = field(default_factory=list)   # why the day is yellow/red
    limits: list[str] = field(default_factory=list)    # why intensity is off (bot's own schedule rules)
    quality_ok: bool = True     # threshold / VO2 session allowed
    long_ok: bool = False       # long run allowed
    day_off: bool = False       # scheduled rest day

    def cap(self, level: str, reason: str) -> None:
        if LEVELS[level] > LEVELS[self.level]:
            self.level = level
        self.reasons.append(reason)

    def no_quality(self, reason: str) -> None:
        self.quality_ok = False
        self.limits.append(reason)


def is_hard(run: dict) -> bool:
    title = run["title"].replace("​", "")   # Garmin names may contain zero-width spaces
    return ((run.get("effort_per_h") or 0) >= HARD_EFFORT_PER_H
            or (run["hr"] or 0) >= HARD_AVG_HR
            or bool(HARD_TITLE.search(title)))


def is_long(run: dict) -> bool:
    return run["km"] >= LONG_KM or run["min"] >= LONG_MIN


def fmt_pace(s: float | None) -> str:
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}/км"


def assess(today: date, snap: Snapshot) -> tuple[Readiness, list[str]]:
    """Returns readiness and metric lines for the message (not yet HTML-escaped)."""
    r = Readiness()
    lines = []
    past = [x for x in snap.acts if x["sport"] == "running" and x["date"] < today]

    warnings = 0   # physiological under-recovery signals; two together mean rest
    for s in snap.signals:
        lines.append(s.text)
        if s.level:
            r.cap(s.level, s.reason)
        warnings += s.warning
    if warnings >= 2:
        r.cap("red", "несколько признаков недовосстановления сразу")

    # A ramp is an injury risk from intensity first: cut quality, keep easy volume
    if snap.acwr:
        lines.append(f"Нагрузка неделя/месяц: {snap.acwr:.2f}")
        if snap.acwr > 1.8:
            r.cap("yellow", f"нагрузка за неделю резко выросла ({snap.acwr:.2f})")
        elif snap.acwr > 1.5:
            r.no_quality(f"нагрузка за неделю быстро растёт ({snap.acwr:.2f})")

    week_km = sum(x["km"] for x in past if x["date"] >= today - timedelta(7))
    month_km = sum(x["km"] for x in past if x["date"] >= today - timedelta(28)) / 4
    lines.append(f"Бег за 7 дней: {week_km:.0f} км (в среднем {month_km:.0f} км/нед)")

    # Race recovery window: any race-length run, or a run of >=15 km on a race day from the calendar
    race_days = {x["date"] for x in snap.races}
    races = [x for x in past if (x["km"] >= RACE_KM or (x["date"] in race_days and x["km"] >= 15))
             and (today - x["date"]).days <= RACE_RECOVERY_DAYS]
    if races:
        last = races[-1]
        days = (today - last["date"]).days
        race = "марафона" if last["km"] >= 42 else "полумарафона" if last["km"] >= 20 else f"забега {last['km']:.0f} км"
        rest, easy, no_quality = (2, 7, 14) if last["km"] >= RACE_KM else (1, 3, 7)   # (days) marathon vs half
        if days <= rest:
            r.cap("red", f"{days} дн. после {race} — отдых")
            r.quality_ok = False
        elif days <= easy:
            r.cap("yellow", f"{days} дн. после {race} — восстановление")
            r.quality_ok = False
        elif days <= no_quality:
            r.no_quality(f"{days} дн. после {race} — интенсивность рано")
    for race in snap.races:
        if race["date"] == today + timedelta(1):
            r.cap("yellow", f"завтра старт: {race['title']}")
            r.quality_ok = False

    # Session spacing. Long runs are their own weekly key session, not one of the 2 quality ones
    hard = [x for x in past if (today - x["date"]).days <= 7 and is_hard(x) and not is_long(x)]
    if any(is_hard(x) or is_long(x) for x in past if x["date"] == today - timedelta(1)):
        r.no_quality("вчера была тяжёлая или длительная тренировка")
    if len(hard) >= 2:
        r.no_quality(f"{len(hard)} интенсивные за 7 дней — лимит недели")

    longs = [x for x in past if is_long(x) and x["km"] < RACE_KM]
    days_since_long = (today - longs[-1]["date"]).days if longs else None
    if today.weekday() == LONG_WEEKDAY:
        r.quality_ok = False    # Sunday is reserved for the long run
        if not races and (days_since_long is None or days_since_long >= 6):
            r.long_ok = True

    streak = 0
    while any(x["date"] == today - timedelta(streak + 1) for x in past):
        streak += 1
    if streak >= MAX_RUN_STREAK:
        r.cap("red", f"{streak} дней подряд с бегом — нужен день отдыха")

    if today.weekday() == REST_WEEKDAY:
        r.day_off = True
        r.cap("red", "суббота — выходной")

    if r.level != "green":
        r.quality_ok = r.long_ok = False
    return r, lines


# ── Session choice ───────────────────────────────────────────────────────────

def allowed_sessions(r: Readiness, z: dict) -> list[str]:
    easy = f"{z['easy'][0]}–{z['easy'][1]}"
    if r.level == "yellow":
        return [f"ЛЁГКИЙ ДЕНЬ: 30–60 мин лёгкого бега, пульс {easy}. Без интервалов и ускорений."]
    if r.long_ok:
        return [f"ДЛИТЕЛЬНЫЙ БЕГ (воскресенье): пульс {easy}, не больше чем на 10–15% длиннее последнего длительного."]
    options = [f"Аэробный бег 45–70 мин, пульс {easy}, в конце 4–6 ускорений по 20 с с полным отдыхом."]
    if r.quality_ok:
        options.append(f"Пороговая тренировка: темповые отрезки суммарно 20–30 мин, пульс {z['threshold'][0]}–{z['threshold'][1]}.")
        options.append(f"Интервалы на МПК: отрезки 2–4 мин, пульс от {z['vo2_min']}, суммарно 12–20 мин работы, отдых трусцой равный отрезку.")
    return options


WATCH_CODE_RULES = """Код тренировки для часов. Блоки через «|»:
префикс w — разминка, c — заминка (у основной работы префикса нет); NxДЛИТ — повторы;
длительность: число = МИНУТЫ, секунды ОБЯЗАТЕЛЬНО с «s» (ускорения по 20 секунд — 20s, а не 20!);
после длительности всегда буква зоны: r восстановление, e лёгкая, m средняя, t ПАНО, v МПК;
«/ДЛИТзона» — отдых между повторами. Только латиница и цифры, без пробелов.
Примеры: w10e|4x8t/2e|c10e (4×8 мин ПАНО) · w10e|20m|c10e (20 мин в средней зоне) ·
w15e|5x3v/3r|c10e (5×3 мин МПК) · 50e|6x20sv/90sr (50 мин легко + 6 ускорений по 20 секунд) ·
120e (длительный 2 часа)"""


def build_prompt(today: date, r: Readiness, metrics: list[str], z: dict, snap: Snapshot) -> str:
    recent = [a for a in snap.acts if (today - a["date"]).days <= 14 and a["date"] < today]
    history = "\n".join(
        f"- {WEEKDAYS_SHORT[a['date'].weekday()]} {a['date']:%d.%m}: {a['sport']}, {a['km']:.1f} км, "
        f"{a['min']:.0f} мин, темп {fmt_pace(a['pace'])}, пульс {a['hr'] or '—'}, "
        f"нагрузка {a['load'] or 0:.0f}, «{a['title']}»"
        for a in recent
    ) or "- нет тренировок"
    marathons = [a for a in snap.acts if a["sport"] == "running" and a["km"] >= 42
                 and (today - a["date"]).days <= 365]
    best = min(marathons, key=lambda a: a["min"]) if marathons else None
    best_txt = f"{int(best['min'] // 60)}:{int(best['min'] % 60):02d} ({best['date']:%d.%m.%Y})" if best else "нет данных"
    options = "\n".join(f"{i}. {o}" for i, o in enumerate(allowed_sessions(r, z), 1))
    context = "\n".join(f"- {c}" for c in snap.context)

    return f"""Ты тренер по бегу. Выбери тренировку на сегодня ({WEEKDAYS[today.weekday()]}, {today:%d.%m.%Y}).

Долгосрочная цель бегуна: {GOAL}. Лучший марафон за год: {best_txt}.
Это многолетняя цель: тренировки строй от ТЕКУЩЕГО уровня и пульсовых зон, а не от целевого темпа 4:15/км.
Принципы: восстановление в приоритете; ~80% объёма легко; не больше 2 интенсивных тренировок в неделю;
прирост недельного объёма не больше 10%.
Расписание недели: суббота — выходной, воскресенье — длительный бег, ключевые тренировки лучше во вторник и четверг.

Состояние после сна (решение по готовности уже принято правилами, НЕ повышай интенсивность):
{chr(10).join('- ' + m for m in metrics)}
Готовность: {r.level}. Причины ограничений: {'; '.join(r.reasons + r.limits) or 'нет'}.
{f"Дополнительно ({snap.source}):{chr(10)}{context}" if context else ""}

Пульсовые зоны: восстановление до {z['recovery_max']}, лёгкая {z['easy'][0]}–{z['easy'][1]},
средняя {z['steady'][0]}–{z['steady'][1]}, ПАНО {z['threshold'][0]}–{z['threshold'][1]}, МПК от {z['vo2_min']}.

Тренировки за 14 дней:
{history}

Разрешённые варианты на сегодня (выбери РОВНО ОДИН, с учётом баланса недели и дня недели):
{options}

{WATCH_CODE_RULES}

Ответ строго в три строки, без markdown:
НАЗВАНИЕ: 2–4 слова, например «Порог 2×12», «Лёгкий бег», «Длительный 2 ч»
ЧАСЫ: код выбранной тренировки
ЗАМЕТКА: одна короткая фраза, только если сегодня есть что-то особенное именно по данным выше
(например: сон короче нормы, первая интенсивная после перерыва, нехватка анаэробной нагрузки по Garmin).
Общие советы про сон, воду, питание, растяжку и самочувствие НЕ писать. Если особенного нет: ЗАМЕТКА: нет"""


def watch_problem(blocks: list, r: Readiness) -> str | None:
    """Checks the code against today's rules — it must not be harder than the plan allows."""
    def minutes(zones: str) -> float:   # sustained work (>=60 s) in these zones
        return sum(b.reps * b.seconds for b in blocks if b.zone in zones and b.seconds >= 60) / 60 + \
               sum(b.reps * b.rest_seconds for b in blocks if b.rest_zone in zones and b.rest_seconds >= 60) / 60
    total = sum(b.total for b in blocks) / 60
    if r.level != "green" and any(b.zone in "mtv" for b in blocks):
        return "сегодня только лёгкий бег, без ускорений"
    if not r.quality_ok and minutes("mtv"):
        return "интенсивные отрезки сегодня не разрешены (допустимы только ускорения короче минуты)"
    limit = 180 if r.long_ok else 35 if r.level == "red" else 75 if r.level == "yellow" else 100
    if total > limit:
        return f"слишком долго: {total:.0f} мин при лимите {limit} мин"
    if minutes("t") > 35:
        return f"{minutes('t'):.0f} мин в зоне ПАНО — больше 35"
    if minutes("v") > 22 or any(b.zone == "v" and b.seconds > 6 * 60 for b in blocks):
        return "слишком много работы в зоне МПК (больше 22 мин или отрезки длиннее 6 мин)"
    return None


def checked_code(code: str, r: Readiness) -> tuple[list | None, str | None]:
    try:
        blocks = watch_workout.parse(code)
    except ValueError as exc:
        return None, str(exc)
    problem = watch_problem(blocks, r)
    return (None, problem) if problem else (blocks, None)


def field_value(name: str, answer: str) -> str:
    m = re.search(rf"^\s*{name}:\s*(.*?)\s*$", answer, re.M)
    return m.group(1).strip().strip("«»\"`") if m else ""


@dataclass
class Session:
    title: str
    code: str | None = None
    blocks: list | None = None
    note: str | None = None
    problem: str | None = None    # why there is no watch code


def choose_session(prompt: str, r: Readiness, z: dict) -> Session:
    """LLM picks one allowed session as title + watch code + optional note.

    The code is the session itself: it is checked against today's rules, gets
    one LLM retry if malformed or too hard, otherwise the session is dropped.
    """
    answer = ask_groq(prompt, max_tokens=3000)
    note = field_value("ЗАМЕТКА", answer)
    s = Session(field_value("НАЗВАНИЕ", answer) or "Тренировка",
                note=None if note.lower() in ("", "нет", "-", "—") else note)
    code = field_value("ЧАСЫ", answer)
    s.blocks, s.problem = checked_code(code, r)
    if s.problem:
        print(f"Watch code rejected ({code!r}): {s.problem}; retrying", file=sys.stderr)
        code = ask_groq(f"""Тренировка «{s.title}». Разрешено сегодня:
{chr(10).join(allowed_sessions(r, z))}

Код «{code}» неверен: {s.problem}.
{WATCH_CODE_RULES}
Напиши ТОЛЬКО исправленный код одной строкой, без пояснений.""", max_tokens=1500).strip().strip("`")
        s.blocks, s.problem = checked_code(code, r)
    if s.problem:
        print(f"Watch code rejected again ({code!r}): {s.problem}", file=sys.stderr)
    else:
        s.code = code
    return s


# ── Message ──────────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return html.escape(s, quote=False)


# Garmin training-effect labels of plan sessions that are fine on an easy day
EASY_PLAN_LABELS = {"AEROBIC_BASE", "RECOVERY", "UNKNOWN", ""}


def watch_button(code: str) -> dict:
    # The whole workout travels in the button (<=64 bytes): nothing is stored between steps
    return {"inline_keyboard": [[{"text": "⌚ Отправить на часы", "callback_data": f"W:{code}"}]]}


def plan_lines(snap: Snapshot, r: Readiness, z: dict) -> tuple[list[str], dict | None]:
    """Today's Garmin plan, passed through the recovery gate.

    The plan (already on the watch) sets the schedule; the bot only steps in
    when recovery says so: a hard or long session on a yellow day is swapped
    for an easy run (with a watch button), a red day cancels everything.
    """
    lines = [f"📋 <b>По плану Garmin</b> ({esc(snap.plan_info)})"]
    keyboard = None
    if not snap.plan_today:
        return lines + ["Отдых."], None
    for s in snap.plan_today:
        desc = esc(" · ".join(x for x in (s["name"], s["desc"], f"≈{s['min']} мин" if s["min"] else "") if x))
        if s["sport"] != "running":
            lines.append(f"➕ <s>{desc}</s> — сегодня пропусти" if r.level == "red" else f"➕ {desc}")
        elif r.level == "red":
            lines += [f"<s>{desc}</s>", f"🛌 Вместо неё отдых или 20–30 мин совсем легко, пульс до {z['recovery_max']}."]
        elif r.level == "yellow" and (s["long"] or s["label"] not in EASY_PLAN_LABELS):
            minutes = min(max(round(s["min"] * 0.6), 30), 60) if s["long"] else 40
            blocks, problem = checked_code(f"{minutes}e", r)
            lines += [f"<s>{desc}</s>", f"🔄 Замена: {minutes} мин легко, пульс {z['easy'][0]}–{z['easy'][1]}"]
            if blocks:
                keyboard = watch_button(f"{minutes}e")
        else:
            lines.append(f"🏃 {desc}")
            if r.level == "yellow":
                lines.append("Держи пульс в нижней части зоны.")
    return lines, keyboard


def session_lines(s: Session, z: dict) -> list[str]:
    total = sum(b.total for b in s.blocks) // 60
    return [f"🏃 <b>{esc(s.title)}</b> · ≈{total} мин",
            *(esc(line) for line in watch_workout.describe_lines(s.blocks, z)),
            *([f"💡 {esc(s.note)}"] if s.note else [])]


def build_message(today: date, label: str, summary: str, r: Readiness, body: list[str], notes: list[str],
                  show_limits: bool = True) -> str:
    """Short on purpose: the same lines every morning are noise."""
    lines = [f"<b>{WEEKDAYS_SHORT[today.weekday()].capitalize()}, {today.day} {MONTHS_GEN[today.month - 1]}</b> · {label}",
             *(esc(n) for n in notes)]
    if summary:
        lines.append(esc(summary))
    # Recovery signals come first; two reasons explain the decision, the rest is noise
    reasons = list(dict.fromkeys(r.reasons + (r.limits if show_limits else [])))[:2]
    if reasons and not r.day_off:
        lines.append("⚠️ " + esc("; ".join(reasons)))
    return "\n".join(lines + [""] + body)


# ── Main ─────────────────────────────────────────────────────────────────────

def load_snapshot(today: date, source: str, notes: list[str]) -> Snapshot:
    if source in ("auto", "garmin"):
        try:
            return garmin_snapshot(today)
        except (Exception, SystemExit) as exc:   # SystemExit: Garmin auth failed
            if source == "garmin":
                raise
            print(f"Garmin unavailable, falling back to Tredict: {exc!r}", file=sys.stderr)
            notes.append("⚠️ Garmin недоступен — данные Tredict")
    return tredict_snapshot(today)   # Tredict auth failure exits 3 → workflow alert


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--date", type=date.fromisoformat, help="plan for this date (testing)")
    parser.add_argument("--source", choices=["auto", "garmin", "tredict"], default="auto")
    args = parser.parse_args()

    today = args.date or datetime.now(TZ).date()
    notes: list[str] = []
    snap = load_snapshot(today, args.source, notes)
    zones, zones_note = hr_zones(snap.lt_hr)
    if zones_note:
        notes.append(zones_note)

    readiness, metrics = assess(today, snap)
    summary = " · ".join(s.short for s in snap.signals if s.short)
    label = "😴 <b>выходной</b>" if readiness.day_off else LEVEL_LABEL[readiness.level]
    keyboard = None

    # Rest days, race days and finished days need no LLM: nothing to choose
    race_today = next((x for x in snap.races if x["date"] == today), None)
    if race_today:
        label, body = "🏁 <b>старт</b>", [f"Сегодня {esc(race_today['title'])}. Удачи!"]
        readiness.reasons.clear()
    elif readiness.day_off:
        race_tomorrow = any(x["date"] == today + timedelta(1) for x in snap.races)
        body = ["Бега нет — завтра старт." if race_tomorrow else "Бега нет — завтра длительный."]
    elif any(a["date"] == today and a["sport"] == "running" for a in snap.acts):
        label, body = "✅ <b>сделано</b>", ["Тренировка сегодня уже выполнена."]
        readiness.reasons.clear()   # limits for a finished day are irrelevant
        readiness.limits.clear()
    elif snap.plan_info:
        # Garmin plan sets the schedule; only recovery signals apply, not the bot's own spacing rules
        body, keyboard = plan_lines(snap, readiness, zones)
    elif readiness.level == "red":
        body = [f"Отдых или 20–30 мин совсем легко, пульс до {zones['recovery_max']}."]
    else:
        try:
            session = choose_session(build_prompt(today, readiness, metrics, zones, snap), readiness, zones)
        except RuntimeError as exc:   # Groq down: the rules already decided what is safe
            session = Session("Тренировка", problem=str(exc))
        if session.code:
            body = session_lines(session, zones)
            keyboard = watch_button(session.code)
        else:
            body = [f"🏃 {esc(allowed_sessions(readiness, zones)[0])}",
                    f"<i>Без кнопки для часов: {esc(session.problem or 'нет кода')}</i>"]

    # On Garmin plan days the bot's own intensity rules do not apply, so they are not shown either
    message = build_message(today, label, summary, readiness, body, notes, show_limits=not snap.plan_info)
    if args.dry_run:
        print(message)
        print(f"[button] {keyboard['inline_keyboard'][0][0]['callback_data'] if keyboard else 'нет'}")
        return
    if not tg_send(message, keyboard):
        sys.exit("Failed to send plan to Telegram")
    print("Plan sent.")


if __name__ == "__main__":
    main()
