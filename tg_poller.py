"""
Polls Garmin Connect every 15 minutes.
When a new run appears → sends biophysical metrics to Telegram.

Run once and leave in background:
    python tg_poller.py

Required .env variables:
    GARMIN_EMAIL, GARMIN_PASSWORD
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import os
import sys
import time
import json
import requests
from datetime import date, timedelta
from dotenv import load_dotenv

from db import get_conn, init_db
from garmin_sync import login, fetch_activities, is_run, parse_activity, upsert_run, fmt_pace

load_dotenv()

POLL_INTERVAL = 15 * 60  # seconds


# ── Telegram helpers ────────────────────────────────────────────────────────

def tg_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[TG] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
    if not resp.ok:
        print(f"[TG] Send failed: {resp.status_code} {resp.text}")


def format_run(row) -> str:
    def pace(s):
        if not s:
            return "—"
        m, sec = divmod(int(s), 60)
        return f"{m}:{sec:02d} /km"

    def val(v, unit="", decimals=1):
        if v is None:
            return "—"
        return f"{v:.{decimals}f}{unit}"

    dist_km = (row["distance_m"] or 0) / 1000
    dur_s = int(row["duration_s"] or 0)
    h, rem = divmod(dur_s, 3600)
    m, s = divmod(rem, 60)
    duration_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

    lines = [
        f"<b>Пробежка  {row['start_time'][:10]}</b>",
        f"{row['name']}",
        "",
        f"Дистанция:       {dist_km:.2f} км",
        f"Время:           {duration_str}",
        f"Темп:            {pace(row['avg_pace_s_km'])}",
        f"Пульс ср/макс:   {int(row['avg_hr'] or 0)} / {int(row['max_hr'] or 0)} уд/мин",
        f"Калории:         {int(row['calories'] or 0)} ккал",
        "",
        "<b>Беговая динамика</b>",
        f"Каденс:          {val(row['avg_cadence'], ' ш/мин', 0)}",
        f"ВКЗ:             {val(row['avg_gct_ms'], ' мс', 0)}",
        f"Длина шага:      {val(row['avg_stride_length_cm'], ' см', 0)}",
        f"Верт. колебание: {val(row['avg_vert_osc_cm'], ' см')}",
        f"Верт. соотношение:{val(row['avg_vert_ratio_pct'], '%')}",
    ]
    return "\n".join(lines)


def mark_sent(activity_id: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE runs SET tg_sent = 1 WHERE activity_id = ?", (activity_id,))


def get_unsent_runs() -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE tg_sent = 0 ORDER BY start_time ASC"
        ).fetchall()


# ── Main loop ───────────────────────────────────────────────────────────────

def check_new_runs(client) -> None:
    end = date.today()
    start = end - timedelta(days=3)  # look back 3 days to catch delayed syncs
    activities = fetch_activities(client, start, end)
    runs = [a for a in activities if is_run(a)]

    new = 0
    for act in runs:
        row = parse_activity(act)
        if upsert_run(row):
            new += 1
            print(f"  New run: {row['start_time'][:10]}  {(row['distance_m'] or 0)/1000:.2f} km")

    if new:
        print(f"  {new} new run(s) added to DB")


def send_unsent() -> None:
    rows = get_unsent_runs()
    for row in rows:
        text = format_run(row)
        tg_send(text)
        mark_sent(row["activity_id"])
        print(f"  Sent to Telegram: {row['start_time'][:10]} {row['name']}")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        sys.exit(
            "Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env\n"
            "See README for how to get them."
        )

    init_db()
    print("Garmin → Telegram poller started. Press Ctrl+C to stop.")
    print(f"Checking every {POLL_INTERVAL // 60} minutes...\n")

    client = login()
    print("Logged in to Garmin Connect.\n")

    while True:
        print(f"[{time.strftime('%H:%M:%S')}] Checking for new runs...")
        try:
            check_new_runs(client)
            send_unsent()
        except Exception as exc:
            print(f"  Error: {exc} — will retry next cycle")

        print(f"  Sleeping {POLL_INTERVAL // 60} min...\n")
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
