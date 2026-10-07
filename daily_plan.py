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
WEEKDAYS_ACC = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]


# ── Readiness rules ──────────────────────────────────────────────────────────

@dataclass
class Readiness:
    level: str = "green"
    reasons: list[str] = field(default_factory=list)
    quality_ok: bool = True     # threshold / VO2 session allowed
    long_ok: bool = False       # long run allowed
    day_off: bool = False       # scheduled rest day

    def cap(self, level: str, reason: str) -> None:
        if LEVELS[level] > LEVELS[self.level]:
            self.level = level
        self.reasons.append(reason)

    def no_quality(self, reason: str) -> None:
        self.quality_ok = False
        self.reasons.append(reason)


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

    # Race recovery window
    races = [x for x in past if x["km"] >= RACE_KM and (today - x["date"]).days <= RACE_RECOVERY_DAYS]
    if races:
        days = (today - races[-1]["date"]).days
        race = "марафона" if races[-1]["km"] >= 42 else f"забега {races[-1]['km']:.0f} км"
        if days <= 2:
            r.cap("red", f"{days} дн. после {race} — отдых")
            r.quality_ok = False
        elif days <= 7:
            r.cap("yellow", f"{days} дн. после {race} — восстановление")
            r.quality_ok = False
        else:
            r.no_quality(f"{days} дн. после {race} — интенсивность рано")

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


# ── Plan text ────────────────────────────────────────────────────────────────

def allowed_sessions(r: Readiness, z: dict) -> list[str]:
    easy = f"{z['easy'][0]}–{z['easy'][1]}"
    if r.day_off:
        return ["ВЫХОДНОЙ: бега нет. Прогулка, растяжка или ролл — по желанию; цель — свежим выйти на воскресный длительный."]
    if r.level == "red":
        return [f"ОТДЫХ: полный отдых или 20–30 мин очень лёгкого бега/ходьбы, пульс до {z['recovery_max']}. Без интенсивности."]
    if r.level == "yellow":
        return [f"ЛЁГКИЙ ДЕНЬ: 30–60 мин лёгкого бега, пульс {easy}. Без интервалов и ускорений."]
    if r.long_ok:
        return [f"ДЛИТЕЛЬНЫЙ БЕГ (воскресенье): пульс {easy}, не больше чем на 10–15% длиннее последнего длительного."]
    options = [f"Аэробный бег 45–70 мин, пульс {easy}, в конце 4–6 ускорений по 20 с с полным отдыхом."]
    if r.quality_ok:
        options.append(f"Пороговая тренировка: темповые отрезки суммарно 20–30 мин, пульс {z['threshold'][0]}–{z['threshold'][1]}.")
        options.append(f"Интервалы на МПК: отрезки 2–4 мин, пульс от {z['vo2_min']}, суммарно 12–20 мин работы, отдых трусцой равный отрезку.")
    return options


def build_prompt(today: date, r: Readiness, metrics: list[str], z: dict, snap: Snapshot) -> str:
    recent = [a for a in snap.acts if (today - a["date"]).days <= 14 and a["date"] <= today]
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
    today_done = any(a["date"] == today and a["sport"] == "running" for a in recent)
    options = "\n".join(f"{i}. {o}" for i, o in enumerate(allowed_sessions(r, z), 1))
    context = "\n".join(f"- {c}" for c in snap.context)

    return f"""Ты тренер по бегу. Составь тренировку на сегодня ({WEEKDAYS[today.weekday()]}, {today:%d.%m.%Y}).

Долгосрочная цель бегуна: {GOAL}. Лучший марафон за год: {best_txt}.
Это многолетняя цель: тренировки строй от ТЕКУЩЕГО уровня и пульсовых зон, а не от целевого темпа 4:15/км.
Принципы: восстановление в приоритете; ~80% объёма легко; не больше 2 интенсивных тренировок в неделю;
прирост недельного объёма не больше 10%.
Расписание недели: суббота — выходной, воскресенье — длительный бег, ключевые тренировки лучше во вторник и четверг.

Состояние после сна (решение по готовности уже принято правилами, НЕ повышай интенсивность):
{chr(10).join('- ' + m for m in metrics)}
Готовность: {r.level}. Причины ограничений: {'; '.join(r.reasons) or 'нет'}.
{f"Дополнительно ({snap.source}):{chr(10)}{context}" if context else ""}

Пульсовые зоны: восстановление до {z['recovery_max']}, лёгкая {z['easy'][0]}–{z['easy'][1]},
средняя {z['steady'][0]}–{z['steady'][1]}, ПАНО {z['threshold'][0]}–{z['threshold'][1]}, МПК от {z['vo2_min']}.

Тренировки за 14 дней:
{history}
{"Сегодня тренировка УЖЕ выполнена — дай рекомендации по восстановлению, новую тренировку не назначай." if today_done else ""}

Разрешённые варианты на сегодня (выбери РОВНО ОДИН, с учётом баланса недели и дня недели):
{options}

Ответ строго в формате, без markdown и без лишних заголовков:
ТРЕНИРОВКА: название
РАЗМИНКА: ...
ОСНОВНАЯ ЧАСТЬ: конкретные отрезки/время с пульсом
ЗАМИНКА: ...
ЗАЧЕМ: 1–2 предложения, как это ведёт к цели
СЕГОДНЯ ВАЖНО: 1–2 совета по восстановлению (сон, питание, жара)

Для дня отдыха вместо разминки/заминки кратко опиши, чем заняться. Пиши по-русски, кратко и конкретно."""


def build_message(today: date, r: Readiness, metrics: list[str], plan: str, notes: list[str], source: str) -> str:
    esc = lambda s: html.escape(s, quote=False)
    reasons = "".join(f"\n• {esc(x)}" for x in r.reasons)
    return "\n".join([
        f"🌅 <b>План на {WEEKDAYS_ACC[today.weekday()]}, {today.day} {MONTHS_GEN[today.month - 1]}</b>",
        *(esc(n) for n in notes),
        "",
        f"Готовность: {'😴 <b>выходной</b>' if r.day_off else LEVEL_LABEL[r.level]}",
        *(f"• {esc(m)}" for m in metrics),
        *([f"\n<b>Ограничения:</b>{reasons}"] if r.reasons else []),
        "",
        "🏃 <b>Тренировка</b>",
        esc("\n".join(line.rstrip() for line in plan.splitlines())),
        "",
        f"<i>Данные: {source}. Боль, недомогание или пульс выше обычного на разминке — снижай нагрузку или отдыхай.</i>",
    ])


# ── Main ─────────────────────────────────────────────────────────────────────

def load_snapshot(today: date, source: str, notes: list[str]) -> Snapshot:
    if source in ("auto", "garmin"):
        try:
            return garmin_snapshot(today)
        except (Exception, SystemExit) as exc:   # SystemExit: Garmin auth failed
            if source == "garmin":
                raise
            print(f"Garmin unavailable, falling back to Tredict: {exc!r}", file=sys.stderr)
            notes.append("⚠️ Garmin недоступен — план по данным Tredict")
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
    zones, zones_note = hr_zones(snap)
    if zones_note:
        notes.append(zones_note)

    readiness, metrics = assess(today, snap)
    try:
        plan = ask_groq(build_prompt(today, readiness, metrics, zones, snap), max_tokens=3000)
    except RuntimeError as exc:
        # The rules already decided what is safe; send that rather than nothing
        plan = f"⚠️ {exc}. Рекомендация по правилам:\n{allowed_sessions(readiness, zones)[0]}"

    message = build_message(today, readiness, metrics, plan, notes, snap.source)
    if args.dry_run:
        print(message)
        return
    if not tg_send(message):
        sys.exit("Failed to send plan to Telegram")
    print("Plan sent.")


if __name__ == "__main__":
    main()
