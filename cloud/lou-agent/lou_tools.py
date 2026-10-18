"""Lou's tools as plain functions, so they test without LiveKit (lou_agent.py wraps them as @function_tool).

T1 weather_now: the current weather for a place, from open-meteo (no key, no spend, no account).
T6 transcript: at call end the session history goes to Redis lou:transcript:<room>:<session> (the owner's standing
   rule: every Lou call saves a transcript). If Redis cannot be reached the record goes to a local file
   instead, so a call never ends without one.
Consent (the owner, 2026-10-18: "Lou consent gate before greeting/recording"): a call only exists once the visitor
   has consented. Lou says nothing, the realtime model hears nothing and no transcript is kept until the visitor's
   OWN participant attribute lou.consent is exactly "yes". Withdrawn at any point after the yes, the call ends and
   nothing is kept; a yes set again later in the same call does not count. One Consent object per call holds this.
   PIPEDA and the CRTC require consent before recording or a transcript; a pre-checked box, a name or a company is
   not consent, and neither is the room's notice. Who may set the attribute: only the visitor's own client, on the
   visitor's own action (its token needs canUpdateOwnMetadata). A server must never mint it into a token.

Nothing here dials, texts or contacts anyone. Spec: Drive doc 1OFhYVy_omCXwxaJRqcMagfJa9Aln-wTsSu7xpNK7MX4.
"""
import asyncio
import json
import os
import time
import urllib.parse
import urllib.request

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_SECONDS = 4          # the caller is waiting on the line
TRANSCRIPT_TTL = 90 * 24 * 3600
DEFAULT_COUNTRY = os.environ.get("LOU_DEFAULT_COUNTRY", "CA")   # Converspan is Canada-first
CONSENT_ATTRIBUTE = "lou.consent"
CONSENT_WAIT = 180        # seconds Lou waits, silent, for the visitor's consent before it leaves

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


def _geocode(fetch, place, country=""):
    params = {"name": place, "count": 10, "language": "en", "format": "json"}
    if country:
        params["countryCode"] = country
    return list(((fetch(GEOCODE_URL + "?" + urllib.parse.urlencode(params)) or {}).get("results")) or [])


def _in_region(result, region) -> bool:
    """The caller's 'London, Ontario' or 'Paris, France': the province or state, the country, or its code."""
    region = region.strip().lower()
    names = [str(result.get(k) or "").strip().lower() for k in ("admin1", "country", "country_code")]
    return any(n and (n == region or n.startswith(region)) for n in names)


def _same_name(a, b) -> bool:
    """'london' is London; 'Londonderry' and 'St Johns Island' are not London or St Johns."""
    a, b = str(a or "").strip().casefold(), str(b or "").strip().casefold()
    return a == b


def _first(results, place, region=""):
    """The first result that IS the place asked for (and in the region, when one was named). A near name is
    never taken: the search returns Londonderry for 'London', and the caller would hear the wrong town's
    weather. None, so Lou asks which town instead (Aya on be319c7: London, Nova Scotia gave Londonderry)."""
    return next((r for r in results if _same_name(r.get("name"), place) and (not region or _in_region(r, region))),
                None)


def find_place(location, fetch, default_country=DEFAULT_COUNTRY):
    """The place the caller means. With a region ('London, Ontario') only a match in it; without one, the
    default country first (Canada: 'London' is London, Ontario), then anywhere. None when nothing fits."""
    parts = [p.strip() for p in str(location or "").split(",") if p.strip()]
    if not parts:
        return None
    place, region = parts[0], (parts[1] if len(parts) > 1 else "")
    home = _geocode(fetch, place, default_country) if default_country else []
    return _first(home, place, region) or _first(_geocode(fetch, place), place, region)


def weather_now(location, fetch=None, default_country=None) -> str:
    """One sentence Lou can say. Never raises: a failure is a sentence too, so the call goes on."""
    fetch = fetch or http_json
    default_country = DEFAULT_COUNTRY if default_country is None else default_country
    asked = ", ".join(p.strip() for p in str(location or "").split(",") if p.strip())
    if not asked:
        return "I need a town or city to check the weather."
    try:
        top = find_place(asked, fetch, default_country)
        if top is None:
            return "I couldn't find a place called %s. Which town or city do you mean?" % asked
        now = fetch(FORECAST_URL + "?" + urllib.parse.urlencode({
            "latitude": top["latitude"], "longitude": top["longitude"],
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m", "timezone": "auto"}))
        current = now["current"]
        temp = round(float(current["temperature_2m"]))
        feels = round(float(current["apparent_temperature"]))
        sky = WMO.get(int(current["weather_code"]), "")
    except Exception:  # noqa: BLE001 - the network, a key missing from the answer, a bad number
        return "I couldn't reach the weather service just now."
    where = str(top.get("name") or asked.split(",")[0])
    if top.get("admin1"):
        where += ", " + str(top["admin1"])
    sentence = "It's %d degrees%s in %s right now" % (temp, (" and " + sky) if sky else "", where)
    if feels != temp:
        sentence += ", feels like %d" % feels
    return sentence + "."


def transcript_key(room, session) -> str:
    """lou:transcript:<room>:<session>. The room is reused across calls (C8J98G), so the room alone
    would overwrite the last call's transcript; the session (LiveKit's RM_... id) makes each call its own."""
    room, session = str(room or ""), str(session or "")
    if not (0 < len(room) <= 128 and 0 < len(session) <= 128):
        raise ValueError("room or session not usable as a key")
    # Each part percent-encoded, so ':' inside a name can never make two calls one key.
    return "lou:transcript:%s:%s" % (urllib.parse.quote(room, safe=""), urllib.parse.quote(session, safe=""))


def transcript_record(room, session, history: dict, started: float, ended: float, agent_name: str) -> dict:
    """The stored shape: who, when, and the turns. Function calls stay in, so a tool's use is on record."""
    items = (history or {}).get("items") or []
    return {"room": room, "session": session, "agent": agent_name, "started_utc": _iso(started), "ended_utc": _iso(ended),
            "seconds": round(max(0.0, ended - started), 1), "turns": len(items), "items": items}


def save_transcript(record: dict, client=None, fallback_dir=None, ttl=TRANSCRIPT_TTL) -> str:
    """Redis first; if that fails, a local JSON file. Returns where it went: 'redis:<key>' or 'file:<path>'."""
    key = transcript_key(record.get("room"), record.get("session"))
    body = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if client is not None:
        try:
            client.set(key, body, ex=ttl)
            return "redis:" + key
        except Exception:  # noqa: BLE001 - fall through to the file
            pass
    folder = fallback_dir or os.environ.get("LOU_TRANSCRIPT_DIR") or "/tmp/lou-transcripts"
    os.makedirs(folder, exist_ok=True)
    # Percent-encoded, so two distinct keys are never one file (':' and '_' used to collapse together).
    path = os.path.join(folder, urllib.parse.quote(key, safe="") + ".json")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    return "file:" + path


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def consented(attributes) -> bool:
    """The visitor's consent: their participant attribute lou.consent is exactly "yes". Anything else is no."""
    return isinstance(attributes, dict) and attributes.get(CONSENT_ATTRIBUTE) == "yes"


def consent_wait(environ=None) -> float:
    """LOU_CONSENT_WAIT seconds, 10 to 900; CONSENT_WAIT when unset or unusable."""
    environ = os.environ if environ is None else environ
    try:
        wait = float(environ.get("LOU_CONSENT_WAIT") or CONSENT_WAIT)
    except ValueError:
        return CONSENT_WAIT
    return wait if 10 <= wait <= 900 else CONSENT_WAIT


class Consent:
    """THE consent state of one call, shared by Lou's startup and the transcript save, so the two never disagree.
    It listens from the moment it is made, so a change between two looks is never missed. A withdrawal is
    latched: a "yes" set again later in the same call does not bring the call or its transcript back (Aya,
    PR 426: a withdrawal before the session started was not seen, and withdraw-then-restore kept a transcript).
    room is a livekit.rtc.Room (on/off, participant_attributes_changed, participant_disconnected)."""

    def __init__(self, room, visitor):
        self.room, self.visitor = room, visitor
        self.given = consented(visitor.attributes)
        self.withdrawn = False
        self._waiters, self._on_withdrawn = [], []
        room.on("participant_attributes_changed", self._changed)
        room.on("participant_disconnected", self._left)

    def holds(self) -> bool:
        """Given and never withdrawn in this call."""
        return self.given and not self.withdrawn

    def on_withdrawn(self, callback):
        """callback() once, at the withdrawal; at once if consent was already withdrawn."""
        if self.withdrawn:
            callback()
        else:
            self._on_withdrawn.append(callback)

    async def wait(self, timeout=CONSENT_WAIT) -> bool:
        """True once the visitor consents and consent still holds; False when they leave first, withdraw, or the
        wait runs out."""
        if self.given or self.withdrawn:
            return self.holds()
        done = asyncio.get_running_loop().create_future()
        self._waiters.append(done)
        try:
            await asyncio.wait_for(done, timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            if done in self._waiters:
                self._waiters.remove(done)
        return self.holds()

    def close(self):
        self.room.off("participant_attributes_changed", self._changed)
        self.room.off("participant_disconnected", self._left)

    def _wake(self):
        for done in self._waiters:
            if not done.done():
                done.set_result(None)

    def _changed(self, _changed, who):
        if who.identity != self.visitor.identity:
            return
        if consented(who.attributes):
            if not self.withdrawn:
                self.given = True
        elif self.given and not self.withdrawn:
            self.withdrawn = True
            callbacks, self._on_withdrawn = self._on_withdrawn, []
            for callback in callbacks:
                callback()
        self._wake()

    def _left(self, who):
        if who.identity == self.visitor.identity:
            self._wake()
