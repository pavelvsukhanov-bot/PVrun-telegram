"""
Running-dynamics colour bands, the same as Garmin Connect shows them.

Garmin rates each metric in five bands from population data: purple and blue
are the best, then green, orange, red. Stride length has no band: it depends
on height and speed.
"""

from bisect import bisect_right

# metric: (band boundaries, ascending; colour for each band from the lowest value up)
BANDS = {
    "cadence":    ([153, 164, 174, 184], ["🔴", "🟠", "🟢", "🔵", "🟣"]),   # steps/min, higher is better
    "gct":        ([218, 249, 278, 309], ["🟣", "🔵", "🟢", "🟠", "🔴"]),   # ground contact, ms
    "vert_osc":   ([6.4, 8.2, 9.8, 11.6], ["🟣", "🔵", "🟢", "🟠", "🔴"]),  # vertical oscillation, cm
    "vert_ratio": ([6.1, 7.5, 8.7, 10.2], ["🟣", "🔵", "🟢", "🟠", "🔴"]),  # vertical ratio, %
}


def color(metric: str, value: float | None) -> str:
    if value is None or metric not in BANDS:
        return ""
    bounds, colours = BANDS[metric]
    return colours[bisect_right(bounds, value)]
