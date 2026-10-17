"""Lou as code (cloud/lou-agent): T1 get_weather and T6 the transcript.

The ways this goes wrong, each with a test that fails when the guard is removed:
  - the weather answer is read from the wrong keys (fixtures are the REAL open-meteo answers)
  - London resolves to England for a Canadian caller, or a named region is ignored (Codex, 2159d7c)
  - a bad place, a dead network or a malformed answer raises mid-call instead of giving Lou a sentence
  - two calls in the reused room, or two distinct keys, land on one transcript (Codex and cowork-chrome)
  - Redis failing drops the transcript instead of writing the file
  - the tool is defined but the SDK never runs it, or the session id is lost (skipped without livekit-agents)
"""
import asyncio
import importlib
import json
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOU = REPO / "cloud" / "lou-agent"
sys.path.insert(0, str(LOU))
tools = importlib.import_module("lou_tools")


# Captured from the live API (open-meteo geocoding, count=10), 2026-10-16 23:45Z and 2026-10-17 02:40Z.
def _r(name, admin1, country, code, lat, lon):
    return {"name": name, "admin1": admin1, "country": country, "country_code": code, "latitude": lat, "longitude": lon}


GEO = {
    ("Mississauga", "CA"): [_r("Mississauga", "Ontario", "Canada", "CA", 43.5789, -79.6583),
                            _r("Mississauga Beach", "Ontario", "Canada", "CA", 43.25876, -79.08625)],
    ("London", "CA"): [_r("London", "Ontario", "Canada", "CA", 42.98339, -81.23304),
                       _r("Londonderry", "Nova Scotia", "Canada", "CA", 45.48345, -63.5987)],
    ("London", ""): [_r("London", "England", "United Kingdom", "GB", 51.50853, -0.12574),
                     _r("London", "Ontario", "Canada", "CA", 42.98339, -81.23304),
                     _r("London", "Ohio", "United States", "US", 39.88645, -83.44825)],
    ("Paris", "CA"): [_r("Paris", "Ontario", "Canada", "CA", 43.2, -80.38333),
                      _r("Paris", "Yukon", "Canada", "CA", 63.81447, -138.65434)],
    ("Paris", ""): [_r("Paris", "Ile-de-France Region", "France", "FR", 48.85341, 2.3488),
                    _r("Paris", "Texas", "United States", "US", 33.66094, -95.55551)],
}
FORECAST = {"latitude": 43.57767, "longitude": -79.66636, "timezone": "America/Toronto",
            "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C",
                              "apparent_temperature": "°C", "weather_code": "wmo code", "wind_speed_10m": "km/h"},
            "current": {"time": "2026-10-16T19:45", "interval": 900, "temperature_2m": 8.2,
                        "apparent_temperature": 4.1, "weather_code": 1, "wind_speed_10m": 19.2}}
MISSISSAUGA_NOW = "It's 8 degrees and mostly clear in Mississauga, Ontario right now, feels like 4."


def fake_fetch(geo=None, forecast=FORECAST, seen=None):
    geo = GEO if geo is None else geo

    def fetch(url):
        if seen is not None:
            seen.append(url)
        if url.startswith(tools.GEOCODE_URL):
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            return {"results": geo.get((q["name"][0], q.get("countryCode", [""])[0]), []), "generationtime_ms": 0.1}
        if url.startswith(tools.FORECAST_URL):
            return forecast
        raise AssertionError(url)
    return fetch


def lat_asked(seen):
    return [u for u in seen if u.startswith(tools.FORECAST_URL)][0].split("latitude=")[1].split("&")[0]


class WeatherTest(unittest.TestCase):
    def test_the_real_answer_becomes_one_sentence(self):
        self.assertEqual(MISSISSAUGA_NOW, tools.weather_now("Mississauga", fetch=fake_fetch()))

    def test_london_is_london_ontario_by_default(self):
        """Codex's NO-GO on 2159d7c: London resolved to London, England (latitude 51.5)."""
        seen = []
        said = tools.weather_now("London", fetch=fake_fetch(seen=seen))
        self.assertEqual("42.98339", lat_asked(seen))
        self.assertIn("London, Ontario", said)
        self.assertIn("countryCode=CA", seen[0])

    def test_a_named_region_wins(self):
        for asked, lat in (("London, Ontario", "42.98339"), ("London, England", "51.50853"),
                           ("Paris, France", "48.85341"), ("Paris, Ontario", "43.2"), ("London, Ohio", "39.88645"),
                           ("Paris, FR", "48.85341")):
            seen = []
            tools.weather_now(asked, fetch=fake_fetch(seen=seen))
            self.assertEqual(lat, lat_asked(seen), asked)

    def test_no_default_country_means_the_first_match_anywhere(self):
        seen = []
        tools.weather_now("London", fetch=fake_fetch(seen=seen), default_country="")
        self.assertEqual("51.50853", lat_asked(seen))

    def test_a_region_with_no_such_place_is_not_guessed(self):
        self.assertEqual("I couldn't find a place called London, Yukon.",
                         tools.weather_now("London, Yukon", fetch=fake_fetch()))

    def test_no_feels_like_when_it_is_the_same(self):
        same = json.loads(json.dumps(FORECAST))
        same["current"]["apparent_temperature"] = 8.4
        self.assertEqual("It's 8 degrees and mostly clear in Mississauga, Ontario right now.",
                         tools.weather_now("Mississauga", fetch=fake_fetch(forecast=same)))

    def test_failures_are_sentences_not_exceptions(self):
        def dead(url):
            raise OSError("network down")
        self.assertEqual("I couldn't reach the weather service just now.", tools.weather_now("Mississauga", fetch=dead))
        self.assertEqual("I couldn't reach the weather service just now.",
                         tools.weather_now("Mississauga", fetch=fake_fetch(forecast={"current": {}})))
        self.assertEqual("I couldn't find a place called Nowhereville.",
                         tools.weather_now("Nowhereville", fetch=fake_fetch()))
        self.assertEqual("I need a town or city to check the weather.", tools.weather_now("  ", fetch=fake_fetch()))


class TranscriptTest(unittest.TestCase):
    HISTORY = {"items": [{"type": "message", "role": "assistant", "content": ["Hi, this is Lou, an AI assistant."]},
                         {"type": "message", "role": "user", "content": ["What's the weather?"]}]}

    def record(self, room="C8J98G", session="RM_XgPGs9mjFt8r"):
        return tools.transcript_record(room, session, self.HISTORY, 1792193000.0, 1792193600.0, "lou-code")

    def test_the_record_carries_the_turns_the_times_and_the_session(self):
        r = self.record()
        self.assertEqual((2, 600.0, "lou-code", "RM_XgPGs9mjFt8r"), (r["turns"], r["seconds"], r["agent"], r["session"]))
        self.assertEqual(self.HISTORY["items"], r["items"])
        self.assertTrue(r["started_utc"].endswith("Z"))

    def test_it_goes_to_redis_under_the_room_and_session_with_a_ttl(self):
        class Client:
            def set(self, key, value, ex=None):
                self.call = (key, json.loads(value), ex)
        c = Client()
        self.assertEqual("redis:lou:transcript:C8J98G:RM_XgPGs9mjFt8r", tools.save_transcript(self.record(), client=c))
        self.assertEqual(("lou:transcript:C8J98G:RM_XgPGs9mjFt8r", 2, tools.TRANSCRIPT_TTL),
                         (c.call[0], c.call[1]["turns"], c.call[2]))

    def test_two_calls_in_the_same_room_are_two_transcripts(self):
        """cowork-chrome: C8J98G is a fixed room; keyed by the room alone the second call overwrote the first."""
        with tempfile.TemporaryDirectory() as d:
            first = tools.save_transcript(self.record(session="RM_one"), fallback_dir=d)
            second = tools.save_transcript(self.record(session="RM_two"), fallback_dir=d)
            self.assertNotEqual(first, second)
            self.assertEqual(2, len(list(Path(d).iterdir())))

    def test_keys_that_differ_never_share_a_file_or_a_key(self):
        """Codex's NO-GO on 2159d7c: rooms a:b and a_b wrote one file."""
        pairs = [(("a:b", "s"), ("a_b", "s")), (("a:b", "c"), ("a", "b:c")), (("a b", "s"), ("a_b", "t")),
                 (("a_b", "c"), ("a", "b_c"))]
        with tempfile.TemporaryDirectory() as d:
            for one, two in pairs:
                self.assertNotEqual(tools.transcript_key(*one), tools.transcript_key(*two), (one, two))
                p1 = tools.save_transcript(self.record(*one), fallback_dir=d)
                p2 = tools.save_transcript(self.record(*two), fallback_dir=d)
                self.assertNotEqual(p1, p2, (one, two))
            self.assertEqual(8, len(list(Path(d).iterdir())))

    def test_redis_failing_still_leaves_a_file(self):
        class Broken:
            def set(self, *a, **k):
                raise ConnectionError("no route")
        with tempfile.TemporaryDirectory() as d:
            where = tools.save_transcript(self.record(), client=Broken(), fallback_dir=d)
            self.assertTrue(where.startswith("file:"))
            self.assertEqual(2, json.loads(Path(where[5:]).read_text(encoding="utf-8"))["turns"])

    def test_an_empty_or_oversized_part_is_refused(self):
        for room, session in (("", "s"), ("r", ""), ("x" * 129, "s"), ("r", "x" * 129)):
            with self.assertRaises(ValueError):
                tools.transcript_key(room, session)


class ModelTest(unittest.TestCase):
    def test_the_builder_lous_model_and_voice_are_the_defaults(self):
        """cowork-chrome, 2026-10-17: the Builder Lou runs GPT-Live 1 with voice Marin."""
        source = (LOU / "lou_agent.py").read_text(encoding="utf-8")
        self.assertIn("openai.realtime.GPTLiveModel(", source)
        self.assertIn('"LOU_REALTIME_MODEL", "gpt-live-1"', source)
        self.assertIn('"LOU_VOICE", "marin"', source)


try:
    import livekit.agents  # noqa: F401
    HAVE_LIVEKIT = True
except ImportError:
    HAVE_LIVEKIT = False


@unittest.skipUnless(HAVE_LIVEKIT, "livekit-agents is not installed")
class SdkTest(unittest.TestCase):
    """The SDK itself runs get_weather when the model asks for it. A fake model stands in for the realtime one:
    the first turn calls the tool, the second speaks what the tool returned."""

    def test_the_session_fires_the_tool(self):
        from livekit.agents import AgentSession, llm  # noqa: PLC0415
        agent = importlib.import_module("lou_agent")

        class Stream(llm.LLMStream):
            async def _run(self):
                outputs = [i for i in self.chat_ctx.items if i.type == "function_call_output"]
                if outputs:
                    delta = llm.ChoiceDelta(role="assistant", content="Lou says: " + outputs[-1].output)
                else:
                    delta = llm.ChoiceDelta(role="assistant", tool_calls=[llm.FunctionToolCall(
                        name="get_weather", arguments='{"location": "Mississauga"}', call_id="call_1")])
                self._event_ch.send_nowait(llm.ChatChunk(id="c", delta=delta))

        class Model(llm.LLM):
            def chat(self, *, chat_ctx, tools=None, conn_options=None, **kwargs):
                return Stream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)

        async def run():
            original = tools.http_json
            tools.http_json = fake_fetch()
            try:
                async with AgentSession(llm=Model()) as session:
                    await session.start(agent.Lou("test prompt"))
                    result = await session.run(user_input="What's the weather in Mississauga?")
                    return result
            finally:
                tools.http_json = original

        result = asyncio.run(run())
        result.expect.next_event().is_function_call(name="get_weather", arguments={"location": "Mississauga"})
        result.expect.next_event().is_function_call_output(output=MISSISSAUGA_NOW)
        result.expect.next_event().is_message(role="assistant")

    def test_the_session_id_is_livekits_and_a_failure_keeps_the_fallback(self):
        agent = importlib.import_module("lou_agent")

        class Room:
            def __init__(self, sid=None, fail=False):
                self._sid, self._fail = sid, fail

            @property
            async def sid(self):
                if self._fail:
                    raise RuntimeError("not connected")
                return self._sid

        self.assertEqual("RM_XgPGs9mjFt8r", asyncio.run(agent.room_session_id(Room("RM_XgPGs9mjFt8r"), "t1")))
        self.assertEqual("t1", asyncio.run(agent.room_session_id(Room(fail=True), "t1")))
        self.assertEqual("t1", asyncio.run(agent.room_session_id(Room(""), "t1")))

    def test_the_gpt_live_model_takes_the_tool(self):
        from livekit.plugins import openai  # noqa: PLC0415
        model = openai.realtime.GPTLiveModel(model="gpt-live-1", voice="marin", api_key="offline-test")
        self.assertTrue(model.capabilities.auto_tool_reply_generation)


if __name__ == "__main__":
    unittest.main()
