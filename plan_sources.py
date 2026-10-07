"""
Recovery data for the training plan, normalized into one Snapshot.

Garmin is the primary source (training readiness, sleep score and stages, HRV
status with its personal range, resting HR, VO2max, race predictions). Tredict
is the fallback: its official API is stable, but it exposes only a subset.

Each source judges its own recovery signals (thresholds differ: Garmin gives a
personal HRV range, Tredict a single baseline); the training rules in
daily_plan.py are source-independent.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

import tredict

TZ = ZoneInfo("Europe/Madrid")

READINESS_RU = {"PRIME": "отличная", "HIGH": "высокая", "MODERATE": "средняя", "LOW": "низкая", "POOR": "очень низкая"}
HRV_STATUS_RU = {"BALANCED": "в норме", "UNBALANCED": "нестабильна", "LOW": "низкая", "POOR": "очень низкая"}


@dataclass
class Signal:
    text: str                  # metric line shown to the user
    level: str | None = None   # "yellow" / "red" when it limits training
    reason: str = ""
    warning: bool = False      # counts towards "2 under-recovery signals → rest"
    short: str = ""            # compact form for the one-line summary ("" = not shown)


@dataclass
class Snapshot:
    source: str
    acts: list[dict]                                     # all sports, oldest first
    signals: list[Signal] = field(default_factory=list)
    acwr: float | None = None
    context: list[str] = field(default_factory=list)     # extra facts for the LLM
    lt_hr: int | None = None                             # lactate threshold HR


def fmt_hm(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h} ч {m:02d} мин"


def fmt_hhmm(seconds: float) -> str:
    h, m = divmod(round(seconds / 60), 60)
    return f"{h}:{m:02d}"


def fmt_hms(seconds: float) -> str:
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


# ── Shared thresholds ────────────────────────────────────────────────────────

def missing(what: str) -> Signal:
    return Signal(f"{what}: нет данных за ночь", "yellow", f"нет данных: {what.lower()} — синхронизируй часы",
                  short=f"нет данных: {what.lower()}")


def sleep_signal(sec: float, need: float, text: str, score: int | None = None) -> Signal:
    short = f"сон {fmt_hhmm(sec)}" + (f" ({score})" if score is not None else "")
    if sec < 5 * 3600:
        return Signal(text, "red", f"сон всего {fmt_hm(sec)}", short=short)
    if sec < 6.5 * 3600 or sec < 0.85 * need or (score is not None and score < 60):
        return Signal(text, "yellow", f"сон хуже нормы ({fmt_hm(sec)})", warning=True, short=short)
    return Signal(text, short=short)


def rhr_signal(today_hr: float, norm: float) -> Signal:
    text = f"Пульс покоя: {today_hr:.0f} (норма {norm:.0f})"
    short = f"пульс покоя {today_hr:.0f}"
    if today_hr - norm >= 8:
        return Signal(text, "red", f"пульс покоя выше нормы на {today_hr - norm:.0f}", short=short)
    if today_hr - norm >= 5:
        return Signal(text, "yellow", f"пульс покоя выше нормы на {today_hr - norm:.0f}", warning=True, short=short)
    return Signal(text, short=short)


# ── Garmin (primary) ─────────────────────────────────────────────────────────

def garmin_snapshot(today: date) -> Snapshot:
    """Raises (or exits) if Garmin is unavailable; the caller falls back to Tredict."""
    from garmin_sync import login   # Garmin login only when actually used
    c = login()
    d = today.isoformat()

    acts = []
    for a in c.get_activities_by_date((today - timedelta(days=370)).isoformat(), d):
        km = (a.get("distance") or 0) / 1000
        sec = a.get("duration") or 0
        acts.append({
            "date":  date.fromisoformat(a["startTimeLocal"][:10]),
            "sport": "running" if "run" in (a.get("activityType") or {}).get("typeKey", "") else
                     (a.get("activityType") or {}).get("typeKey", "other"),
            "title": a.get("activityName") or "",
            "km":    km,
            "min":   sec / 60,
            "pace":  sec / km if km else None,
            "hr":    round(a["averageHR"]) if a.get("averageHR") else None,
            "load":  a.get("activityTrainingLoad"),
        })
    snap = Snapshot("Garmin", sorted(acts, key=lambda a: a["date"]))

    # Readiness at wake-up, not the one recomputed after today's run
    wake = [x for x in c.get_training_readiness(d) or [] if x.get("inputContext") == "AFTER_WAKEUP_RESET"]
    if wake:
        score, level = wake[0].get("score"), wake[0].get("level")
        text = f"Готовность Garmin: {score} — {READINESS_RU.get(level, level)}"
        short = f"готовность {score}"
        if level == "POOR":
            snap.signals.append(Signal(text, "red", f"готовность Garmin очень низкая ({score})", short=short))
        elif level == "LOW":
            snap.signals.append(Signal(text, "yellow", f"готовность Garmin низкая ({score})", short=short))
        else:
            snap.signals.append(Signal(text, short=short))
    else:
        snap.signals.append(missing("Готовность Garmin"))

    hrv = (c.get_hrv_data(d) or {}).get("hrvSummary") or {}
    base = hrv.get("baseline") or {}
    if hrv.get("lastNightAvg") and base.get("balancedLow"):
        night, week = hrv["lastNightAvg"], hrv.get("weeklyAvg")
        low, high, floor = base["balancedLow"], base.get("balancedUpper"), base.get("lowUpper") or 0
        text = (f"ВСР: {night} мс, за неделю {week} (норма {low}–{high}), "
                f"{HRV_STATUS_RU.get(hrv.get('status'), hrv.get('status'))}")
        short = f"ВСР {night} ({low}–{high})"
        if night < floor or hrv.get("status") == "POOR":
            snap.signals.append(Signal(text, "red", f"ВСР {night} мс — ниже твоего нижнего порога {floor}", short=short))
        elif night < low or (week and week < low):
            snap.signals.append(Signal(text, "yellow", f"ВСР ниже нормы ({night} при норме от {low})", warning=True, short=short))
        else:
            snap.signals.append(Signal(text, short=short))
    else:
        snap.signals.append(missing("ВСР"))

    sleep = (c.get_sleep_data(d) or {}).get("dailySleepDTO") or {}
    if sleep.get("sleepTimeSeconds"):
        sec = sleep["sleepTimeSeconds"]
        need = ((sleep.get("sleepNeed") or {}).get("actual") or 480) * 60
        score = ((sleep.get("sleepScores") or {}).get("overall") or {}).get("value")
        text = (f"Сон: {fmt_hm(sec)} (нужно {fmt_hm(need)}), оценка {score}; "
                f"глубокий {fmt_hm(sleep.get('deepSleepSeconds') or 0)}, REM {fmt_hm(sleep.get('remSleepSeconds') or 0)}")
        snap.signals.append(sleep_signal(sec, need, text, score))
    else:
        snap.signals.append(missing("Сон"))

    stats = c.get_stats(d) or {}
    if stats.get("restingHeartRate") and stats.get("lastSevenDaysAvgRestingHeartRate"):
        snap.signals.append(rhr_signal(stats["restingHeartRate"], stats["lastSevenDaysAvgRestingHeartRate"]))
    if stats.get("bodyBatteryAtWakeTime"):
        snap.signals.append(Signal(f"Body Battery утром: {stats['bodyBatteryAtWakeTime']}"))

    # Context only: useful for choosing the session, not for limiting it
    try:
        ts = c.get_training_status(d) or {}
        status = next(iter(((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}).values()), {})
        snap.acwr = (status.get("acuteTrainingLoadDTO") or {}).get("dailyAcuteChronicWorkloadRatio")
        if status.get("trainingStatusFeedbackPhrase"):
            snap.context.append(f"Тренировочный статус Garmin: {status['trainingStatusFeedbackPhrase']}")
        balance = next(iter(((ts.get("mostRecentTrainingLoadBalance") or {}).get("metricsTrainingLoadBalanceDTOMap") or {}).values()), {})
        if balance.get("trainingBalanceFeedbackPhrase"):
            snap.context.append(
                f"Баланс нагрузки за 4 недели (Garmin): {balance['trainingBalanceFeedbackPhrase']} — "
                f"аэробная лёгкая {balance.get('monthlyLoadAerobicLow', 0):.0f}, аэробная интенсивная "
                f"{balance.get('monthlyLoadAerobicHigh', 0):.0f}, анаэробная {balance.get('monthlyLoadAnaerobic', 0):.0f}")
        vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {})
        if vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"):
            snap.context.append(f"МПК (VO2max) по Garmin: {vo2.get('vo2MaxPreciseValue') or vo2.get('vo2MaxValue')}")
    except Exception as exc:
        print(f"Garmin training status unavailable: {exc!r}")
    try:
        p = c.get_race_predictions() or {}
        if p.get("timeMarathon"):
            snap.context.append(
                f"Прогноз Garmin: 5 км {fmt_hms(p['time5K'])}, 10 км {fmt_hms(p['time10K'])}, "
                f"полумарафон {fmt_hms(p['timeHalfMarathon'])}, марафон {fmt_hms(p['timeMarathon'])}")
    except Exception as exc:
        print(f"Garmin race predictions unavailable: {exc!r}")
    try:
        lt = (c.get_lactate_threshold() or {}).get("speed_and_heart_rate") or {}
        snap.lt_hr = lt.get("heartRate")
    except Exception as exc:
        print(f"Garmin lactate threshold unavailable: {exc!r}")
    return snap


# ── Tredict (fallback) ───────────────────────────────────────────────────────

def by_day(records: dict) -> dict[date, list]:
    return {datetime.strptime(k, "%Y%m%d").date(): v for k, v in records.items()}


def local_date(ts: str, offset_s: int | None = None) -> date:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if offset_s is not None:
        return (dt + timedelta(seconds=offset_s)).date()
    return dt.astimezone(TZ).date()


def tredict_snapshot(today: date) -> Snapshot:
    efforts = {d: sum(e[0] for e in v) for d, v in by_day(tredict.get("efforts")["trainingEfforts"]).items()}

    acts = []
    for a in tredict.get("activityList", pageSize=200, extendedSummary=1)["_embedded"]["activityList"]:
        s = a.get("summary") or {}
        day, minutes = local_date(a["date"]), (s.get("duration") or 0) / 60
        acts.append({
            "date":  day,
            "sport": a.get("sportType"),
            "title": a.get("title") or "",
            "km":    (s.get("distance") or 0) / 1000,
            "min":   minutes,
            "pace":  s.get("pace"),
            "hr":    s.get("heartrate"),
            "load":  efforts.get(day),
            # Tredict effort per hour separates intervals (>=120) from easy runs (<=108)
            "effort_per_h": efforts.get(day, 0) / (minutes / 60) if minutes else 0,
        })
    snap = Snapshot("Tredict", sorted(acts, key=lambda a: a["date"]))

    hrv = by_day(tredict.get("hrv")["hrv"])
    if today in hrv:
        night, base = hrv[today]
        r3 = mean(hrv[d][0] / hrv[d][1] for d in (today - timedelta(i) for i in range(3)) if d in hrv and hrv[d][1])
        text = f"ВСР: {night} мс (норма {base})"
        short = f"ВСР {night} (норма {base})"
        # A crash (<60%) is red on its own: good nights before it must not mask it
        if night / base < 0.6 or (night / base < 0.75 and r3 < 0.9):
            snap.signals.append(Signal(text, "red", f"ВСР {night} мс — сильно ниже нормы {base}", short=short))
        elif night / base < 0.85 or r3 < 0.9:
            snap.signals.append(Signal(text, "yellow", f"ВСР ниже нормы ({night} при норме {base})", warning=True, short=short))
        else:
            snap.signals.append(Signal(text, short=short))
    else:
        snap.signals.append(missing("ВСР"))

    sleep = by_day(tredict.get("sleep")["sleep"])
    if today in sleep:
        sec, base = sleep[today]
        snap.signals.append(sleep_signal(sec, base, f"Сон: {fmt_hm(sec)} (норма {fmt_hm(base)})"))
    else:
        snap.signals.append(missing("Сон"))

    rhr = {local_date(v["timestamp"], v.get("timezoneOffsetInSeconds")): v["hrRestDynamic"]
           for v in tredict.get("bodyvalues")["bodyvalues"] if v.get("hrRestDynamic")}
    prev = [rhr[today - timedelta(i)] for i in range(1, 15) if today - timedelta(i) in rhr]
    if today in rhr and prev:
        snap.signals.append(rhr_signal(rhr[today], mean(prev)))

    def load(days: int) -> float:
        return sum(efforts.get(today - timedelta(i), 0) for i in range(1, days + 1))
    if load(28):
        snap.acwr = load(7) / (load(28) / 4)
    return snap


# ── HR zones ─────────────────────────────────────────────────────────────────

def zones_from_lt(lt: int) -> dict:
    return {"recovery_max": round(lt * .78), "easy": (round(lt * .79), round(lt * .89)),
            "steady": (round(lt * .90), round(lt * .95)), "threshold": (round(lt * .96), lt),
            "vo2_min": lt + 1}


def hr_zones(lt_hr: int | None) -> tuple[dict, str | None]:
    """Tredict zones (set by the athlete); otherwise derived from Garmin's LT heart rate."""
    try:
        zones = tredict.get("zones", sportType="running")["zones"]["running"]["heartrate"]
        z = zones[max(zones)]
        if len(z) == 5:
            return {"recovery_max": z[0]["to"], "easy": (z[1]["from"], z[1]["to"]),
                    "steady": (z[2]["from"], z[2]["to"]), "threshold": (z[3]["from"], z[3]["to"]),
                    "vo2_min": z[4]["from"]}, None
    except (Exception, SystemExit) as exc:   # SystemExit: Tredict token rejected
        print(f"Tredict zones unavailable: {exc!r}")
    if lt_hr:
        return zones_from_lt(lt_hr), "⚠️ Tredict недоступен — зоны рассчитаны от ПАНО Garmin"
    raise RuntimeError("No HR zones: Tredict and Garmin lactate threshold both unavailable")
