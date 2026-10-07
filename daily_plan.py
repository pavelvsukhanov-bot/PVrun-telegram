"""
Training plan for today → Telegram, on demand: the user sends "?" to the bot,
a Cloudflare Worker (telegram_worker/worker.js) starts this workflow.

Readiness is decided by fixed rules from Tredict data (overnight HRV, sleep,
resting HR, training load, recent sessions). The LLM only writes the session
within the limits those rules allow, so recovery always wins a conflict.

Usage:
    python daily_plan.py             # build and send to Telegram
    python daily_plan.py --dry-run   # print the message instead of sending
"""

import argparse
import html
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

import tredict
from llm import ask_groq
from telegram import tg_send

TZ = ZoneInfo("Europe/Madrid")

GOAL = "марафон быстрее 3:00 (темп 4:15/км)"

# Classification of past sessions, calibrated on Aug-Oct 2026 runs: easy runs
# give <=108 Tredict effort per hour, interval sessions >=120. Effort alone
# grows with duration, so a long easy run is not "hard".
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


# ── Data ─────────────────────────────────────────────────────────────────────

def ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def by_day(records: dict) -> dict[date, list]:
    return {datetime.strptime(k, "%Y%m%d").date(): v for k, v in records.items()}


def local_date(ts: str, offset_s: int | None = None) -> date:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if offset_s is not None:
        return (dt + timedelta(seconds=offset_s)).date()
    return dt.astimezone(TZ).date()


def fetch_activities() -> list[dict]:
    """Last ~200 activities (all sports), oldest first."""
    data = tredict.get("activityList", pageSize=200, extendedSummary=1)
    acts = []
    for a in data["_embedded"]["activityList"]:
        s = a.get("summary") or {}
        acts.append({
            "date":  local_date(a["date"]),
            "sport": a.get("sportType"),
            "title": a.get("title") or "",
            "km":    (s.get("distance") or 0) / 1000,
            "min":   (s.get("duration") or 0) / 60,
            "pace":  s.get("pace"),          # s/km
            "hr":    s.get("heartrate"),
        })
    return sorted(acts, key=lambda a: a["date"])


def fetch_resting_hr() -> dict[date, int]:
    data = tredict.get("bodyvalues")["bodyvalues"]
    return {
        local_date(v["timestamp"], v.get("timezoneOffsetInSeconds")): v["hrRestDynamic"]
        for v in data if v.get("hrRestDynamic")
    }


def hr_zones() -> dict:
    """Running HR zones from Tredict; falls back to LT-based zones if the layout is unexpected."""
    zones = tredict.get("zones", sportType="running")["zones"]["running"]["heartrate"]
    latest = zones[max(zones)]
    if len(latest) == 5:
        z = latest
        return {"recovery_max": z[0]["to"], "easy": (z[1]["from"], z[1]["to"]),
                "steady": (z[2]["from"], z[2]["to"]), "threshold": (z[3]["from"], z[3]["to"]),
                "vo2_min": z[4]["from"]}
    lt = tredict.get("capacity", sportType="running")["capacity"]["running"][-1]["hrLth"]
    return {"recovery_max": round(lt * .78), "easy": (round(lt * .79), round(lt * .89)),
            "steady": (round(lt * .90), round(lt * .95)), "threshold": (round(lt * .96), lt),
            "vo2_min": lt + 1}


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


def is_hard(run: dict, effort: float) -> bool:
    per_hour = effort / (run["min"] / 60) if run["min"] else 0
    title = run["title"].replace("​", "")   # Garmin names may contain zero-width spaces
    return per_hour >= HARD_EFFORT_PER_H or (run["hr"] or 0) >= HARD_AVG_HR or bool(HARD_TITLE.search(title))


def is_long(run: dict) -> bool:
    return run["km"] >= LONG_KM or run["min"] >= LONG_MIN


def fmt_hm(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} ч {m:02d} мин"


def fmt_pace(s: float | None) -> str:
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}/км"


def assess(today: date, hrv: dict, sleep: dict, rhr: dict, efforts: dict, runs: list) -> tuple[Readiness, list[str]]:
    """Returns readiness and human-readable metric lines (already HTML-safe)."""
    r = Readiness()
    lines = []
    past = [x for x in runs if x["date"] < today]
    warnings = 0   # physiological under-recovery signals; two together mean rest

    # Overnight HRV vs personal baseline (single nights are noisy → also 3-day mean)
    if today in hrv:
        rmssd, base = hrv[today]
        ratios = [hrv[d][0] / hrv[d][1] for d in (today - timedelta(i) for i in range(3))
                  if d in hrv and hrv[d][1]]
        r3 = mean(ratios)
        lines.append(f"ВСР: {rmssd} мс (норма {base})")
        # A crash (<60%) is red on its own: good nights before it must not mask it
        if rmssd / base < 0.6 or (rmssd / base < 0.75 and r3 < 0.9):
            r.cap("red", f"ВСР {rmssd} мс — сильно ниже нормы {base}")
        elif rmssd / base < 0.85 or r3 < 0.9:
            r.cap("yellow", f"ВСР ниже нормы ({rmssd} при норме {base})")
            warnings += 1
    else:
        lines.append("ВСР: нет данных за ночь")
        r.cap("yellow", "нет данных ВСР за ночь — синхронизируй часы")

    if today in sleep:
        sec, base = sleep[today]
        lines.append(f"Сон: {fmt_hm(sec)} (норма {fmt_hm(base)})")
        if sec < 5 * 3600:
            r.cap("red", f"сон всего {fmt_hm(sec)}")
        elif sec < 6.5 * 3600 or sec < 0.85 * base:
            r.cap("yellow", f"сон короче нормы ({fmt_hm(sec)})")
            warnings += 1
    else:
        lines.append("Сон: нет данных за ночь")
        r.cap("yellow", "нет данных сна — синхронизируй часы")

    if today in rhr:
        prev = [rhr[today - timedelta(i)] for i in range(1, 15) if today - timedelta(i) in rhr]
        if prev:
            norm = mean(prev)
            lines.append(f"Пульс покоя: {rhr[today]} (норма {norm:.0f})")
            if rhr[today] - norm >= 8:
                r.cap("red", f"пульс покоя выше нормы на {rhr[today] - norm:.0f}")
            elif rhr[today] - norm >= 5:
                r.cap("yellow", f"пульс покоя выше нормы на {rhr[today] - norm:.0f}")
                warnings += 1
    if warnings >= 2:
        r.cap("red", "несколько признаков недовосстановления сразу")

    # Acute:chronic training load (Tredict effort, all sports)
    def load(days: int) -> float:
        return sum(efforts.get(today - timedelta(i), 0) for i in range(1, days + 1))
    acute, chronic = load(7), load(28) / 4
    if chronic:
        acwr = acute / chronic
        lines.append(f"Нагрузка неделя/месяц: {acwr:.2f}")
        # A ramp is an injury risk from intensity first: cut quality, keep easy volume
        if acwr > 1.8:
            r.cap("yellow", f"нагрузка за неделю резко выросла ({acwr:.2f})")
        elif acwr > 1.5:
            r.no_quality(f"нагрузка за неделю быстро растёт ({acwr:.2f})")

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
        r.long_ok = False

    # Session spacing
    # Long runs are their own weekly key session, not one of the 2 quality ones
    hard = [x for x in past if (today - x["date"]).days <= 7
            and is_hard(x, efforts.get(x["date"], 0)) and not is_long(x)]
    yesterday = [x for x in past if x["date"] == today - timedelta(1)]
    if any(is_hard(x, efforts.get(x["date"], 0)) or is_long(x) for x in yesterday):
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
    return r, [html.escape(x, quote=False) for x in lines]


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


def build_prompt(today: date, r: Readiness, metrics: list[str], z: dict, acts: list, efforts: dict) -> str:
    recent = [a for a in acts if (today - a["date"]).days <= 14 and a["date"] <= today]
    history = "\n".join(
        f"- {WEEKDAYS_SHORT[a['date'].weekday()]} {a['date']:%d.%m}: {a['sport']}, {a['km']:.1f} км, "
        f"{a['min']:.0f} мин, темп {fmt_pace(a['pace'])}, пульс {a['hr'] or '—'}, "
        f"нагрузка {efforts.get(a['date'], 0):.0f}, «{a['title']}»"
        for a in recent
    ) or "- нет тренировок"
    runs = [a for a in acts if a["sport"] == "running"]
    marathons = [a for a in runs if a["km"] >= 42 and (today - a["date"]).days <= 365]
    best = min(marathons, key=lambda a: a["min"]) if marathons else None
    best_txt = f"{int(best['min'] // 60)}:{int(best['min'] % 60):02d} ({best['date']:%d.%m.%Y})" if best else "нет данных"
    today_done = [a for a in recent if a["date"] == today]
    options = "\n".join(f"{i}. {o}" for i, o in enumerate(allowed_sessions(r, z), 1))

    return f"""Ты тренер по бегу. Составь тренировку на сегодня ({WEEKDAYS[today.weekday()]}, {today:%d.%m.%Y}).

Долгосрочная цель бегуна: {GOAL}. Лучший марафон за год: {best_txt}.
Это многолетняя цель: тренировки строй от ТЕКУЩЕГО уровня и пульсовых зон, а не от целевого темпа 4:15/км.
Принципы: восстановление в приоритете; ~80% объёма легко; не больше 2 интенсивных тренировок в неделю;
прирост недельного объёма не больше 10%.
Расписание недели: суббота — выходной, воскресенье — длительный бег, ключевые тренировки лучше во вторник и четверг.

Состояние после сна (решение по готовности уже принято правилами, НЕ повышай интенсивность):
{chr(10).join('- ' + m for m in metrics)}
Готовность: {r.level}. Причины ограничений: {'; '.join(r.reasons) or 'нет'}.

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


def build_message(today: date, r: Readiness, metrics: list[str], plan: str) -> str:
    reasons = "".join(f"\n• {html.escape(x, quote=False)}" for x in r.reasons)
    return "\n".join([
        f"🌅 <b>План на {WEEKDAYS_ACC[today.weekday()]}, {today.day} {MONTHS_GEN[today.month - 1]}</b>",
        "",
        f"Готовность: {'😴 <b>выходной</b>' if r.day_off else LEVEL_LABEL[r.level]}",
        *(f"• {m}" for m in metrics),
        *([f"\n<b>Ограничения:</b>{reasons}"] if r.reasons else []),
        "",
        "🏃 <b>Тренировка</b>",
        html.escape("\n".join(line.rstrip() for line in plan.splitlines()), quote=False),
        "",
        "<i>Боль, недомогание или пульс выше обычного на разминке — снижай нагрузку или отдыхай.</i>",
    ])


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--date", type=date.fromisoformat, help="plan for this date (testing)")
    args = parser.parse_args()

    today = args.date or datetime.now(TZ).date()
    hrv = by_day(tredict.get("hrv")["hrv"])
    sleep = by_day(tredict.get("sleep")["sleep"])
    efforts = {d: sum(e[0] for e in v) for d, v in by_day(tredict.get("efforts")["trainingEfforts"]).items()}
    rhr = fetch_resting_hr()
    acts = fetch_activities()
    zones = hr_zones()

    runs = [a for a in acts if a["sport"] == "running"]
    readiness, metrics = assess(today, hrv, sleep, rhr, efforts, runs)

    try:
        plan = ask_groq(build_prompt(today, readiness, metrics, zones, acts, efforts), max_tokens=3000)
    except RuntimeError as exc:
        # The rules already decided what is safe; send that rather than nothing
        plan = f"⚠️ {exc}. Рекомендация по правилам:\n{allowed_sessions(readiness, zones)[0]}"

    message = build_message(today, readiness, metrics, plan)
    if args.dry_run:
        print(message)
        return
    if not tg_send(message):
        sys.exit("Failed to send plan to Telegram")
    print("Plan sent.")


if __name__ == "__main__":
    main()
