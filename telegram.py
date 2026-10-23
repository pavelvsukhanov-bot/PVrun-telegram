"""Telegram helpers: sending messages and formatting a run card."""

import html
import os

import requests
from dotenv import load_dotenv

from running_dynamics import color

load_dotenv()


def tg_call(method: str, **payload) -> bool:
    """Calls a Bot API method in the configured chat. Returns True on success."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("[TG] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — skipping send")
        return False
    url = f"https://api.telegram.org/bot{token}/{method}"
    resp = requests.post(url, json={"chat_id": chat_id, **payload}, timeout=10)
    if not resp.ok:
        print(f"[TG] {method} failed: {resp.status_code} {resp.text}")
    return resp.ok


def tg_send(text: str, reply_markup: dict | None = None) -> bool:
    """Returns True if Telegram accepted the message."""
    payload = {"text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg_call("sendMessage", **payload)


def format_run(row, drift_text: str | None = None, weather_line: str | None = None) -> str:
    """Run card. drift_text: Pa:HR or why drift was not measured; weather_line: observed heat (WBGT)."""
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
        f"{html.escape(row['name'] or '', quote=False)}",
        "",
        f"Дистанция:       {dist_km:.2f} км",
        f"Время:           {duration_str}",
        f"Темп:            {pace(row['avg_pace_s_km'])}",
        f"Пульс ср/макс:   {int(row['avg_hr'] or 0)} / {int(row['max_hr'] or 0)} уд/мин",
        f"Калории:         {int(row['calories'] or 0)} ккал",
        "",
        "<b>Беговая динамика</b>",
        # Colours as in Garmin Connect (stride length has no band)
        f"Каденс:          {val(row['avg_cadence'], ' ш/мин', 0)} {color('cadence', row['avg_cadence'])}",
        f"ВКЗ:             {val(row['avg_gct_ms'], ' мс', 0)} {color('gct', row['avg_gct_ms'])}",
        f"Длина шага:      {val(row['avg_stride_length_cm'], ' см', 0)}",
        f"Верт. колебание: {val(row['avg_vert_osc_cm'], ' см')} {color('vert_osc', row['avg_vert_osc_cm'])}",
        f"Верт. соотношение:{val(row['avg_vert_ratio_pct'], '%')} {color('vert_ratio', row['avg_vert_ratio_pct'])}",
    ]
    if weather_line or drift_text:
        lines.append("")
    if weather_line:
        lines.append(html.escape(weather_line, quote=False))
    if drift_text:
        lines.append(f"<b>Кардиодрифт:</b> {html.escape(drift_text, quote=False)}")
    return "\n".join(lines)
