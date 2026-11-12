"""NFL game-time weather from Open-Meteo (free, no key). Indoor stadiums are skipped."""
from __future__ import annotations

from datetime import datetime, timezone

import requests

from .names import norm_team

# team -> (lat, lon, roof). "dome" and "retractable" are treated as weather-proof.
# Neutral-site / international games will be wrong: the home team's stadium is assumed.
STADIUMS = {norm_team(k): v for k, v in {
    "Arizona Cardinals": (33.5276, -112.2626, "retractable"), "Atlanta Falcons": (33.7554, -84.4008, "retractable"),
    "Baltimore Ravens": (39.2780, -76.6227, "open"), "Buffalo Bills": (42.7738, -78.7870, "open"),
    "Carolina Panthers": (35.2258, -80.8528, "open"), "Chicago Bears": (41.8623, -87.6167, "open"),
    "Cincinnati Bengals": (39.0954, -84.5160, "open"), "Cleveland Browns": (41.5061, -81.6995, "open"),
    "Dallas Cowboys": (32.7473, -97.0945, "retractable"), "Denver Broncos": (39.7439, -105.0201, "open"),
    "Detroit Lions": (42.3400, -83.0456, "dome"), "Green Bay Packers": (44.5013, -88.0622, "open"),
    "Houston Texans": (29.6847, -95.4107, "retractable"), "Indianapolis Colts": (39.7601, -86.1639, "retractable"),
    "Jacksonville Jaguars": (30.3239, -81.6373, "open"), "Kansas City Chiefs": (39.0489, -94.4839, "open"),
    "Las Vegas Raiders": (36.0909, -115.1833, "dome"), "Los Angeles Chargers": (33.9535, -118.3392, "dome"),
    "Los Angeles Rams": (33.9535, -118.3392, "dome"), "Miami Dolphins": (25.9580, -80.2389, "open"),
    "Minnesota Vikings": (44.9736, -93.2575, "dome"), "New England Patriots": (42.0909, -71.2643, "open"),
    "New Orleans Saints": (29.9511, -90.0812, "dome"), "New York Giants": (40.8135, -74.0745, "open"),
    "New York Jets": (40.8135, -74.0745, "open"), "Philadelphia Eagles": (39.9008, -75.1675, "open"),
    "Pittsburgh Steelers": (40.4468, -80.0158, "open"), "San Francisco 49ers": (37.4033, -121.9694, "open"),
    "Seattle Seahawks": (47.5952, -122.3316, "open"), "Tampa Bay Buccaneers": (27.9759, -82.5033, "open"),
    "Tennessee Titans": (36.1665, -86.7713, "open"), "Washington Commanders": (38.9076, -76.8645, "open"),
}.items()}

WIND_MPH, GUST_MPH, PRECIP_IN_HR = 15.0, 25.0, 0.05


class WeatherClient:
    URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, timeout: float = 10):
        self.s = requests.Session()
        self.timeout = timeout

    def forecast(self, lat: float, lon: float) -> dict:
        r = self.s.get(self.URL, timeout=self.timeout, params={
            "latitude": lat, "longitude": lon, "timezone": "UTC", "forecast_days": 7,
            "hourly": "temperature_2m,precipitation,wind_speed_10m,wind_gusts_10m",
            "wind_speed_unit": "mph", "temperature_unit": "fahrenheit", "precipitation_unit": "inch"})
        r.raise_for_status()
        return r.json()


def game_weather(payload: dict, kickoff_ts: float) -> dict | None:
    """Conditions for the hour ~1.5h after kickoff (mid-game)."""
    h = payload.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    target = kickoff_ts + 5400
    stamps = [datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp() for t in times]
    i = min(range(len(stamps)), key=lambda j: abs(stamps[j] - target))
    if abs(stamps[i] - target) > 3 * 3600:
        return None
    w = {"wind": h["wind_speed_10m"][i], "gust": h["wind_gusts_10m"][i],
         "precip": h["precipitation"][i], "temp": h["temperature_2m"][i]}
    bad = []
    if (w["wind"] or 0) >= WIND_MPH or (w["gust"] or 0) >= GUST_MPH:
        bad.append(f"wind {w['wind']:.0f} mph (gusts {w['gust']:.0f})")
    if (w["precip"] or 0) >= PRECIP_IN_HR:
        bad.append(f"precip {w['precip']:.2f} in/hr")
    w["flag"] = ", ".join(bad)
    return w
