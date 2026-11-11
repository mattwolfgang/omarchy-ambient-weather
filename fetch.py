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
  fetch.py --paste clipboard|primary         bounded clipboard read for the settings view

Every input is bounded: HTTP bodies, files and stdin by size, network
requests by an absolute deadline, and the strings passed on to the panel by
length, so the JSON this prints is always small.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import selectors
import signal
import stat
import subprocess
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
CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f]+")

# Size, time and length limits.
HTTP_LIMIT = 2 * 1024 * 1024     # bytes in one HTTP response body
FILE_LIMIT = 64 * 1024           # bytes in config.json, the cache, Omarchy's location file
STDIN_LIMIT = 16 * 1024          # bytes of settings on stdin
OUTPUT_LIMIT = 256 * 1024        # bytes of JSON printed for the panel
PASTE_LIMIT = 4096               # bytes read from the clipboard
PASTE_TIMEOUT = 2.0              # seconds to wait for the clipboard
MAX_DEVICES = 50                 # Ambient stations looked at
MAX_PERIODS = 8                  # forecast periods shown
MAX_PLACES = 6                   # place search results
KEY_MAX = 256                    # characters in an Ambient key
TEXT_MAX = 200                   # characters in any other string
NWS_HOST = "api.weather.gov"

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

class TimedOut(Exception):
    pass


class TooLarge(Exception):
    pass


def _on_alarm(signum, frame):
    raise TimedOut()


def get_json(url: str, timeout: float = 10.0, limit: int = HTTP_LIMIT):
    """GET url as JSON within an absolute deadline and a body size limit.

    The socket timeout only bounds each read, so an interval timer raises
    TimedOut once `timeout` seconds have passed in total. (A DNS lookup can
    outlast it; the panel kills this script at its own deadline as well.)"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            length = resp.headers.get("Content-Length", "")
            if length.isdigit() and int(length) > limit:
                raise TooLarge()
            body = bytearray()
            while True:
                chunk = resp.read(min(65536, limit + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
                if len(body) > limit:
                    raise TooLarge()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return json.loads(body)


def failure(exc: Exception) -> str:
    if isinstance(exc, TimedOut):
        return "timed out"
    if isinstance(exc, TooLarge):
        return "reply too large"
    return type(exc).__name__


def num(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def text(value, limit: int = TEXT_MAX) -> str:
    """A display string: control characters flattened, length capped."""
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return CONTROL_RE.sub(" ", str(value)[:limit * 2]).strip()[:limit]


def as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value) -> list:
    return value if isinstance(value, list) else []


def read_json_file(path: Path, limit: int = FILE_LIMIT) -> dict:
    """A JSON object from a regular file of at most `limit` bytes, else {}."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return {}
    try:
        with os.fdopen(fd, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                return {}
            raw = fh.read(limit + 1)
    except OSError:
        return {}
    if len(raw) > limit:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    return as_dict(data)


# ---------------------------------------------------------------- config

def load_config(path: Path) -> dict:
    data = read_json_file(path)
    config = {}
    for key in CONFIG_KEYS:
        value = data.get(key)
        if key in ("latitude", "longitude"):
            value = num(value)
        elif isinstance(value, str):
            value = value.strip()[:KEY_MAX if key in SECRET_KEYS else TEXT_MAX]
        else:
            value = None
        if value not in (None, ""):
            config[key] = value
    return config


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
        data = as_dict(get_json(f"http://{host}/get_network_info", timeout=3, limit=FILE_LIMIT))
        return normalize_mac(text(data.get("mac", ""), 64))
    except Exception:
        return None


def save_config(path: Path, incoming: dict) -> dict:
    config = load_config(path)
    notes = []

    for key in SECRET_KEYS:  # blank means "keep the saved key"
        value = str(incoming.get(key) or "").strip()
        if value:
            if len(value) > KEY_MAX or not re.fullmatch(r"[\x21-\x7e]+", value):
                raise ValueError("Keys are single words of printable characters")
            config[key] = value

    for key, normalize in (("deviceMac", normalize_mac), ("stationIp", normalize_host)):
        value = str(incoming.get(key) or "").strip()[:TEXT_MAX]
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

    lat = str(incoming.get("latitude") or "").strip()[:64]
    lon = str(incoming.get("longitude") or "").strip()[:64]
    if lat or lon:
        try:
            lat_f, lon_f = float(lat), float(lon)
        except (ValueError, OverflowError):
            raise ValueError("Latitude and longitude must both be numbers")
        if not (-90 <= lat_f <= 90 and -180 <= lon_f <= 180):
            raise ValueError("Latitude must be -90…90 and longitude -180…180")
        config["latitude"], config["longitude"] = round(lat_f, 4), round(lon_f, 4)
    else:
        config.pop("latitude", None)
        config.pop("longitude", None)

    name = text(incoming.get("locationName"))
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
        return {"error": f"Ambient request failed ({failure(exc)})"}, None
    devices = [d for d in as_list(devices)[:MAX_DEVICES] if isinstance(d, dict)]
    if not devices:
        return {"error": "No stations on this Ambient account"}, None

    wanted_mac = str(config.get("deviceMac", "")).lower()
    device = devices[0]
    if wanted_mac:
        device = next((d for d in devices if text(d.get("macAddress"), 64).lower() == wanted_mac), device)

    data = as_dict(device.get("lastData"))
    info = as_dict(device.get("info"))
    coords = as_dict(as_dict(info.get("coords")).get("coords"))
    lat, lon = num(coords.get("lat")), num(coords.get("lon"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat = lon = None
    observed = num(data.get("dateutc"))
    station = {
        "name": text(info.get("name"), 80) or text(info.get("location"), 80) or "Weather Station",
        "observedAt": int(observed) if observed is not None and 0 < observed < 1e14 else None,
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
        return float(config["latitude"]), float(config["longitude"]), text(config.get("locationName"), 120)
    shared = read_json_file(OMARCHY_LOCATION)
    if num(shared.get("latitude")) is not None and num(shared.get("longitude")) is not None:
        return float(shared["latitude"]), float(shared["longitude"]), text(shared.get("name"), 120)
    if station_coords:
        return station_coords[0], station_coords[1], ""
    return None


def nws_forecast_url(lat: float, lon: float) -> tuple[str, str]:
    """Resolve (and cache) the NWS gridpoint forecast URL for a coordinate."""
    cache_file = CACHE_DIR / f"nws-point-{lat:.4f},{lon:.4f}.json"
    cached = read_json_file(cache_file)
    saved_at = num(cached.get("savedAt")) or 0
    if 0 <= time.time() - saved_at < 7 * 86400 and is_nws_url(cached.get("forecast")):
        return cached["forecast"], text(cached.get("place"), 120)

    props = as_dict(as_dict(get_json(NWS_POINTS_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))).get("properties"))
    rel = as_dict(as_dict(props.get("relativeLocation")).get("properties"))
    place = ", ".join(p for p in (text(rel.get("city"), 80), text(rel.get("state"), 40)) if p) or f"{lat:.2f}, {lon:.2f}"
    forecast = props.get("forecast")
    if not is_nws_url(forecast):
        raise ValueError("unexpected forecast URL")
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"forecast": forecast, "place": place, "savedAt": time.time()}))
    except OSError:
        pass
    return forecast, place


def is_nws_url(url) -> bool:
    """Only follow forecast links that stay on the NWS API over HTTPS."""
    if not isinstance(url, str) or len(url) > 512:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == NWS_HOST


def glyph_for(icon_url: str, is_day: bool) -> str:
    # e.g. https://api.weather.gov/icons/land/day/tsra_sct,40/sct?size=medium
    # path parts: ["", "icons", "land", "day", "tsra_sct,40", "sct"]; the
    # first code describes the start of the period.
    path = urllib.parse.urlparse(text(icon_url, 512)).path.split("/")
    code = path[4].split(",")[0] if len(path) > 4 else ""
    day_glyph, night_glyph = GLYPHS[NWS_ICON_FAMILY.get(code, "cloudy")]
    return day_glyph if is_day else night_glyph


def fetch_forecast(location) -> dict:
    if not location:
        return {"error": "Set a location in settings"}
    lat, lon, name = location
    try:
        url, place = nws_forecast_url(lat, lon)
        props = as_dict(as_dict(get_json(url, timeout=15)).get("properties"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"error": "The NWS forecast only covers US locations"}
        return {"error": f"NWS forecast unavailable (HTTP {exc.code})"}
    except Exception as exc:
        return {"error": f"NWS forecast unavailable ({failure(exc)})"}

    periods = []
    for p in as_list(props.get("periods"))[:MAX_PERIODS]:
        p = as_dict(p)
        temp = num(p.get("temperature"))
        if temp is None:
            continue
        pop = num(as_dict(p.get("probabilityOfPrecipitation")).get("value"))
        is_day = p.get("isDaytime") is True
        periods.append({
            "name": text(p.get("name"), 40),
            "isDaytime": is_day,
            "temp": round(temp),
            "short": text(p.get("shortForecast"), 120),
            "precip": round(max(0, min(100, pop))) if pop is not None else 0,
            "icon": glyph_for(p.get("icon"), is_day),
        })
    updated = text(props.get("updateTime"), 40) or text(props.get("generatedAt"), 40)
    return {"place": text(name, 120) or place, "updated": updated, "periods": periods}


# ---------------------------------------------------------------- geocoding

def geocode(query: str) -> dict:
    query = text(query, 100)
    if len(query) < 2:
        return {"error": "Type at least two characters", "results": []}
    url = GEOCODE_URL + "?" + urllib.parse.urlencode({"name": query, "count": MAX_PLACES, "language": "en", "format": "json"})
    try:
        results = as_list(as_dict(get_json(url, timeout=8)).get("results"))
    except Exception as exc:
        return {"error": f"Location search failed ({failure(exc)})", "results": []}
    out = []
    for r in results[:MAX_PLACES]:
        r = as_dict(r)
        lat, lon = num(r.get("latitude")), num(r.get("longitude"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        country = None if r.get("country_code") == "US" else text(r.get("country"), 60)
        parts = [text(r.get("name"), 80), text(r.get("admin1"), 60), country]
        out.append({
            "name": ", ".join(p for p in parts if p),
            "latitude": round(lat, 4),
            "longitude": round(lon, 4),
        })
    return {"results": out}


# ---------------------------------------------------------------- clipboard

def read_clipboard(selection: str) -> dict:
    """At most PASTE_LIMIT bytes of clipboard text within PASTE_TIMEOUT seconds.

    The panel's text fields paste through this instead of letting Qt read the
    clipboard itself, which has no size or time limit. wl-paste runs in its
    own process group, killed when this returns."""
    cmd = ["wl-paste", "--no-newline", "--type", "text"] + (["--primary"] if selection == "primary" else [])
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        return {"ok": False, "error": "Couldn't read the clipboard; wl-paste (wl-clipboard) is needed"}
    deadline = time.monotonic() + PASTE_TIMEOUT
    out = bytearray()
    error = None
    try:
        fd = proc.stdout.fileno()
        with selectors.DefaultSelector() as sel:
            sel.register(fd, selectors.EVENT_READ)
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    error = "The clipboard didn't answer in time"
                    break
                if not sel.select(left):
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out += chunk
                if len(out) > PASTE_LIMIT:
                    error = f"The clipboard holds more than {PASTE_LIMIT} bytes, too much for this field"
                    break
        if error is None:
            try:
                if proc.wait(timeout=max(0.05, deadline - time.monotonic())) != 0:
                    error = "The clipboard has no text"
            except subprocess.TimeoutExpired:
                error = "The clipboard didn't answer in time"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.stdout.close()
        proc.wait()
    if error:
        return {"ok": False, "error": error}
    return {"ok": True, "text": CONTROL_RE.sub(" ", out.decode("utf-8", "replace")).strip()}


# ---------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--show-config", action="store_true")
    mode.add_argument("--save-config", action="store_true")
    mode.add_argument("--geocode", metavar="QUERY")
    mode.add_argument("--paste", choices=("clipboard", "primary"))
    args = parser.parse_args()
    config_path = Path(os.path.expanduser(args.config))

    if args.show_config:
        out = {"config": config_summary(load_config(config_path))}
    elif args.save_config:
        try:
            line = sys.stdin.readline(STDIN_LIMIT + 1)
            if len(line) > STDIN_LIMIT:
                raise ValueError("Settings are too long")
            try:
                incoming = json.loads(line or "{}")
            except RecursionError:
                raise ValueError("Settings must be a JSON object")
            if not isinstance(incoming, dict):
                raise ValueError("Settings must be a JSON object")
            out = save_config(config_path, incoming)
        except (ValueError, OSError) as exc:
            out = {"ok": False, "message": str(exc)}
    elif args.geocode is not None:
        out = geocode(args.geocode)
    elif args.paste:
        out = read_clipboard(args.paste)
    else:
        config = load_config(config_path)
        station, station_coords = fetch_station(config)
        out = {
            "station": station,
            "forecast": fetch_forecast(resolve_location(config, station_coords)),
            "fetchedAt": int(time.time()),
        }
    encoded = json.dumps(out, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > OUTPUT_LIMIT:  # can't happen with the limits above; a backstop
        encoded = json.dumps({"error": "Result too large"})
    sys.stdout.write(encoded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
