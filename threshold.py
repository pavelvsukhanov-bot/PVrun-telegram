"""
Is the lactate threshold set in Garmin (HR, pace, power) still right?

Checked against the athlete's own threshold work, and silent unless the
evidence is consistent:
- evidence = work intervals of the last LOOKBACK_DAYS: ≥5 min at ≥88% of
  threshold speed, measured after the first 2 min (HR settles); races and hot
  sessions (WBGT ≥ 23, heat inflates HR) are left out;
- each interval's grade-adjusted speed and power are projected to the
  threshold HR along the heart-rate reserve,
      x_at_LTHR = x × (LTHR − HRrest) / (HR − HRrest),
  only for intervals within 8 bpm of LTHR, so the projection stays short;
- a recommendation needs ≥3 intervals from ≥2 sessions, a median gap beyond
  the tolerance (pace 4%, power 5%) and ≥75% of intervals on the same side;
- threshold pace can't be typed into Garmin (it comes from its LT
  detection), so pace advice is to run the watch's lactate threshold test;
- threshold HR: a continuous ≥20 min interval held ≥3 bpm above LTHR means
  LTHR is set too low (the core of Friel's 30-minute field test).
"""

import os
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean, median

from garminconnect.activity_details import parse_activity_detail_metrics

LOOKBACK_DAYS = 42
REP_SPEED = 0.88        # work interval: at least this share of threshold speed
MIN_REP_S = 300         # shorter reps end before HR catches up: projections run high
SETTLE_S = 120
MAX_HR_GAP = 8
PACE_TOL = 0.04
POWER_TOL = 0.05
MIN_REPS, MIN_SESSIONS = 3, 2
AGREEMENT = 0.75        # share of intervals that must sit on the same side of the setting
HOT_WBGT = 23
RACE_KM = 30


@dataclass
class Threshold:
    hr: int
    speed: float            # m/s
    power: float | None     # W
    hr_auto: bool           # Garmin updates LTHR itself
    hr_version: str         # changes when HR/pace are re-set (they change together)
    power_version: str      # changes when power is re-set


@dataclass
class Rep:
    seconds: float
    speed: float
    hr: float
    power: float | None


def fmt_pace(v: float) -> str:
    s = round(1000 / v)
    return f"{s // 60}:{s % 60:02d}"


def current(client) -> Threshold | None:
    lt = client.get_lactate_threshold() or {}
    sh, pw = lt.get("speed_and_heart_rate") or {}, lt.get("power") or {}
    if not sh.get("heartRate") or not sh.get("speed"):
        return None
    profile = (client.get_user_profile() or {}).get("userData", {})
    return Threshold(
        hr=sh["heartRate"],
        speed=sh["speed"] * 10,     # Garmin stores m/s divided by 10 (0.3167 → 3.17 m/s = 5:16/km)
        power=pw.get("functionalThresholdPower"),
        hr_auto=bool(profile.get("thresholdHeartRateAutoDetected")),
        hr_version=str(sh.get("version")),
        power_version=str(pw.get("ftpCreateTime")),
    )


def work_reps(samples: list[dict], lt: Threshold) -> list[Rep]:
    """Work intervals from 30-second buckets of grade-adjusted speed."""
    buckets: dict[int, list] = {}
    for p in samples:
        v = p.get("directGradeAdjustedSpeed") or p.get("directSpeed")
        if p.get("directHeartRate") and v:
            buckets.setdefault(int((p.get("sumMovingDuration") or 0) // 30), []).append(
                (p["directHeartRate"], v, p.get("directPower")))
    rows = [(k, mean(x[0] for x in r), mean(x[1] for x in r),
             mean(x[2] for x in r if x[2]) if any(x[2] for x in r) else None)
            for k, r in sorted(buckets.items())]
    reps, cur = [], []
    for row in rows + [(None, 0, 0, None)]:          # sentinel closes the last interval
        if row[0] is not None and row[2] >= REP_SPEED * lt.speed and (not cur or row[0] == cur[-1][0] + 1):
            cur.append(row)
            continue
        if len(cur) * 30 >= MIN_REP_S:
            settled = cur[SETTLE_S // 30:]
            powers = [x[3] for x in settled if x[3]]
            reps.append(Rep(len(cur) * 30, mean(x[2] for x in cur), mean(x[1] for x in settled),
                            mean(powers) if powers else None))
        cur = [row] if row[0] is not None and row[2] >= REP_SPEED * lt.speed else []
    return reps


@dataclass
class Finding:
    items: list[tuple[str, str]]   # (key, text); the key changes with the LT setting, so advice can return

    @property
    def lines(self) -> list[str]:
        return [text for _, text in self.items]


def check(client, today: date, lt: Threshold, rest_hr: float, wbgt_by_id: dict | None = None) -> Finding:
    wbgt_by_id = wbgt_by_id or {}
    speeds, powers, sessions, long_above = [], [], set(), []
    start = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    for a in client.get_activities_by_date(start, today.isoformat()):
        if "run" not in (a.get("activityType") or {}).get("typeKey", "") or (a.get("distance") or 0) / 1000 >= RACE_KM:
            continue
        if (wbgt_by_id.get(str(a["activityId"])) or 0) >= HOT_WBGT:
            continue
        if (a.get("maxHR") or 0) < lt.hr - MAX_HR_GAP:
            continue    # never got near threshold: skip the detail download
        try:
            reps = work_reps(parse_activity_detail_metrics(client.get_activity_details(str(a["activityId"]))), lt)
        except Exception as exc:
            print(f"Threshold check: details unavailable for {a['activityId']}: {exc!r}")
            continue
        for r in reps:
            if r.seconds >= 20 * 60 and r.hr >= lt.hr + 3:
                long_above.append(r)
            if abs(r.hr - lt.hr) > MAX_HR_GAP or r.hr <= rest_hr:
                continue
            k = (lt.hr - rest_hr) / (r.hr - rest_hr)
            speeds.append(r.speed * k)
            if r.power:
                powers.append(r.power * k)
            sessions.add(a["activityId"])

    items = []      # (advice key, text)
    enough = len(speeds) >= MIN_REPS and len(sessions) >= MIN_SESSIONS
    basis = f"{len(speeds)} рабочих отрезков из {len(sessions)} тренировок за {LOOKBACK_DAYS // 7} недель"
    if enough:
        v = median(speeds)
        faster = v > lt.speed
        if abs(v / lt.speed - 1) > PACE_TOL and _agree(speeds, lt.speed, faster):
            # Garmin derives threshold pace itself (it can't be typed in): only its LT test updates it
            items.append((f"pace:{'up' if faster else 'down'}",
                          f"Темп ПАНО: на пульсе {lt.hr} ты бежишь ≈{fmt_pace(v)}/км ({basis}), "
                          f"а Garmin считает пороговым {fmt_pace(lt.speed)}/км. Вручную темп не меняется — "
                          f"пройди на часах тест на лактатный порог: он обновит и пульс, и темп."))
        if lt.power and len(powers) >= MIN_REPS:
            p = median(powers)
            higher = p > lt.power
            if abs(p / lt.power - 1) > POWER_TOL and _agree(powers, lt.power, higher):
                items.append((f"power:{'up' if higher else 'down'}",
                              f"Мощность ПАНО: на пульсе {lt.hr} выходит ≈{p:.0f} Вт, а в Garmin стоит "
                              f"{lt.power:.0f} Вт — рассмотри {'повысить' if higher else 'понизить'} пороговую мощность."))
    if long_above:
        r = max(long_above, key=lambda r: r.hr)
        auto = " (у тебя включено автоопределение — Garmin может обновить его сам)" if lt.hr_auto else ""
        items.append(("hr:up", f"Пульс ПАНО: {r.seconds / 60:.0f} мин непрерывно с пульсом {r.hr:.0f} при пороговом "
                               f"{lt.hr} — рассмотри повысить пульс ПАНО{auto}."))
    # Advice is keyed to the setting it is about: re-setting power doesn't repeat the pace advice
    return Finding([(f"{lt.power_version if k.startswith('power') else lt.hr_version}:{k}", text)
                    for k, text in items])


def _agree(values: list[float], setting: float, above: bool) -> bool:
    """Most intervals point the same way: a median pulled by a few outliers is not evidence."""
    same_side = sum((x > setting) == above for x in values)
    return same_side >= AGREEMENT * len(values)


def review(client, today: date) -> Finding | None:
    """Threshold check with everything it needs fetched: current LT, resting HR, heat per run."""
    lt = current(client)
    if not lt:
        return None
    rest = (client.get_stats(today.isoformat()) or {}).get("lastSevenDaysAvgRestingHeartRate") or 50
    wbgt = {}
    if os.environ.get("ARCHIVE_URL"):
        try:
            import archive
            wbgt = {str(r["ID"]): float(r["WBGT °C"])
                    for r in archive.fetch(today - timedelta(days=LOOKBACK_DAYS), today)
                    if r.get("WBGT °C") not in ("", None)}
        except Exception as exc:
            print(f"Threshold check: archive unavailable: {exc!r}")
    return check(client, today, lt, rest, wbgt)
