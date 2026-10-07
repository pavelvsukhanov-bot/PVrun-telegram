"""
"⌚ Отправить на часы" button → structured workout in the Garmin calendar.

The Cloudflare Worker passes the workout code from the button, the plan
message's date and id. The workout is uploaded with the athlete's HR zones and
scheduled for the plan's day; the watch picks it up on its next sync.

Usage (normally from the send_workout workflow):
    python send_workout.py --code "w10e|4x8t/2e|c10e" --msg-date 1759910400 --message-id 123
"""

import argparse
import html
import sys
from datetime import datetime

import watch_workout
from garmin_sync import login
from plan_sources import TZ, hr_zones
from telegram import tg_call, tg_send


def already_scheduled(client, day, workout_id: int, name: str) -> bool:
    items = (client.get_scheduled_workouts(day.year, day.month) or {}).get("calendarItems", [])
    return any(i.get("itemType") == "workout" and i.get("date") == day.isoformat()
               and (i.get("workoutId") == workout_id or i.get("title") == name) for i in items)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True)
    parser.add_argument("--msg-date", type=int, required=True, help="plan message date (unix time)")
    parser.add_argument("--message-id", type=int, required=True)
    args = parser.parse_args()

    # First thing: remove the button so a second tap cannot send it twice
    tg_call("editMessageReplyMarkup", message_id=args.message_id, reply_markup={"inline_keyboard": []})

    try:
        blocks = watch_workout.parse(args.code)
    except ValueError as exc:
        tg_send(f"⚠️ Не могу разобрать тренировку для часов: {html.escape(str(exc))}")
        sys.exit(1)

    day = datetime.fromtimestamp(args.msg_date, TZ).date()
    today = datetime.now(TZ).date()
    if day != today:
        # Readiness was judged for that morning; today's state may differ
        tg_send(f"⏰ Этот план был на {day:%d.%m}. Пришли <b>?</b> — составлю план на сегодня.")
        return

    client = login()
    lt_hr = None
    try:
        lt_hr = ((client.get_lactate_threshold() or {}).get("speed_and_heart_rate") or {}).get("heartRate")
    except Exception as exc:
        print(f"Garmin lactate threshold unavailable: {exc!r}")
    zones, _ = hr_zones(lt_hr)

    name = f"PVrun {day:%d.%m} {watch_workout.short_title(blocks)}"
    existing = [w for w in client.get_workouts(0, 50) if w.get("workoutName") == name]
    if existing:
        workout_id = existing[0]["workoutId"]
    else:
        workout_id = client.upload_running_workout(watch_workout.to_garmin(blocks, zones, name))["workoutId"]

    if already_scheduled(client, day, workout_id, name):
        tg_send(f"✅ «{html.escape(name)}» уже в календаре Garmin на сегодня.")
        return
    client.schedule_workout(workout_id, day.isoformat())

    tg_send("\n".join([
        f"✅ <b>{html.escape(name)}</b> — в календаре Garmin на сегодня.",
        html.escape(watch_workout.describe(blocks)),
        "",
        "Синхронизируй часы (Garmin Connect на телефоне) — тренировка появится в «Тренировки» → «Календарь».",
    ]))
    print(f"Workout {workout_id} scheduled for {day}")


if __name__ == "__main__":
    main()
