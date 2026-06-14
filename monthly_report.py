"""
Monthly running report with AI analysis and marathon recommendations.
Sent to Telegram on the 1st of each month at 8:00 AM.

Usage:
    python monthly_report.py            # report for previous month
    python monthly_report.py --test     # test with current month's data
"""

import argparse
import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

from db import get_conn
from tg_poller import tg_send

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

    sorted_by_pace = sorted([p for p in paces if p], )

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
    thresholds = {
        "cadence":    [(170, "✅"), (160, "⚠️"), (0, "❌")],
        "gct":        [(0, "✅"), (250, "⚠️"), (280, "❌")],   # lower is better
        "vert_osc":   [(0, "✅"), (8.0, "⚠️"), (10.0, "❌")],  # lower is better
        "vert_ratio": [(0, "✅"), (8.0, "⚠️"), (10.0, "❌")],  # lower is better
    }
    if metric == "cadence":
        for threshold, emoji in thresholds["cadence"]:
            if value >= threshold:
                return emoji
    elif metric in ("gct", "vert_osc", "vert_ratio"):
        # lower is better: reverse logic
        limits = {"gct": (250, 280), "vert_osc": (8.0, 10.0), "vert_ratio": (8.0, 10.0)}
        low, high = limits[metric]
        if value < low:
            return "✅"
        elif value < high:
            return "⚠️"
        else:
            return "❌"
    return ""


# ── AI analysis ───────────────────────────────────────────────────────────────

def generate_ai_analysis(stats: dict, month_name: str) -> str:
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return "⚠️ GROQ_API_KEY не задан — анализ недоступен."

    prompt = f"""Ты опытный тренер по бегу. Проанализируй тренировочный месяц бегуна,
который готовится к марафону, и дай конкретные рекомендации.

Данные за {month_name}:
- Пробежек: {stats['total_runs']}
- Итого км: {stats['total_km']:.1f} км
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

Напиши ответ строго в этом формате (без лишних заголовков):

АНАЛИЗ МЕСЯЦА
[2–3 предложения: объём, интенсивность, общая оценка готовности к марафону]

ТЕХНИКА БЕГА
[2–3 предложения: что хорошо, что требует внимания, конкретные наблюдения]

РЕКОМЕНДАЦИИ НА СЛЕДУЮЩИЙ МЕСЯЦ
1. [конкретная рекомендация]
2. [конкретная рекомендация]
3. [конкретная рекомендация]
4. [конкретная рекомендация]

Отвечай на русском языке. Будь конкретным и практичным."""

    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": "llama-3.3-70b-versatile",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 700,
        },
        timeout=30,
    )
    if not resp.ok:
        return f"⚠️ Groq API недоступен ({resp.status_code}): {resp.text[:150]}"
    return resp.json()["choices"][0]["message"]["content"].strip()


# ── Report builder ────────────────────────────────────────────────────────────

def build_report(stats: dict, month: int, year: int) -> str:
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
        "🤖 <b>Анализ и рекомендации</b>",
    ]

    ai_text = generate_ai_analysis(stats, month_name)
    lines.append(ai_text)

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
        tg_send(f"📊 Отчёт за {MONTHS_RU_GEN[month]} {year}: пробежек не найдено.")
        return

    stats = compute_stats(rows)
    report = build_report(stats, month, year)

    print("Sending to Telegram...")
    tg_send(report)
    print("Done.")


if __name__ == "__main__":
    main()
