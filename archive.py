"""
Private run archive in the athlete's Google Sheet ("PVrun — архив пробежек").

Writes go through the sheet's Apps Script web app (google_sheet/Code.gs),
authorised by a shared token, so the sheet itself stays private. Needs
ARCHIVE_URL and ARCHIVE_TOKEN; without them archiving is simply skipped.

Usage:
    python archive.py --backfill 2026-08-01   # one-off: archive all runs since a date
"""

import argparse
import os
import sys
from datetime import date, datetime

import requests
from dotenv import load_dotenv

load_dotenv()

# Must match the header row of the sheet
COLUMNS = ["ID", "Дата", "Старт", "UTC-смещение", "Название", "Место", "Широта", "Долгота",
           "Км", "Мин", "Темп /км", "Пульс ср", "Pa:HR %", "Ровная",
           "Температура °C", "Влажность %", "Точка росы °C", "Ветер м/с",
           "Погода: источник", "Станция", "До станции км", "WBGT °C"]


def _config() -> tuple[str, str]:
    return os.environ.get("ARCHIVE_URL", "").strip(), os.environ.get("ARCHIVE_TOKEN", "").strip()


def enabled() -> bool:
    return all(_config())


def upsert(rows: list[dict]) -> bool:
    """Adds or updates rows by ID. Returns True on success."""
    url, token = _config()
    # Apps Script answers a POST with a redirect to the result; requests follows it with GET
    resp = requests.post(url, json={"token": token, "rows": rows}, timeout=60)
    try:
        ok = resp.json().get("ok")
    except ValueError:
        ok = False
    if not ok:
        print(f"[archive] upsert failed: {resp.status_code} {resp.text[:200]}")
    return bool(ok)


def fetch(start: date, end: date) -> list[dict]:
    url, token = _config()
    resp = requests.get(url, params={"token": token, "from": start.isoformat(), "to": end.isoformat()}, timeout=60)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"archive fetch failed: {data.get('error')}")
    return data["rows"]


def _r(x, digits=1):
    return round(x, digits) if x is not None else None


def run_row(act: dict, drift=None, weather=None) -> dict:
    """One sheet row for a Garmin activity (summary dict), its drift and observed weather."""
    local = datetime.fromisoformat(act["startTimeLocal"])
    offset_min = round((local - datetime.fromisoformat(act["startTimeGMT"])).total_seconds() / 60)
    km = (act.get("distance") or 0) / 1000
    sec = act.get("duration") or 0
    pace = round(sec / km) if km else None
    return {
        "ID": str(act["activityId"]),
        "Дата": local.date().isoformat(),
        "Старт": local.strftime("%H:%M"),
        "UTC-смещение": f"{'+' if offset_min >= 0 else '-'}{abs(offset_min) // 60:02d}:{abs(offset_min) % 60:02d}",
        "Название": act.get("activityName") or "",
        "Место": act.get("locationName") or "",
        "Широта": _r(act.get("startLatitude"), 3),     # ~100 m: enough to see moves, no exact route
        "Долгота": _r(act.get("startLongitude"), 3),
        "Км": _r(km, 2),
        "Мин": _r(sec / 60),
        "Темп /км": f"{pace // 60}:{pace % 60:02d}" if pace else "",
        "Пульс ср": _r(act.get("averageHR"), 0),
        "Pa:HR %": _r(drift.decoupling) if drift else None,
        "Ровная": ("да" if drift.steady else "нет") if drift else "",
        "Температура °C": _r(weather.temp_c) if weather else None,
        "Влажность %": _r(weather.humidity, 0) if weather else None,
        "Точка росы °C": _r(weather.dew_point_c) if weather else None,
        "Ветер м/с": _r(weather.wind_ms) if weather else None,
        "Погода: источник": weather.source if weather else "",
        "Станция": weather.station if weather else "",
        "До станции км": _r(weather.station_km) if weather else None,
        "WBGT °C": _r(weather.wbgt_c) if weather and weather.wbgt_c is not None else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backfill", type=date.fromisoformat, required=True, help="archive runs since this date")
    args = parser.parse_args()
    if not enabled():
        sys.exit("Set ARCHIVE_URL and ARCHIVE_TOKEN in .env")

    import cardiac_drift
    import weather
    from plan_sources import garmin_client
    client = garmin_client()
    acts = [a for a in client.get_activities_by_date(args.backfill.isoformat(), date.today().isoformat())
            if "run" in (a.get("activityType") or {}).get("typeKey", "")]
    rows = []
    for a in sorted(acts, key=lambda a: a["startTimeGMT"]):
        km = (a.get("distance") or 0) / 1000
        drift = cardiac_drift.for_activity(client, a["activityId"], a.get("activityName") or "", km)
        rows.append(run_row(a, drift, weather.for_activity(client, a)))
        print(f"  {rows[-1]['Дата']} {rows[-1]['Км']} км  Pa:HR {rows[-1]['Pa:HR %']}  "
              f"WBGT {rows[-1]['WBGT °C']}  {rows[-1]['Погода: источник']} {rows[-1]['Станция']}")
    for i in range(0, len(rows), 50):
        if not upsert(rows[i:i + 50]):
            sys.exit("Archive write failed")
    print(f"Archived {len(rows)} runs.")


if __name__ == "__main__":
    main()
