"""
Cardiac drift of a run: heart rate and pace in the first vs second half.

The first 10 minutes are skipped (HR is still climbing), stops and walking are
dropped, and pace is Garmin's grade-adjusted speed so hills don't fake drift.
The reported figure is aerobic decoupling (Pa:HR): how much the
speed-per-heartbeat fell in the second half. Unlike the plain HR change it is
not fooled by speeding up or slowing down. Under 5% is a solid aerobic base
for that duration.

Drift is measured only where its rules allow: a calm (easy, aerobic) run of
40+ minutes at a steady pace. Interval and intensive sessions, runs with
pickups, short runs and races get a "not measured" label instead of a number,
and stay out of monthly trends.
"""

import re
from dataclasses import dataclass
from statistics import mean, median, pstdev

from garminconnect.activity_details import parse_activity_detail_metrics

WARMUP_S = 600
MIN_ANALYZED_S = 30 * 60      # 40+ minute runs: shorter ones are over before drift shows
MIN_SPEED = 1.0               # m/s; slower means stopped or walking
# Steadiness, calibrated on Sep-Oct 2026 runs (1-minute averages of grade-adjusted speed)
MAX_STEADY_CV = 0.12
MAX_FAST_MINUTES = 0.05       # share of minutes >15% faster than the median: strides, pickups
RACE_KM = 30
INTERVAL_TITLE = re.compile(r"интервал|спринт|фартлек|повтор|\d+\s*[xх×]\s*\d", re.I)
INTENSIVE_TITLE = re.compile(r"интенсив|темп|порог", re.I)
HARD_TITLE = re.compile(f"{INTERVAL_TITLE.pattern}|{INTENSIVE_TITLE.pattern}", re.I)
EASY_HR_MARGIN = 3            # first-half HR this far above the easy zone = not a calm run

# Why drift was not measured, shown instead of the number
NOT_MEASURED = {
    "interval": "интервальная тренировка — не определяется",
    "intensive": "интенсивная тренировка — не определяется",
    "unsteady": "неровный темп (ускорения или рывки) — не определяется",
    "short": "пробежка короче 40 минут — не определяется",
    "race": "забег — не определяется",
}


@dataclass
class Drift:
    kind: str                   # "easy" (measured) or a NOT_MEASURED key
    minutes: float = 0          # analyzed time (after warm-up)
    hr1: float | None = None
    hr2: float | None = None
    v1: float | None = None     # m/s, grade-adjusted
    v2: float | None = None
    # Observed weather during the run, set by the caller from weather.py (nearest station)
    temp_c: float | None = None
    wbgt_c: float | None = None

    @property
    def steady(self) -> bool:
        """Drift was measured: a calm, steady run (comparable with other such runs)."""
        return self.kind == "easy"

    @property
    def decoupling(self) -> float | None:
        """Pa:HR — loss of speed per heartbeat in the second half, %."""
        if not self.steady:
            return None
        return (self.v1 / self.hr1 - self.v2 / self.hr2) / (self.v1 / self.hr1) * 100


def _wmean(points: list[tuple[float, float]]) -> float:
    return sum(v * w for v, w in points) / sum(w for _, w in points)


def analyze(samples: list[dict], title: str = "", km: float = 0, easy_max_hr: float | None = None) -> Drift:
    title = title.replace("\u200b", "")   # Garmin names may contain zero-width spaces
    if INTERVAL_TITLE.search(title):
        return Drift("interval")
    if INTENSIVE_TITLE.search(title):
        return Drift("intensive")
    if km >= RACE_KM:
        return Drift("race")
    pts = [(p.get("sumMovingDuration") or 0, p.get("directHeartRate"),
            p.get("directGradeAdjustedSpeed") or p.get("directSpeed")) for p in samples]
    pts = [(t, hr, v) for t, hr, v in pts if hr and v and v > MIN_SPEED and t >= WARMUP_S]
    if len(pts) < 20 or pts[-1][0] - pts[0][0] < MIN_ANALYZED_S:
        return Drift("short")
    # Samples are not evenly spaced: weight each by the time until the next one
    weights = [max(b[0] - a[0], 0) for a, b in zip(pts, pts[1:])] + [0]
    mid = (pts[0][0] + pts[-1][0]) / 2
    first = [(p, w) for p, w in zip(pts, weights) if p[0] < mid]
    second = [(p, w) for p, w in zip(pts, weights) if p[0] >= mid]
    if not sum(w for _, w in first) or not sum(w for _, w in second):
        return Drift("short")
    hr1 = _wmean([(p[1], w) for p, w in first])
    # Drift is defined for aerobic effort: a faster run has its own HR dynamics
    if easy_max_hr and hr1 > easy_max_hr + EASY_HR_MARGIN:
        return Drift("intensive")

    by_minute: dict[int, list[float]] = {}
    for t, _, v in pts:
        by_minute.setdefault(int(t // 60), []).append(v)
    minutes = [mean(v) for v in by_minute.values()]
    typical = median(minutes)
    if (pstdev(minutes) / mean(minutes) > MAX_STEADY_CV
            or sum(m > 1.15 * typical for m in minutes) / len(minutes) > MAX_FAST_MINUTES):
        return Drift("unsteady")

    return Drift(
        "easy",
        minutes=(pts[-1][0] - pts[0][0]) / 60,
        hr1=hr1, hr2=_wmean([(p[1], w) for p, w in second]),
        v1=_wmean([(p[2], w) for p, w in first]), v2=_wmean([(p[2], w) for p, w in second]),
    )


def for_activity(client, activity_id, title: str = "", km: float = 0, easy_max_hr: float | None = None) -> Drift | None:
    """Drift of one Garmin activity; None if its detail data is unavailable."""
    try:
        return analyze(parse_activity_detail_metrics(client.get_activity_details(str(activity_id))),
                       title, km, easy_max_hr)
    except Exception as exc:
        print(f"Cardiac drift unavailable for {activity_id}: {exc!r}")
        return None


def grade(decoupling: float) -> str:
    return "✅" if decoupling < 5 else "🟡" if decoupling < 10 else "🔴"


def describe(d: Drift) -> str:
    """Text for the run message (plain, no HTML): the number, or why there is none."""
    if d.steady:
        return f"Pa:HR (пульс с учётом темпа) {d.decoupling:.1f}% {grade(d.decoupling)}"
    return NOT_MEASURED[d.kind]


def summarize(drifts: list[Drift]) -> dict | None:
    """Monthly figures over steady runs only."""
    steady = [d for d in drifts if d.steady]
    if not steady:
        return None
    long_runs = [d for d in steady if d.minutes + WARMUP_S / 60 >= 75]
    temps = [d.temp_c for d in steady if d.temp_c is not None]
    wbgts = [d.wbgt_c for d in steady if d.wbgt_c is not None]
    return {
        "n": len(steady),
        "decoupling": median(d.decoupling for d in steady),
        "long_n": len(long_runs),
        "long_decoupling": median(d.decoupling for d in long_runs) if long_runs else None,
        "temp_c": mean(temps) if temps else None,
        "wbgt_c": mean(wbgts) if wbgts else None,
    }
