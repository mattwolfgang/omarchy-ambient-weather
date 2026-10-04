#!/usr/bin/env python3
"""Ambient Weather station readings and the NWS forecast as one JSON blob.

Everything user-specific (Ambient keys, station MAC/IP, location) lives in
config.json next to this script. That file is written by the panel's
settings view (--save-config), is gitignored, and is created owner-only.
The forecast comes from the National Weather Service, which needs no key.

Usage:
  fetch.py [--config PATH]                   fetch station + forecast
  fetch.py --show-config                     non-secret config summary
  fetch.py --save-config < settings.json     merge settings (one JSON line)
  fetch.py --geocode QUERY                   place search for the settings view
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PLUGIN_ID = "io.github.mattwolfgang.ambient-weather"
DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.json"
# Omarchy's shared weather location (set from the built-in weather panel).
OMARCHY_LOCATION = Path.home() / ".local" / "state" / "omarchy" / "settings" / "weather.json"

USER_AGENT = f"omarchy-ambient-weather-plugin ({PLUGIN_ID})"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / PLUGIN_ID
AMBIENT_DEVICES_URL = "https://api.ambientweather.net/v1/devices"
NWS_POINTS_URL = "https://api.weather.gov/points/{lat},{lon}"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

CONFIG_KEYS = ("apiKey", "applicationKey", "deviceMac", "stationIp", "locationName", "latitude", "longitude")
SECRET_KEYS = ("apiKey", "applicationKey")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

# Nerd-font weather glyphs, matching the built-in omarchy.weather widget.
GLYPHS = {
    "clear": ("\ue30d", "\ue32b"),
    "partly": ("\ue302", "\ue32e"),
    "cloudy": ("\ue33d", "\ue33d"),
    "fog": ("\ue313", "\ue346"),
    "drizzle": ("\ue308", "\ue333"),
    "rain": ("\ue318", "\ue318"),
    "snow": ("\ue31a", "\ue31a"),
    "sleet": ("\ue3ad", "\ue3ad"),
    "storm": ("\ue31d", "\ue31d"),
    "wind": ("\ue34b", "\ue34b"),
    "hot": ("\ue30d", "\ue32b"),
    "cold": ("\ue33d", "\ue33d"),
}

# NWS icon code (from the icon URL path) -> glyph family.
NWS_ICON_FAMILY = {
    "skc": "clear", "few": "clear", "hot": "hot", "cold": "cold",
    "sct": "partly", "bkn": "partly",
    "ovc": "cloudy",
    "wind_skc": "wind", "wind_few": "wind", "wind_sct": "wind", "wind_bkn": "wind", "wind_ovc": "wind",
    "fog": "fog", "haze": "fog", "smoke": "fog", "dust": "fog",
    "rain": "rain", "rain_showers": "rain", "rain_showers_hi": "drizzle",
    "snow": "snow", "blizzard": "snow", "rain_snow": "sleet",
    "rain_sleet": "sleet", "snow_sleet": "sleet", "sleet": "sleet",
    "fzra": "sleet", "rain_fzra": "sleet", "snow_fzra": "sleet",
    "tsra": "storm", "tsra_sct": "storm", "tsra_hi": "storm",
    "tornado": "storm", "hurricane": "storm", "tropical_storm": "storm",
}


# ---------------------------------------------------------------- helpers

def get_json(url: str, timeout: float = 10.0):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_json_file(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    data = read_json_file(path)
    return {k: data.get(k) for k in CONFIG_KEYS if data.get(k) not in (None, "")}


def write_config(path: Path, config: dict) -> None:
    """Atomically write config.json, readable only by its owner."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def config_summary(config: dict) -> dict:
    """What the settings view may see: everything except the key values."""
    summary = {k: config.get(k, "") for k in CONFIG_KEYS if k not in SECRET_KEYS}
    for k in ("latitude", "longitude"):
        summary[k] = "" if config.get(k) is None else str(config[k])
    summary["hasApiKey"] = bool(config.get("apiKey"))
    summary["hasApplicationKey"] = bool(config.get("applicationKey"))
    return summary


def normalize_mac(raw: str) -> str:
    mac = re.sub(r"[^0-9A-Fa-f]", "", raw or "").upper()
    if len(mac) != 12:
        raise ValueError("Station MAC must look like AA:BB:CC:DD:EE:FF")
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def normalize_host(raw: str) -> str:
    host = re.sub(r"^https?://", "", (raw or "").strip()).split("/")[0]
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            raise ValueError("Station IP must be an address like 192.168.1.50")
    return host


def station_mac_from_ip(host: str) -> str | None:
    """Ask the console for its MAC. AMBWeatherPro-firmware consoles (e.g. the
    WS-2902) answer /get_network_info on the LAN; others simply don't."""
    try:
        data = get_json(f"http://{host}/get_network_info", timeout=3)
        return normalize_mac(str(data.get("mac", "")))
    except Exception:
        return None


def save_config(path: Path, incoming: dict) -> dict:
    config = load_config(path)
    notes = []

    for key in SECRET_KEYS:  # blank means "keep the saved key"
        value = str(incoming.get(key) or "").strip()
        if value:
            config[key] = value

    for key, normalize in (("deviceMac", normalize_mac), ("stationIp", normalize_host)):
        value = str(incoming.get(key) or "").strip()
        if value:
            config[key] = normalize(value)
        else:
            config.pop(key, None)

    if config.get("stationIp") and not config.get("deviceMac"):
        mac = station_mac_from_ip(config["stationIp"])
        if mac:
            config["deviceMac"] = mac
            notes.append(f"Found station MAC {mac}")
        else:
            notes.append(f"Couldn't read a MAC from {config['stationIp']}; using the first station on the account")

    lat = str(incoming.get("latitude") or "").strip()
    lon = str(incoming.get("longitude") or "").strip()
    if lat or lon:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except ValueError:
            raise ValueError("Latitude and longitude must both be numbers")
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            raise ValueError("Latitude must be -90…90 and longitude -180…180")
        config["latitude"], config["longitude"] = round(lat_f, 4), round(lon_f, 4)
    else:
        config.pop("latitude", None)
        config.pop("longitude", None)

    name = str(incoming.get("locationName") or "").strip()
    if name and "latitude" in config:
        config["locationName"] = name
    else:
        config.pop("locationName", None)

    write_config(path, config)
    return {"ok": True, "message": "; ".join(notes) or "Saved", "config": config_summary(config)}


# ---------------------------------------------------------------- station

def fetch_station(config: dict) -> tuple[dict, tuple[float, float] | None]:
    """Return (station readings, station coordinates if Ambient knows them)."""
    api_key = config.get("apiKey", "")
    app_key = config.get("applicationKey", "")
    if not api_key or not app_key:
        return {"error": "Add your Ambient Weather keys in settings", "needsSetup": True}, None

    query = urllib.parse.urlencode({"apiKey": api_key, "applicationKey": app_key})
    try:
        devices = get_json(f"{AMBIENT_DEVICES_URL}?{query}", timeout=15)
    except urllib.error.HTTPError as exc:  # the message embeds the URL; never surface the keys
        if exc.code in (401, 403):
            return {"error": "Ambient rejected the keys; check them in settings", "needsSetup": True}, None
        return {"error": f"Ambient request failed (HTTP {exc.code})"}, None
    except Exception as exc:
        return {"error": f"Ambient request failed ({type(exc).__name__})"}, None
    if not devices:
        return {"error": "No stations on this Ambient account"}, None

    wanted_mac = str(config.get("deviceMac", "")).lower()
    device = devices[0]
    if wanted_mac:
        device = next((d for d in devices if str(d.get("macAddress", "")).lower() == wanted_mac), device)

    data = device.get("lastData") or {}
    info = device.get("info") or {}
    coords = ((info.get("coords") or {}).get("coords")) or {}
    lat, lon = num(coords.get("lat")), num(coords.get("lon"))
    observed = data.get("dateutc")
    station = {
        "name": info.get("name") or info.get("location") or "Weather Station",
        "observedAt": int(observed) if isinstance(observed, (int, float)) else None,
        "tempF": num(data.get("tempf")),
        "feelsLikeF": num(data.get("feelsLike")),
        "dewPointF": num(data.get("dewPoint")),
        "humidity": num(data.get("humidity")),
        "windMph": num(data.get("windspeedmph")),
        "gustMph": num(data.get("windgustmph")),
        "windDir": num(data.get("winddir")),
        "pressureInHg": num(data.get("baromrelin")),
        "rainDailyIn": num(data.get("dailyrainin")),
        "rainRateInHr": num(data.get("hourlyrainin")),
        "uv": num(data.get("uv")),
        "solarWm2": num(data.get("solarradiation")),
        "indoorTempF": num(data.get("tempinf")),
        "indoorHumidity": num(data.get("humidityin")),
    }
    return station, ((lat, lon) if lat is not None and lon is not None else None)


# ---------------------------------------------------------------- forecast

def resolve_location(config: dict, station_coords) -> tuple[float, float, str] | None:
    """Plugin settings, then Omarchy's weather location, then the station's own coordinates."""
    if num(config.get("latitude")) is not None and num(config.get("longitude")) is not None:
        return float(config["latitude"]), float(config["longitude"]), config.get("locationName", "")
    shared = read_json_file(OMARCHY_LOCATION)
    if num(shared.get("latitude")) is not None and num(shared.get("longitude")) is not None:
        return float(shared["latitude"]), float(shared["longitude"]), str(shared.get("name") or "")
    if station_coords:
        return station_coords[0], station_coords[1], ""
    return None


def nws_forecast_url(lat: float, lon: float) -> tuple[str, str]:
    """Resolve (and cache) the NWS gridpoint forecast URL for a coordinate."""
    cache_file = CACHE_DIR / f"nws-point-{lat:.4f},{lon:.4f}.json"
    try:
        cached = json.loads(cache_file.read_text())
        if time.time() - cached.get("savedAt", 0) < 7 * 86400:
            return cached["forecast"], cached["place"]
    except (OSError, ValueError, KeyError):
        pass

    props = get_json(NWS_POINTS_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))["properties"]
    rel = (props.get("relativeLocation") or {}).get("properties") or {}
    place = ", ".join(p for p in (rel.get("city"), rel.get("state")) if p) or f"{lat:.2f}, {lon:.2f}"
    forecast = props["forecast"]
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"forecast": forecast, "place": place, "savedAt": time.time()}))
    except OSError:
        pass
    return forecast, place


def glyph_for(icon_url: str, is_day: bool) -> str:
    # e.g. https://api.weather.gov/icons/land/day/tsra_sct,40/sct?size=medium
    # path parts: ["", "icons", "land", "day", "tsra_sct,40", "sct"]; the
    # first code describes the start of the period.
    path = urllib.parse.urlparse(icon_url or "").path.split("/")
    code = path[4].split(",")[0] if len(path) > 4 else ""
    day_glyph, night_glyph = GLYPHS[NWS_ICON_FAMILY.get(code, "cloudy")]
    return day_glyph if is_day else night_glyph


def fetch_forecast(location) -> dict:
    if not location:
        return {"error": "Set a location in settings"}
    lat, lon, name = location
    try:
        url, place = nws_forecast_url(lat, lon)
        props = get_json(url, timeout=15)["properties"]
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": "The NWS forecast only covers US locations"}
        return {"error": f"NWS forecast unavailable (HTTP {exc.code})"}
    except Exception as exc:
        return {"error": f"NWS forecast unavailable ({type(exc).__name__})"}

    periods = []
    for p in props.get("periods", [])[:8]:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        wind = " ".join(x for x in (p.get("windDirection"), p.get("windSpeed")) if x)
        periods.append({
            "name": p.get("name", ""),
            "isDaytime": bool(p.get("isDaytime")),
            "temp": p.get("temperature"),
            "short": p.get("shortForecast", ""),
            "detailed": p.get("detailedForecast", ""),
            "precip": pop if pop is not None else 0,
            "wind": wind,
            "icon": glyph_for(p.get("icon", ""), bool(p.get("isDaytime"))),
        })
    return {"place": name or place, "updated": props.get("updateTime") or props.get("generatedAt"), "periods": periods}


# ---------------------------------------------------------------- geocoding

def geocode(query: str) -> dict:
    url = GEOCODE_URL + "?" + urllib.parse.urlencode({"name": query, "count": 6, "language": "en", "format": "json"})
    try:
        results = get_json(url, timeout=8).get("results") or []
    except Exception as exc:
        return {"error": f"Location search failed ({type(exc).__name__})", "results": []}
    out = []
    for r in results:
        parts = [r.get("name"), r.get("admin1"), None if r.get("country_code") == "US" else r.get("country")]
        out.append({
            "name": ", ".join(p for p in parts if p),
            "latitude": round(float(r["latitude"]), 4),
            "longitude": round(float(r["longitude"]), 4),
        })
    return {"results": out}


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show-config", action="store_true")
    mode.add_argument("--save-config", action="store_true")
    mode.add_argument("--geocode", metavar="QUERY")
    args = parser.parse_args()
    config_path = Path(os.path.expanduser(args.config))

    if args.show_config:
        out = {"config": config_summary(load_config(config_path))}
    elif args.save_config:
        try:
            incoming = json.loads(sys.stdin.readline() or "{}")
            if not isinstance(incoming, dict):
                raise ValueError("Settings must be a JSON object")
            out = save_config(config_path, incoming)
        except (ValueError, OSError) as exc:
            out = {"ok": False, "message": str(exc)}
    elif args.geocode is not None:
        out = geocode(args.geocode.strip())
    else:
        config = load_config(config_path)
        station, station_coords = fetch_station(config)
        out = {
            "station": station,
            "forecast": fetch_forecast(resolve_location(config, station_coords)),
            "fetchedAt": int(time.time()),
        }
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
