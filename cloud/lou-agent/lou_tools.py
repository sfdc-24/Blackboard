"""Lou's tools as plain functions, so they test without LiveKit (lou_agent.py wraps them as @function_tool).

T1 weather_now: the current weather for a place, from open-meteo (no key, no spend, no account).
T6 transcript: at call end the session history goes to Redis lou:transcript:<room> (the owner's standing
   rule: every Lou call saves a transcript). If Redis cannot be reached the record goes to a local file
   instead, so a call never ends without one.

Nothing here dials, texts or contacts anyone. Spec: Drive doc 1OFhYVy_omCXwxaJRqcMagfJa9Aln-wTsSu7xpNK7MX4.
"""
import json
import os
import re
import time
import urllib.parse
import urllib.request

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_SECONDS = 4          # the caller is waiting on the line
TRANSCRIPT_TTL = 90 * 24 * 3600
ROOM_OK = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")

# WMO weather codes as open-meteo returns them, in words Lou can say.
WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "heavy showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


def http_json(url, seconds=HTTP_SECONDS):
    with urllib.request.urlopen(url, timeout=seconds) as response:
        return json.loads(response.read().decode("utf-8"))


def weather_now(location, fetch=None) -> str:
    """One sentence Lou can say. Never raises: a failure is a sentence too, so the call goes on."""
    fetch = fetch or http_json
    place = str(location or "").split(",")[0].strip()
    if not place:
        return "I need a town or city to check the weather."
    try:
        found = fetch(GEOCODE_URL + "?" + urllib.parse.urlencode(
            {"name": place, "count": 1, "language": "en", "format": "json"}))
        results = (found or {}).get("results") or []
        if not results:
            return "I couldn't find a place called %s." % place
        top = results[0]
        now = fetch(FORECAST_URL + "?" + urllib.parse.urlencode({
            "latitude": top["latitude"], "longitude": top["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m", "timezone": "auto"}))
        current = now["current"]
        temp = round(float(current["temperature_2m"]))
        feels = round(float(current["apparent_temperature"]))
        sky = WMO.get(int(current["weather_code"]), "")
    except Exception:  # noqa: BLE001 - the network, a key missing from the answer, a bad number
        return "I couldn't reach the weather service just now."
    where = top.get("name") or place
    sentence = "It's %d degrees%s in %s right now" % (temp, (" and " + sky) if sky else "", where)
    if feels != temp:
        sentence += ", feels like %d" % feels
    return sentence + "."


def transcript_key(room) -> str:
    room = str(room or "")
    if not ROOM_OK.fullmatch(room):
        raise ValueError("room name not usable as a key")
    return "lou:transcript:" + room


def transcript_record(room, history: dict, started: float, ended: float, agent_name: str) -> dict:
    """The stored shape: who, when, and the turns. Function calls stay in, so a tool's use is on record."""
    items = (history or {}).get("items") or []
    return {"room": room, "agent": agent_name, "started_utc": _iso(started), "ended_utc": _iso(ended),
            "seconds": round(max(0.0, ended - started), 1), "turns": len(items), "items": items}


def save_transcript(record: dict, client=None, fallback_dir=None, ttl=TRANSCRIPT_TTL) -> str:
    """Redis first; if that fails, a local JSON file. Returns where it went: 'redis:<key>' or 'file:<path>'."""
    key = transcript_key(record.get("room"))
    body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if client is not None:
        try:
            client.set(key, body, ex=ttl)
            return "redis:" + key
        except Exception:  # noqa: BLE001 - fall through to the file
            pass
    folder = fallback_dir or os.environ.get("LOU_TRANSCRIPT_DIR") or "/tmp/lou-transcripts"
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, key.replace(":", "_") + ".json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return "file:" + path


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
