"""
Fetches running activities from Garmin Connect and stores them in garmin.db (SQLite).

Usage:
    python garmin_sync.py              # sync last 30 days
    python garmin_sync.py --days 90    # sync last N days
    python garmin_sync.py --all        # sync all available activities
"""

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta

from dotenv import load_dotenv
from garminconnect import Garmin, GarminConnectConnectionError, GarminConnectAuthenticationError

from db import get_conn, init_db

load_dotenv()

RUNNING_TYPE_IDS = {
    "running",
    "trail_running",
    "treadmill_running",
    "ultra_run",
    "obstacle_run",
    "virtual_run",
}

# garminconnect activity type key in the activity dict
ACTIVITY_TYPE_KEY = "activityType"


def _prompt_mfa() -> str:
    # In CI: read from env var (passed as workflow input)
    code = os.environ.get("GARMIN_MFA_CODE", "").strip()
    if code:
        print(f"Using MFA code from environment.")
        return code
    return input("Enter MFA/2FA code from your Garmin authenticator app: ").strip()


def login(tokenstore: str | None = None) -> Garmin:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env file")

    client = Garmin(email, password, prompt_mfa=_prompt_mfa)
    try:
        client.login(tokenstore=tokenstore)
    except GarminConnectAuthenticationError as exc:
        sys.exit(f"Authentication failed: {exc}")
    except GarminConnectConnectionError as exc:
        sys.exit(f"Connection error: {exc}")
    return client


def is_run(activity: dict) -> bool:
    type_info = activity.get(ACTIVITY_TYPE_KEY, {})
    type_key = type_info.get("typeKey", "").lower()
    parent_key = type_info.get("parentTypeId", "")
    return type_key in RUNNING_TYPE_IDS or "run" in type_key


def parse_activity(activity: dict) -> dict:
    duration_s = activity.get("duration", 0)
    distance_m = activity.get("distance", 0)
    avg_pace = (duration_s / (distance_m / 1000)) if distance_m > 0 else None

    return {
        "activity_id":          str(activity.get("activityId")),
        "name":                 activity.get("activityName"),
        "start_time":           activity.get("startTimeLocal"),
        "distance_m":           distance_m,
        "duration_s":           duration_s,
        "moving_time_s":        activity.get("movingDuration"),
        "avg_hr":               activity.get("averageHR"),
        "max_hr":               activity.get("maxHR"),
        "avg_pace_s_km":        avg_pace,
        "calories":             activity.get("calories"),
        "elevation_gain":       activity.get("elevationGain"),
        "avg_cadence":          activity.get("averageRunningCadenceInStepsPerMinute"),
        "avg_gct_ms":           activity.get("avgGroundContactTime"),
        "avg_stride_length_cm": activity.get("avgStrideLength"),
        "avg_vert_osc_cm":      activity.get("avgVerticalOscillation"),
        "avg_vert_ratio_pct":   activity.get("avgVerticalRatio"),
        "training_effect":      activity.get("aerobicTrainingEffect"),
        "raw_json":             json.dumps(activity, ensure_ascii=False),
    }


def upsert_run(row: dict) -> bool:
    """Returns True if newly inserted, False if already existed."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM runs WHERE activity_id = ?", (row["activity_id"],)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """
            INSERT INTO runs (
                activity_id, name, start_time, distance_m, duration_s,
                moving_time_s, avg_hr, max_hr, avg_pace_s_km, calories,
                elevation_gain, avg_cadence, avg_gct_ms, avg_stride_length_cm,
                avg_vert_osc_cm, avg_vert_ratio_pct, training_effect, raw_json
            ) VALUES (
                :activity_id, :name, :start_time, :distance_m, :duration_s,
                :moving_time_s, :avg_hr, :max_hr, :avg_pace_s_km, :calories,
                :elevation_gain, :avg_cadence, :avg_gct_ms, :avg_stride_length_cm,
                :avg_vert_osc_cm, :avg_vert_ratio_pct, :training_effect, :raw_json
            )
            """,
            row,
        )
        return True


def fetch_activities(client: Garmin, start: date, end: date) -> list[dict]:
    """Fetches activities page by page until outside the date range."""
    all_activities = []
    limit = 100
    offset = 0

    while True:
        batch = client.get_activities(offset, limit)
        if not batch:
            break

        in_range = []
        exhausted = False
        for act in batch:
            act_date_str = (act.get("startTimeLocal") or "")[:10]
            try:
                act_date = date.fromisoformat(act_date_str)
            except ValueError:
                continue
            if act_date < start:
                exhausted = True
                break
            if act_date <= end:
                in_range.append(act)

        all_activities.extend(in_range)

        if exhausted or len(batch) < limit:
            break

        offset += limit
        time.sleep(0.5)  # be polite to Garmin servers

    return all_activities


def fmt_pace(seconds_per_km: float | None) -> str:
    if not seconds_per_km:
        return "—"
    m, s = divmod(int(seconds_per_km), 60)
    return f"{m}:{s:02d}/km"


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Garmin running activities to SQLite")
    parser.add_argument("--days", type=int, default=30, help="Number of past days to sync (default: 30)")
    parser.add_argument("--all", action="store_true", dest="sync_all", help="Sync all available activities")
    args = parser.parse_args()

    init_db()

    end = date.today()
    start = date(2000, 1, 1) if args.sync_all else end - timedelta(days=args.days)

    print(f"Logging in to Garmin Connect...")
    client = login()
    print(f"OK. Fetching activities from {start} to {end}...")

    activities = fetch_activities(client, start, end)
    runs = [a for a in activities if is_run(a)]

    print(f"Found {len(activities)} total activities, {len(runs)} runs.")

    new_count = 0
    for act in runs:
        row = parse_activity(act)
        if upsert_run(row):
            new_count += 1
            dist_km = (row["distance_m"] or 0) / 1000
            pace = fmt_pace(row["avg_pace_s_km"])
            print(f"  + {row['start_time'][:10]}  {dist_km:.2f} km  {pace}  HR {row['avg_hr']}  \"{row['name']}\"")

    print(f"\nDone. {new_count} new runs added to garmin.db.")


if __name__ == "__main__":
    main()
