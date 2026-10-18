"""
Monthly running report with AI analysis and marathon recommendations.
Sent to Telegram on the 1st of each month at 8:00 AM.

Usage:
    python monthly_report.py            # report for previous month
    python monthly_report.py --test     # test with current month's data
"""

import argparse
import html
import sys
from dataclasses import dataclass
from datetime import date, timedelta

from dotenv import load_dotenv

import cardiac_drift
from db import get_conn
from llm import ask_groq
from telegram import tg_send

load_dotenv()

MONTHS_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
MONTHS_RU_GEN = {
    1: "январь", 2: "февраль", 3: "март", 4: "апрель",
    5: "май", 6: "июнь", 7: "июль", 8: "август",
    9: "сентябрь", 10: "октябрь", 11: "ноябрь", 12: "декабрь",
}


# ── Date helpers ─────────────────────────────────────────────────────────────

def get_month_range(test_mode: bool) -> tuple[date, date, int, int]:
    today = date.today()
    if test_mode:
        first = today.replace(day=1)
        last = today
        return first, last, today.month, today.year
    # Previous month
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    first_prev = last_prev.replace(day=1)
    return first_prev, last_prev, last_prev.month, last_prev.year


# ── DB queries ───────────────────────────────────────────────────────────────

def fetch_month_runs(start: date, end: date) -> list:
    with get_conn() as conn:
        return conn.execute("""
            SELECT * FROM runs
            WHERE DATE(start_time) >= ? AND DATE(start_time) <= ?
            ORDER BY start_time
        """, (start.isoformat(), end.isoformat())).fetchall()


def avg(values: list) -> float | None:
    clean = [v for v in values if v is not None]
    return sum(clean) / len(clean) if clean else None


def compute_stats(rows) -> dict | None:
    if not rows:
        return None

    dists    = [r["distance_m"] or 0 for r in rows]
    paces    = [r["avg_pace_s_km"] for r in rows]
    hrs      = [r["avg_hr"] for r in rows]
    max_hrs  = [r["max_hr"] for r in rows]
    cals     = [r["calories"] or 0 for r in rows]
    cadences = [r["avg_cadence"] for r in rows]
    gcts     = [r["avg_gct_ms"] for r in rows]
    strides  = [r["avg_stride_length_cm"] for r in rows]
    v_oscs   = [r["avg_vert_osc_cm"] for r in rows]
    v_rats   = [r["avg_vert_ratio_pct"] for r in rows]

    sorted_by_pace = sorted(p for p in paces if p)

    return {
        "total_runs":    len(rows),
        "total_km":      sum(dists) / 1000,
        "max_km":        max(dists) / 1000,
        "avg_pace_s":    avg(paces),
        "best_pace_s":   sorted_by_pace[0] if sorted_by_pace else None,
        "avg_hr":        avg(hrs),
        "max_hr":        max([h for h in max_hrs if h], default=None),
        "total_cal":     sum(cals),
        "avg_cadence":   avg(cadences),
        "avg_gct":       avg(gcts),
        "avg_stride":    avg(strides),
        "avg_vert_osc":  avg(v_oscs),
        "avg_vert_ratio": avg(v_rats),
        "runs":          [dict(r) for r in rows],
    }


# ── Formatting helpers ────────────────────────────────────────────────────────

def fmt_pace(s: float | None) -> str:
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d} /км"


def fmt_val(v, unit="", decimals=1) -> str:
    if v is None:
        return "—"
    return f"{v:.{decimals}f}{unit}"


def technique_emoji(metric: str, value: float | None) -> str:
    """Returns ✅ / ⚠️ / ❌ based on marathon training guidelines."""
    if value is None:
        return ""
    if metric == "cadence":  # higher is better
        return "✅" if value >= 170 else "⚠️" if value >= 160 else "❌"
    # lower is better: (ok below, warning below)
    limits = {"gct": (250, 280), "vert_osc": (8.0, 10.0), "vert_ratio": (8.0, 10.0)}
    if metric not in limits:
        return ""
    low, high = limits[metric]
    return "✅" if value < low else "⚠️" if value < high else "❌"


# ── AI analysis ───────────────────────────────────────────────────────────────

# ── Cardiac drift ─────────────────────────────────────────────────────────────

@dataclass
class DriftMonths:
    current: list           # [(date, title, Drift)] for the report month
    previous: list          # same for the month before
    prev_month: int

    @property
    def summary(self) -> dict | None:
        return cardiac_drift.summarize([d for _, _, d in self.current])

    @property
    def prev_summary(self) -> dict | None:
        return cardiac_drift.summarize([d for _, _, d in self.previous])


def collect_drift(start: date, end: date) -> DriftMonths:
    """Drift of every 30+ min run in the report month and the month before (from Garmin)."""
    from garmin_sync import login
    client = login()
    prev_start = (start - timedelta(days=1)).replace(day=1)
    current, previous = [], []
    for a in client.get_activities_by_date(prev_start.isoformat(), end.isoformat()):
        if "run" not in (a.get("activityType") or {}).get("typeKey", "") or (a.get("duration") or 0) < 30 * 60:
            continue
        day = date.fromisoformat(a["startTimeLocal"][:10])
        drift = cardiac_drift.for_activity(client, a["activityId"], a.get("activityName") or "", (a.get("distance") or 0) / 1000)
        if drift:
            (current if day >= start else previous).append((day, a.get("activityName") or "", drift))
    return DriftMonths(sorted(current, key=lambda x: x[0]), sorted(previous, key=lambda x: x[0]), prev_start.month)


def drift_block(dm: DriftMonths) -> list[str]:
    cur, prev = dm.summary, dm.prev_summary
    if not cur:
        return ["❤️ <b>Кардиодрифт</b>", "  Нет ровных пробежек от 30 мин для расчёта."]
    prev_name = MONTHS_RU_GEN[dm.prev_month]
    trend = ""
    if prev:
        delta = cur["decoupling"] - prev["decoupling"]
        trend = f" ({prev_name}: {prev['decoupling']:.1f}%, {'↓ лучше' if delta < -0.5 else '↑ хуже' if delta > 0.5 else '≈ без изменений'})"
    lines = [
        "❤️ <b>Кардиодрифт</b> (ровные пробежки)",
        f"  Pa:HR медиана:     {cur['decoupling']:.1f}% {cardiac_drift.grade(cur['decoupling'])}{trend}",
        f"  Пульс 2-й половины: {cur['hr_change']:+.1f}%" + (f" ({prev_name}: {prev['hr_change']:+.1f}%)" if prev else ""),
    ]
    if cur["long_decoupling"] is not None:
        lines.append(f"  Длительные 75+ мин: {cur['long_decoupling']:.1f}% (n={cur['long_n']})")
    temp = f" · ср. {cur['temp_c']:.0f}°C" if cur["temp_c"] is not None else ""
    prev_temp = f" ({prev_name}: {prev['temp_c']:.0f}°C)" if prev and prev["temp_c"] is not None else ""
    lines.append(f"  В расчёте: {cur['n']} пробежек{temp}{prev_temp}")
    return lines


def drift_prompt(dm: DriftMonths | None) -> str:
    if not dm or not dm.summary:
        return "Кардиодрифт: нет данных."
    def month_text(summary: dict | None, runs: list, name: str) -> str:
        if not summary:
            return f"{name}: нет ровных пробежек."
        per_run = "; ".join(f"{d:%d.%m} {x.minutes + 10:.0f} мин Pa:HR {x.decoupling:.1f}%"
                            + (f" {x.temp_c:.0f}°C" if x.temp_c is not None else "")
                            for d, _, x in runs if x.steady)
        long_runs = (f"{summary['long_decoupling']:.1f}% (n={summary['long_n']})"
                     if summary["long_decoupling"] is not None else "длительных 75+ мин в этом месяце не было")
        temp = f"{summary['temp_c']:.0f}°C" if summary["temp_c"] is not None else "—"
        return (f"{name}: медиана Pa:HR {summary['decoupling']:.1f}%, рост пульса во 2-й половине "
                f"{summary['hr_change']:+.1f}%, длительные {long_runs}, ср. температура {temp}. "
                f"По пробежкам: {per_run}")
    return ("Кардиодрифт (ровные лёгкие пробежки от 30 мин, без первых 10 мин; Pa:HR — падение скорости "
            "на удар пульса во 2-й половине, <5% — хорошая аэробная база):\n"
            f"- {month_text(dm.summary, dm.current, 'Этот месяц')}\n"
            f"- {month_text(dm.prev_summary, dm.previous, 'Прошлый месяц')}")


def generate_ai_analysis(stats: dict, month_name: str, drift: DriftMonths | None = None) -> str:
    prompt = f"""Ты опытный тренер по бегу. Проанализируй тренировочный месяц бегуна,
который готовится к марафону, и дай конкретные рекомендации.

Объём и тренировки задаёт адаптивный план Garmin к Milan Marathon 4 апреля 2027 (цель 3:59).
НЕ назначай свой недельный объём, длительные и интервалы — это делает план. Рекомендации — о технике,
восстановлении, жаре, кардиодрифте и о том, насколько последовательно выполняется план.

Данные за {month_name}:
- Пробежек: {stats['total_runs']}
- Итого км: {stats['total_km']:.1f} км за {stats['days']} дн. — в среднем {stats['weekly_km']:.0f} км в неделю
- Самая длинная: {stats['max_km']:.1f} км
- Средний темп: {fmt_pace(stats['avg_pace_s'])}
- Лучший темп: {fmt_pace(stats['best_pace_s'])}
- Средний пульс: {fmt_val(stats['avg_hr'], ' уд/мин', 0)}
- Максимальный пульс: {fmt_val(stats['max_hr'], ' уд/мин', 0)}

Техника бега (средние за месяц):
- Каденс: {fmt_val(stats['avg_cadence'], ' ш/мин', 0)} (норма для марафона: 170–185)
- ВКЗ (время контакта с землёй): {fmt_val(stats['avg_gct'], ' мс', 0)} (цель: <250 мс)
- Длина шага: {fmt_val(stats['avg_stride'], ' см', 0)}
- Вертикальное колебание: {fmt_val(stats['avg_vert_osc'], ' см')} (цель: <8 см)
- Вертикальное соотношение: {fmt_val(stats['avg_vert_ratio'], '%')} (цель: <8%)

{drift_prompt(drift)}

Напиши ответ строго в этом формате (без лишних заголовков):

АНАЛИЗ МЕСЯЦА
[2–3 предложения: объём, интенсивность, общая оценка готовности к марафону]

ТЕХНИКА БЕГА
[2–3 предложения: что хорошо, что требует внимания, конкретные наблюдения]

КАРДИОДРИФТ
[2–3 предложения: как изменился дрейф к прошлому месяцу и почему (объём, длительные, жара/влажность),
что это говорит об аэробной базе. Если данных нет — пропусти раздел]

РЕКОМЕНДАЦИИ НА СЛЕДУЮЩИЙ МЕСЯЦ
1. [конкретная рекомендация]
2. [конкретная рекомендация]
3. [конкретная рекомендация]
4. [конкретная рекомендация]

Отвечай на русском языке. Будь конкретным и практичным. Без markdown: никаких **, # и списков со звёздочками."""

    try:
        # Telegram shows markdown literally; the prompt forbids it, this is the safety net
        return ask_groq(prompt, max_tokens=2000).replace("**", "").replace("__", "")
    except RuntimeError as exc:
        return f"⚠️ {exc} — анализ недоступен."


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(stats: dict, month: int, year: int, drift: DriftMonths | None = None) -> str:
    month_name = f"{MONTHS_RU[month]} {year}"

    lines = [
        f"📊 <b>ОТЧЁТ ЗА {MONTHS_RU[month].upper()} {year}</b>",
        "",
        "🏃 <b>Объём</b>",
        f"  Пробежек:          {stats['total_runs']}",
        f"  Итого:             {stats['total_km']:.1f} км",
        f"  Самая длинная:     {stats['max_km']:.1f} км",
        f"  Лучший темп:       {fmt_pace(stats['best_pace_s'])}",
        "",
        "📈 <b>Средние показатели</b>",
        f"  Темп:              {fmt_pace(stats['avg_pace_s'])}",
        f"  Пульс ср/макс:     {fmt_val(stats['avg_hr'], '', 0)} / {fmt_val(stats['max_hr'], ' уд/мин', 0)}",
        f"  Калории итого:     {int(stats['total_cal'])} ккал",
        "",
        "⚙️ <b>Техника бега</b>",
        f"  Каденс:            {fmt_val(stats['avg_cadence'], ' ш/мин', 0)} {technique_emoji('cadence', stats['avg_cadence'])}",
        f"  ВКЗ:               {fmt_val(stats['avg_gct'], ' мс', 0)} {technique_emoji('gct', stats['avg_gct'])}",
        f"  Длина шага:        {fmt_val(stats['avg_stride'], ' см', 0)}",
        f"  Верт. колебание:   {fmt_val(stats['avg_vert_osc'], ' см')} {technique_emoji('vert_osc', stats['avg_vert_osc'])}",
        f"  Верт. соотношение: {fmt_val(stats['avg_vert_ratio'], '%')} {technique_emoji('vert_ratio', stats['avg_vert_ratio'])}",
        "",
        *(drift_block(drift) + [""] if drift else []),
        "🤖 <b>Анализ и рекомендации</b>",
    ]

    # Escape: model output may contain "<" (e.g. "<250 мс"), which breaks Telegram HTML parsing
    ai_text = generate_ai_analysis(stats, month_name, drift)
    lines.append(html.escape(ai_text, quote=False))

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                        help="Use current month data (for testing)")
    args = parser.parse_args()

    start, end, month, year = get_month_range(args.test)
    print(f"Generating report for {MONTHS_RU[month]} {year} ({start} - {end})...")

    rows = fetch_month_runs(start, end)
    if not rows:
        print("No runs found for this period.")
        if not tg_send(f"📊 Отчёт за {MONTHS_RU_GEN[month]} {year}: пробежек не найдено."):
            sys.exit("Failed to send report to Telegram")
        return

    stats = compute_stats(rows)
    stats["days"] = (end - start).days + 1
    stats["weekly_km"] = stats["total_km"] / stats["days"] * 7   # the LLM must not mistake the total for a week
    drift = None
    try:
        drift = collect_drift(start, end)
    except (Exception, SystemExit) as exc:   # SystemExit: Garmin auth failed — report without drift
        print(f"Cardiac drift unavailable: {exc!r}")
    report = build_report(stats, month, year, drift)

    print("Sending to Telegram...")
    if not tg_send(report):
        sys.exit("Failed to send report to Telegram")
    print("Done.")


if __name__ == "__main__":
    main()
