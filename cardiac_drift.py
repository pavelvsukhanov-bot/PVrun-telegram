"""
Cardiac drift of a run: heart rate and pace in the first vs second half.

The first 10 minutes are skipped (HR is still climbing), stops and walking are
dropped, and pace is Garmin's grade-adjusted speed so hills don't fake drift.
The reported figure is aerobic decoupling (Pa:HR): how much the
speed-per-heartbeat fell in the second half. Unlike the plain HR change it is
not fooled by speeding up or slowing down. Under 5% is a solid aerobic base
for that duration.

Drift is only comparable between steady runs: interval sessions, runs with
strides/pickups and races are reported but excluded from monthly trends.
"""

import re
from dataclasses import dataclass
from statistics import mean, median, pstdev

from garminconnect.activity_details import parse_activity_detail_metrics

WARMUP_S = 600
MIN_ANALYZED_S = 20 * 60      # so drift is shown for runs of 30+ minutes
MIN_SPEED = 1.0               # m/s; slower means stopped or walking
# Steadiness, calibrated on Sep-Oct 2026 runs (1-minute averages of grade-adjusted speed)
MAX_STEADY_CV = 0.12
MAX_FAST_MINUTES = 0.05       # share of minutes >15% faster than the median: strides, pickups
RACE_KM = 30
HARD_TITLE = re.compile(r"интенсив|интервал|темп|порог|фартлек|спринт|\d+\s*[xх×]\s*\d", re.I)


@dataclass
class Drift:
    minutes: float      # analyzed time (after warm-up)
    hr1: float
    hr2: float
    v1: float           # m/s, grade-adjusted
    v2: float
    steady: bool        # comparable with other steady runs
    temp_c: float | None = None
    humidity: float | None = None

    @property
    def decoupling(self) -> float:
        """Pa:HR — loss of speed per heartbeat in the second half, %."""
        return (self.v1 / self.hr1 - self.v2 / self.hr2) / (self.v1 / self.hr1) * 100


def _wmean(points: list[tuple[float, float]]) -> float:
    return sum(v * w for v, w in points) / sum(w for _, w in points)


def analyze(samples: list[dict], title: str = "", km: float = 0) -> Drift | None:
    pts = [(p.get("sumMovingDuration") or 0, p.get("directHeartRate"),
            p.get("directGradeAdjustedSpeed") or p.get("directSpeed")) for p in samples]
    pts = [(t, hr, v) for t, hr, v in pts if hr and v and v > MIN_SPEED and t >= WARMUP_S]
    if len(pts) < 20 or pts[-1][0] - pts[0][0] < MIN_ANALYZED_S:
        return None
    # Samples are not evenly spaced: weight each by the time until the next one
    weights = [max(b[0] - a[0], 0) for a, b in zip(pts, pts[1:])] + [0]
    mid = (pts[0][0] + pts[-1][0]) / 2
    first = [(p, w) for p, w in zip(pts, weights) if p[0] < mid]
    second = [(p, w) for p, w in zip(pts, weights) if p[0] >= mid]
    if not sum(w for _, w in first) or not sum(w for _, w in second):
        return None

    by_minute: dict[int, list[float]] = {}
    for t, _, v in pts:
        by_minute.setdefault(int(t // 60), []).append(v)
    minutes = [mean(v) for v in by_minute.values()]
    typical = median(minutes)
    steady = (not HARD_TITLE.search(title.replace("​", ""))
              and km < RACE_KM
              and pstdev(minutes) / mean(minutes) <= MAX_STEADY_CV
              and sum(m > 1.15 * typical for m in minutes) / len(minutes) <= MAX_FAST_MINUTES)

    return Drift(
        minutes=(pts[-1][0] - pts[0][0]) / 60,
        hr1=_wmean([(p[1], w) for p, w in first]), hr2=_wmean([(p[1], w) for p, w in second]),
        v1=_wmean([(p[2], w) for p, w in first]), v2=_wmean([(p[2], w) for p, w in second]),
        steady=steady,
    )


def for_activity(client, activity_id, title: str = "", km: float = 0) -> Drift | None:
    """Drift with weather for one Garmin activity; None if too short or unavailable."""
    try:
        drift = analyze(parse_activity_detail_metrics(client.get_activity_details(str(activity_id))), title, km)
    except Exception as exc:
        print(f"Cardiac drift unavailable for {activity_id}: {exc!r}")
        return None
    if drift:
        try:
            w = client.get_activity_weather(str(activity_id)) or {}
            if w.get("temp") is not None:
                drift.temp_c = (w["temp"] - 32) * 5 / 9      # Garmin reports °F
            drift.humidity = w.get("relativeHumidity")
        except Exception as exc:
            print(f"Weather unavailable for {activity_id}: {exc!r}")
    return drift


def grade(decoupling: float) -> str:
    return "✅" if decoupling < 5 else "🟡" if decoupling < 10 else "🔴"


def format_lines(d: Drift) -> list[str]:
    """Telegram lines for the run message (plain text, no HTML)."""
    weather = ""
    if d.temp_c is not None:
        weather = f" · {d.temp_c:.0f}°C" + (f", влажность {d.humidity:.0f}%" if d.humidity is not None else "")
    lines = [f"Pa:HR (пульс с учётом темпа): {d.decoupling:.1f}% {grade(d.decoupling)}{weather}"]
    if not d.steady:
        lines.append("Неровная пробежка (интервалы/ускорения) — с лёгкими не сравнивается")
    return lines


def summarize(drifts: list[Drift]) -> dict | None:
    """Monthly figures over steady runs only."""
    steady = [d for d in drifts if d.steady]
    if not steady:
        return None
    long_runs = [d for d in steady if d.minutes + WARMUP_S / 60 >= 75]
    temps = [d.temp_c for d in steady if d.temp_c is not None]
    return {
        "n": len(steady),
        "decoupling": median(d.decoupling for d in steady),
        "long_n": len(long_runs),
        "long_decoupling": median(d.decoupling for d in long_runs) if long_runs else None,
        "temp_c": mean(temps) if temps else None,
    }
