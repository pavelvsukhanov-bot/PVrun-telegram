"""
One-shot script for GitHub Actions.
Checks Garmin for new runs → sends to Telegram → saves state.

State (sent activity IDs) is kept in state.json (committed to the repo).
Garmin OAuth tokens are cached in ./garmin_tokens/ (GitHub Actions cache).
"""

import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from garmin_sync import fetch_activities, is_run, login, parse_activity
from tg_poller import format_run, tg_send

load_dotenv()

STATE_FILE  = Path(__file__).parent / "state.json"
TOKEN_DIR   = Path(__file__).parent / "garmin_tokens"

# Wide enough that runs missed during an outage (e.g. expired tokens) are sent
# once sync recovers; state.json prevents duplicates.
LOOKBACK_DAYS = 10


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sent_ids": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def main() -> None:
    state    = load_state()
    sent_ids = set(state.get("sent_ids", []))

    # tokenstore=None → login() reads GARMINTOKENS env var automatically
    client = login()

    end   = date.today()
    start = end - timedelta(days=LOOKBACK_DAYS)
    activities = fetch_activities(client, start, end)
    runs = [a for a in activities if is_run(a)]

    print(f"Found {len(runs)} run(s) in the last {LOOKBACK_DAYS} days.")

    new_count = 0
    failed = 0
    for act in runs:
        row = parse_activity(act)
        if row["activity_id"] in sent_ids:
            print(f"  Skip (already sent): {row['start_time'][:10]}  {row['name']}")
            continue

        print(f"  New run: {row['start_time'][:10]}  {(row['distance_m'] or 0)/1000:.2f} km  {row['name']}")
        text = format_run(row)
        if not tg_send(text):
            failed += 1  # not marked as sent → retried next run
            continue
        sent_ids.add(row["activity_id"])
        new_count += 1
        time.sleep(1)

    print(f"Sent {new_count} new run(s) to Telegram.")

    # Garmin IDs grow over time: sorting keeps the file stable between runs
    # (no noise commits) and the cap drops the oldest IDs, not random ones.
    state["sent_ids"] = sorted(sent_ids, key=int)[-200:]
    save_state(state)

    if failed:
        sys.exit(f"{failed} run(s) failed to send to Telegram")


if __name__ == "__main__":
    main()
