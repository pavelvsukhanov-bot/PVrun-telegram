"""Quick analysis queries against garmin.db."""

from db import get_conn


def print_recent(n: int = 20) -> None:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT start_time, distance_m/1000 AS km,
                   avg_pace_s_km, avg_hr, calories, name
            FROM runs
            ORDER BY start_time DESC
            LIMIT ?
        """, (n,)).fetchall()

    print(f"{'Date':<12} {'Dist':>7} {'Pace':>8} {'HR':>4} {'kcal':>5}  Name")
    print("-" * 60)
    for r in rows:
        pace = _fmt_pace(r["avg_pace_s_km"])
        print(f"{r['start_time'][:10]:<12} {r['km']:>6.2f}k {pace:>8} {r['avg_hr'] or '—':>4} {r['calories'] or '—':>5}  {r['name']}")


def monthly_summary() -> None:
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT substr(start_time,1,7) AS month,
                   COUNT(*) AS runs,
                   ROUND(SUM(distance_m)/1000,1) AS total_km,
                   ROUND(AVG(avg_hr)) AS avg_hr
            FROM runs
            GROUP BY month
            ORDER BY month DESC
        """).fetchall()

    print(f"{'Month':<8} {'Runs':>5} {'Total km':>9} {'Avg HR':>7}")
    print("-" * 35)
    for r in rows:
        print(f"{r['month']:<8} {r['runs']:>5} {r['total_km']:>9} {r['avg_hr'] or '—':>7}")


def _fmt_pace(s: float | None) -> str:
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}/km"


if __name__ == "__main__":
    print("=== Last 20 runs ===")
    print_recent(20)
    print()
    print("=== Monthly summary ===")
    monthly_summary()
