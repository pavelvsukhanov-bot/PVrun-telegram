"""
Weekly check of the marathon build against checkpoints; silent while on track.

Three markers of a marathon build, each judged on what was run and on what
the Garmin plan has for the coming week (lagging only if both fall short, so a
plan that is about to catch up doesn't raise an alarm):
- volume: average of the last 3 full weeks (Mon–Sun) — race weeks and the
  weeks either side are left out, a taper or a recovery week is not a lag;
- long run: the longest run of the last 14 days;
- marathon-pace work, from the build phase on: minutes of continuous running
  (≥5 min) at goal marathon pace ±4%, grade-adjusted, over the last 14 days.
Targets are interpolated between the checkpoints below. No check around a race
(±7 days) or in the taper.

Run weekly by .github/workflows/plan_check.yml.
"""

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from html import escape as esc
from statistics import mean

from dotenv import load_dotenv
from garminconnect.activity_details import parse_activity_detail_metrics

from garmin_sync import is_run
from plan_sources import PHASE_RU, garmin_client, garmin_races, local_today
from telegram import tg_send

load_dotenv()

RACE_NAME = "Милан"
RACE_DAY = date(2027, 4, 4)
GOAL_S = 3 * 3600 + 45 * 60
MP = 42195 / GOAL_S                    # m/s, goal marathon pace (≈5:20/km)

# (date, weekly km, long run km); flat after the last one, no check in the taper
CHECKPOINTS = [
    (date(2026, 10, 7), 45, 15),       # plan start
    (date(2026, 12, 6), 58, 21),       # end of base
    (date(2027, 1, 17), 63, 26),       # mid build
    (date(2027, 2, 14), 67, 29),
    (date(2027, 3, 7), 70, 32),        # peak
]
MP_FROM = date(2026, 12, 21)           # two weeks into the build phase
TAPER_FROM = date(2027, 3, 22)

VOLUME_TOL = 0.85                      # weekly km below 85% of the target is lagging
LONG_TOL_KM = 2
MP_MIN = 20                            # minutes at marathon pace per 14 days
MP_TOL = 0.04
MP_REP_S = 5 * 60
MP_WORDS = re.compile(r"марафон|marathon|race pace|темп гонки", re.I)


@dataclass
class Week:
    start: date
    km: float


def fmt_pace(v: float) -> str:
    s = round(1000 / v)
    return f"{s // 60}:{s % 60:02d}"


def targets(today: date) -> tuple[float, float]:
    """Weekly km and long-run km expected by this date."""
    if today <= CHECKPOINTS[0][0]:
        return CHECKPOINTS[0][1:]
    for (d0, v0, l0), (d1, v1, l1) in zip(CHECKPOINTS, CHECKPOINTS[1:]):
        if today <= d1:
            k = (today - d0).days / (d1 - d0).days
            return v0 + k * (v1 - v0), l0 + k * (l1 - l0)
    return CHECKPOINTS[-1][1:]


def mp_seconds(samples: list[dict]) -> float:
    """Seconds in continuous ≥5-min stretches at marathon pace (1-minute buckets, grade-adjusted)."""
    buckets: dict[int, list] = {}
    for p in samples:
        v = p.get("directGradeAdjustedSpeed") or p.get("directSpeed")
        if v:
            buckets.setdefault(int((p.get("sumMovingDuration") or 0) // 60), []).append(v)
    total, run, prev = 0, 0, None
    for k, vs in sorted(buckets.items()):
        at_mp = abs(mean(vs) / MP - 1) <= MP_TOL
        if at_mp and run and k == prev + 1:
            run += 60
        else:
            total += run if run >= MP_REP_S else 0
            run = 60 if at_mp else 0
        prev = k
    return total + (run if run >= MP_REP_S else 0)


def upcoming(c, today: date) -> tuple[str, list[dict]]:
    """Plan phase label and the plan's sessions for the next 7 days (Garmin taskList)."""
    plans = [p for p in (c.get_training_plans() or {}).get("trainingPlanList", [])
             if p.get("trainingPlanCategory") == "FBT_ADAPTIVE"
             and (p.get("trainingStatus") or {}).get("statusKey") != "Completed"]
    if not plans:
        return "", []
    plan = c.get_adaptive_training_plan_by_id(max(plans, key=lambda p: p.get("createDate") or "")["trainingPlanId"])
    d = today.isoformat()
    phase = next((ph.get("trainingPhase", "") for ph in plan.get("adaptivePlanPhases") or []
                  if ph.get("startDate", "") <= d <= ph.get("endDate", "")), "")
    week = (today - date.fromisoformat(plan["startDate"][:10])).days // 7 + 1
    info = f"{PHASE_RU.get(phase, phase.lower())}, нед. {week}/{plan.get('durationInWeeks')}"
    end = (today + timedelta(days=6)).isoformat()
    sessions = []
    for t in plan.get("taskList") or []:
        w = t.get("taskWorkout") or {}
        if not d <= (t.get("calendarDate") or "") <= end or w.get("restDay") or not w.get("workoutName"):
            continue
        if (w.get("sportType") or {}).get("sportTypeKey", "running") != "running":
            continue
        sessions.append({
            "name": w["workoutName"],
            "km":   (w.get("estimatedDistanceInMeters") or 0) / 1000 or None,
            "long": bool(t.get("longWkt")),
            "mp":   bool(MP_WORDS.search(f"{w['workoutName']} {w.get('workoutDescription') or ''}")),
        })
    return info, sessions


def check(c, today: date, verbose: bool = False) -> list[str] | None:
    """Lagging items as message lines ([] = on track); None when no check applies this week."""
    if not CHECKPOINTS[0][0] <= today < TAPER_FROM:
        return None
    races = garmin_races(c, today, back=63, ahead=7)
    if any(abs((r["date"] - today).days) <= 7 for r in races):
        print("Race within a week: no check.")
        return None

    monday = today - timedelta(days=today.weekday())
    acts = [a for a in c.get_activities_by_date((monday - timedelta(weeks=8)).isoformat(), today.isoformat())
            if is_run(a)]
    day = lambda a: date.fromisoformat(a["startTimeLocal"][:10])

    # Weeks around a race (taper, race week, recovery) say nothing about the build
    race_weeks = {r["date"] - timedelta(days=r["date"].weekday()) + timedelta(weeks=k)
                  for r in races for k in (-1, 0, 1)}
    weeks = [Week(m, sum((a.get("distance") or 0) for a in acts if m <= day(a) < m + timedelta(7)) / 1000)
             for m in (monday - timedelta(weeks=k) for k in range(1, 9)) if m not in race_weeks][:3]
    recent = [a for a in acts if day(a) >= today - timedelta(days=14)]
    longest = max(((a.get("distance") or 0) / 1000 for a in recent), default=0)

    info, plan = upcoming(c, today)
    plan_km = sum(s["km"] for s in plan) if plan and all(s["km"] for s in plan) else None
    plan_long = max((s["km"] or 0 for s in plan if s["long"]), default=0)

    vol_target, long_target = targets(today)
    lines = []
    if len(weeks) >= 2:
        done = mean(w.km for w in weeks)
        if done < VOLUME_TOL * vol_target and (plan_km is None or plan_km < VOLUME_TOL * vol_target):
            planned = f", в плане на неделю {plan_km:.0f}" if plan_km is not None else ""
            lines.append(f"• <b>Объём</b>: {done:.0f} км/нед за {len(weeks)} нед.{planned} — ориентир сейчас "
                         f"{vol_target:.0f}. Прибавляй до 10% в неделю: лишняя лёгкая пробежка или +10–15 мин к лёгким.")
    if longest < long_target - LONG_TOL_KM and plan_long < long_target - LONG_TOL_KM:
        planned = f", в плане {plan_long:.0f}" if plan_long else ""
        lines.append(f"• <b>Длительный</b>: самый длинный за 2 недели {longest:.0f} км{planned} — ориентир "
                     f"{long_target:.0f} км. Удлиняй воскресный на 1–2 км в неделю, легко.")

    mp_min = None
    if today >= MP_FROM:
        mp_s = 0
        for a in recent:
            # A run that never reached marathon pace has nothing to count: skip its download
            if (a.get("maxSpeed") or 0) < MP * (1 - MP_TOL) or (a.get("duration") or 0) < MP_REP_S:
                continue
            try:
                mp_s += mp_seconds(parse_activity_detail_metrics(c.get_activity_details(str(a["activityId"]))))
            except Exception as exc:
                print(f"Plan check: details unavailable for {a['activityId']}: {exc!r}")
        mp_min = mp_s / 60
        if mp_min < MP_MIN and not any(s["mp"] for s in plan):
            lines.append(f"• <b>Марафонский темп</b> {fmt_pace(MP)}/км: за 2 недели {mp_min:.0f} мин, нужно "
                         f"≥{MP_MIN}. Добавь в длительный 2–3 отрезка по 10–15 мин в этом темпе.")

    if verbose:
        print(f"{info or 'no plan'} | targets {vol_target:.1f} km/wk, long {long_target:.1f} km")
        print("weeks: " + ", ".join(f"{w.start:%d.%m} {w.km:.1f}" for w in weeks))
        print(f"longest 14 d: {longest:.1f} | plan next 7 d: {plan_km} km, long {plan_long} | "
              f"MP min: {mp_min if mp_min is None else round(mp_min)}")
        for s in plan:
            print(f"  plan: {s}")
    if lines:
        lines.insert(0, f"📉 <b>Подготовка к марафону ({esc(RACE_NAME)}) отстаёт</b>" + (f" — {esc(info)}" if info else ""))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    parser.add_argument("--date", type=date.fromisoformat, help="check as of this date (testing)")
    args = parser.parse_args()

    lines = check(garmin_client(), args.date or local_today(), verbose=args.dry_run)
    if not lines:
        print("On track: nothing to send." if lines == [] else "No check this week.")
        return
    text = "\n".join(lines)
    if args.dry_run:
        print(text)
    elif not tg_send(text):
        sys.exit("Failed to send plan check to Telegram")
    else:
        print("Lag reported.")


if __name__ == "__main__":
    main()
