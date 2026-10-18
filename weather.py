"""
Observed (not forecast) weather for a run, from the nearest real station.

Providers, nearest station wins:
- AEMET (Spain's met service): ~900 automatic stations with hourly
  observations, but only for the last 24 hours. Used when one of its stations
  is within MAX_STATION_KM of the start point.
- Garmin: the observation Garmin attaches to every activity (METAR of the
  nearest airport). Works in any country, so moving abroad needs no change —
  it is simply the only provider left when no AEMET station is near.
Another country's network can be added as one more provider with `observe()`.

Dew point is computed (Magnus formula): for runners it is the best single
measure of heat stress, better than temperature or humidity alone.
"""

import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

MAX_STATION_KM = 25
OBS_MARGIN = timedelta(minutes=30)   # hourly observations just before the start / after the end count


@dataclass
class Weather:
    temp_c: float
    humidity: float | None
    wind_ms: float | None
    source: str             # "AEMET" / "Garmin"
    station: str
    station_km: float

    @property
    def dew_point_c(self) -> float | None:
        if self.humidity is None or self.humidity <= 0:
            return None
        a, b = 17.62, 243.12
        g = math.log(self.humidity / 100) + a * self.temp_c / (b + self.temp_c)
        return b * g / (a - g)

    def line(self) -> str:
        parts = [f"{self.temp_c:.0f}°C"]
        if self.humidity is not None:
            parts.append(f"влажность {self.humidity:.0f}%")
        if self.dew_point_c is not None:
            parts.append(f"точка росы {self.dew_point_c:.0f}°C")
        return f"🌡 {', '.join(parts)} · {self.source} {self.station} ({self.station_km:.0f} км)"


def km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _mean(values: list) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


# ── AEMET ────────────────────────────────────────────────────────────────────

class Aemet:
    BASE = "https://opendata.aemet.es/opendata/api/"

    def __init__(self, api_key: str):
        self.headers = {"api_key": api_key}
        self._stations: list[dict] | None = None

    def _get(self, path: str):
        # Two steps: the API answers with a temporary URL that holds the data
        meta = requests.get(self.BASE + path, headers=self.headers, timeout=60).json()
        if meta.get("estado") != 200 or not meta.get("datos"):
            raise RuntimeError(f"AEMET {path}: {meta.get('estado')} {meta.get('descripcion')}")
        return requests.get(meta["datos"], timeout=60).json()

    @staticmethod
    def _dms(s: str) -> float:      # "362824N" → 36.4733
        v = int(s[:-1])
        x = v // 10000 + v // 100 % 100 / 60 + v % 100 / 3600
        return -x if s[-1] in "SW" else x

    def nearest(self, lat: float, lon: float) -> tuple[dict, float]:
        if self._stations is None:
            self._stations = [{"id": s["indicativo"], "name": s["nombre"].title(),
                               "lat": self._dms(s["latitud"]), "lon": self._dms(s["longitud"])}
                              for s in self._get("valores/climatologicos/inventarioestaciones/todasestaciones")]
        return min(((s, km_between(lat, lon, s["lat"], s["lon"])) for s in self._stations), key=lambda x: x[1])

    def observe(self, lat: float, lon: float, start: datetime, end: datetime) -> Weather | None:
        station, dist = self.nearest(lat, lon)
        if dist > MAX_STATION_KM:
            return None   # e.g. abroad: no Spanish station anywhere near
        obs = [o for o in self._get(f"observacion/convencional/datos/estacion/{station['id']}")
               if start - OBS_MARGIN <= datetime.fromisoformat(o["fint"]) <= end + OBS_MARGIN]
        temp = _mean([o.get("ta") for o in obs])
        if temp is None:
            return None   # older than 24 h, or not published yet (~2 h delay)
        return Weather(temp, _mean([o.get("hr") for o in obs]), _mean([o.get("vv") for o in obs]),
                       "AEMET", station["name"], dist)


# ── Garmin ───────────────────────────────────────────────────────────────────

def garmin_observe(client, activity_id, lat: float | None, lon: float | None) -> Weather | None:
    w = client.get_activity_weather(str(activity_id)) or {}
    if w.get("temp") is None:
        return None
    station = w.get("weatherStationDTO") or {}
    dist = km_between(lat, lon, w["latitude"], w["longitude"]) if lat is not None and w.get("latitude") else 0
    wind = w.get("windSpeed")
    return Weather((w["temp"] - 32) * 5 / 9,                     # Garmin reports °F and mph
                   w.get("relativeHumidity"),
                   wind * 0.44704 if wind is not None else None,
                   "Garmin", station.get("name") or station.get("id") or "?", dist)


# ── Entry point ──────────────────────────────────────────────────────────────

_aemet: Aemet | None = None


def for_activity(client, activity: dict) -> Weather | None:
    """Observed weather during a Garmin activity (summary dict from the activity list)."""
    global _aemet
    lat, lon = activity.get("startLatitude"), activity.get("startLongitude")
    start = datetime.fromisoformat(activity["startTimeGMT"]).replace(tzinfo=timezone.utc)
    end = start + timedelta(seconds=activity.get("elapsedDuration") or activity.get("duration") or 0)
    candidates = []
    key = os.environ.get("AEMET_API_KEY", "").strip()
    if key and lat is not None:
        try:
            _aemet = _aemet or Aemet(key)
            if w := _aemet.observe(lat, lon, start, end):
                candidates.append(w)
        except Exception as exc:
            print(f"AEMET weather unavailable: {exc!r}")
    try:
        if w := garmin_observe(client, activity["activityId"], lat, lon):
            candidates.append(w)
    except Exception as exc:
        print(f"Garmin weather unavailable: {exc!r}")
    return min(candidates, key=lambda w: w.station_km) if candidates else None
