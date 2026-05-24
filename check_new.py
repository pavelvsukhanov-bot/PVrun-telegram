"""
One-shot script for GitHub Actions.
Checks Garmin for new runs → sends to Telegram → saves state.

State (sent activity IDs) is kept in state.json (committed to the repo).
Garmin OAuth tokens are cached in ./garmin_tokens/ (GitHub Actions cache).
"""

import json
import time
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv

from garmin_sync import fetch_activities, is_run, login, parse_activity
from tg_poller import format_run, tg_send

load_dotenv()

STATE_FILE  = Path(__file__).parent / "state.json"
TOKEN_DIR   = Path(__file__).parent / "garmin_tokens"


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

    TOKEN_DIR.mkdir(exist_ok=True)
    client = login(tokenstore=str(TOKEN_DIR))

    end   = date.today()
    start = end - timedelta(days=3)   # look back 3 days to catch slow syncs
    activities = fetch_activities(client, start, end)
    runs = [a for a in activities if is_run(a)]

    print(f"Found {len(runs)} run(s) in the last 3 days.")

    new_count = 0
    for act in runs:
        row = parse_activity(act)
        if row["activity_id"] in sent_ids:
            print(f"  Skip (already sent): {row['start_time'][:10]}  {row['name']}")
            continue

        print(f"  New run: {row['start_time'][:10]}  {(row['distance_m'] or 0)/1000:.2f} km  {row['name']}")
        text = format_run(row)
        tg_send(text)
        sent_ids.add(row["activity_id"])
        new_count += 1
        time.sleep(1)

    print(f"Sent {new_count} new run(s) to Telegram.")

    # Keep last 200 IDs so the file doesn't grow forever
    state["sent_ids"] = list(sent_ids)[-200:]
    save_state(state)


if __name__ == "__main__":
    main()
