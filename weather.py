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

WBGT is the main heat index. Stations don't measure the globe (sun) term, so
it is estimated: 0.7 × natural wet bulb (Stull 2011, from temperature and
humidity) + 0.3 × air temperature + a sun term from the solar elevation at
the run's place and middle time, assuming clear sky. Clouds are ignored, so
on overcast days it errs high, on the safe side; expect about ±1.5 °C.
Flags follow ACSM's WBGT guidance for running.
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


def wet_bulb_c(temp_c: float, rh: float) -> float:
    """Wet-bulb temperature from air temperature and relative humidity (Stull 2011, ±1 °C)."""
    return (temp_c * math.atan(0.151977 * (rh + 8.313659) ** 0.5) + math.atan(temp_c + rh)
            - math.atan(rh - 1.676331) + 0.00391838 * rh ** 1.5 * math.atan(0.023101 * rh) - 4.686035)


def sun_elevation_deg(lat: float, lon: float, when: datetime) -> float:
    """Solar elevation above the horizon (NOAA approximation, ±1°)."""
    when = when.astimezone(timezone.utc)
    hour = when.hour + when.minute / 60
    g = 2 * math.pi / 365 * (when.timetuple().tm_yday - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    hour_angle = math.radians((hour * 60 + eqtime + 4 * lon) / 4 - 180)
    lat_r = math.radians(lat)
    sin_elev = math.sin(lat_r) * math.sin(decl) + math.cos(lat_r) * math.cos(decl) * math.cos(hour_angle)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_elev))))


def wbgt_c(temp_c: float, rh: float, sun_elev: float | None) -> float:
    # Clear-sky irradiance falls off at low sun (longer path through the air)
    s = math.sin(math.radians(sun_elev)) if sun_elev else 0
    irradiance = 1098 * s * math.exp(-0.057 / s) if s > 0.05 else 0
    # Globe runs ~0.0125 °C per W/m² above air temperature; its WBGT weight is 0.2
    return 0.7 * wet_bulb_c(temp_c, rh) + 0.3 * temp_c + 0.0025 * irradiance


def wbgt_flag(wbgt: float) -> str:
    """ACSM heat-risk categories for running."""
    return "🟢" if wbgt < 18 else "🟡" if wbgt < 23 else "🔴" if wbgt < 28 else "⚫"


@dataclass
class Weather:
    temp_c: float
    humidity: float | None
    wind_ms: float | None
    source: str             # "AEMET" / "Garmin"
    station: str
    station_km: float
    sun_elev: float | None = None   # at the run's middle, degrees

    @property
    def dew_point_c(self) -> float | None:
        if self.humidity is None or self.humidity <= 0:
            return None
        a, b = 17.62, 243.12
        g = math.log(self.humidity / 100) + a * self.temp_c / (b + self.temp_c)
        return b * g / (a - g)

    @property
    def wbgt_c(self) -> float | None:
        return wbgt_c(self.temp_c, self.humidity, self.sun_elev) if self.humidity else None

    def line(self) -> str:
        """Message line: WBGT first; the station is kept out of the message (it is in the archive)."""
        rest = f"{self.temp_c:.0f}°C" + (f", влажность {self.humidity:.0f}%" if self.humidity is not None else "")
        if self.wbgt_c is None:
            return f"🌡 {rest}"
        return f"🌡 WBGT {self.wbgt_c:.0f}°C {wbgt_flag(self.wbgt_c)} · {rest}"


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
        if datetime.now(timezone.utc) - start > timedelta(hours=24):
            return None   # AEMET keeps only 24 h: don't spend requests (it rate-limits bursts)
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
    if not candidates:
        return None
    best = min(candidates, key=lambda w: w.station_km)
    if lat is not None:
        best.sun_elev = sun_elevation_deg(lat, lon, start + (end - start) / 2)
    return best
