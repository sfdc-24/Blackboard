"""Lou as code (cloud/lou-agent): T1 get_weather and T6 the transcript.

The ways this goes wrong, each with a test that fails when the guard is removed:
  - the weather answer is read from the wrong keys (fixtures are the REAL open-meteo answers, 2026-10-16)
  - a bad place, a dead network or a malformed answer raises mid-call instead of giving Lou a sentence
  - a room name that is not a safe key becomes a Redis key
  - Redis failing drops the transcript instead of writing the file
  - the tool is defined but the SDK never runs it (skipped when livekit-agents is not installed)
"""
import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LOU = REPO / "cloud" / "lou-agent"
sys.path.insert(0, str(LOU))
tools = importlib.import_module("lou_tools")

# Captured from the live API, 2026-10-16 23:45Z.
GEOCODE = {"results": [{"id": 6075357, "name": "Mississauga", "latitude": 43.5789, "longitude": -79.6583,
                        "country_code": "CA", "timezone": "America/Toronto", "country": "Canada", "admin1": "Ontario"}],
           "generationtime_ms": 0.11217594}
FORECAST = {"latitude": 43.57767, "longitude": -79.66636, "timezone": "America/Toronto",
            "current_units": {"time": "iso8601", "interval": "seconds", "temperature_2m": "°C",
                              "apparent_temperature": "°C", "weather_code": "wmo code", "wind_speed_10m": "km/h"},
            "current": {"time": "2026-10-16T19:45", "interval": 900, "temperature_2m": 8.2,
                        "apparent_temperature": 4.1, "weather_code": 1, "wind_speed_10m": 19.2}}


def fake_fetch(geocode=GEOCODE, forecast=FORECAST, seen=None):
    def fetch(url):
        if seen is not None:
            seen.append(url)
        if url.startswith(tools.GEOCODE_URL):
            return geocode
        if url.startswith(tools.FORECAST_URL):
            return forecast
        raise AssertionError(url)
    return fetch


class WeatherTest(unittest.TestCase):
    def test_the_real_answer_becomes_one_sentence(self):
        self.assertEqual("It's 8 degrees and mostly clear in Mississauga right now, feels like 4.",
                         tools.weather_now("Mississauga", fetch=fake_fetch()))

    def test_it_asks_for_the_place_it_found(self):
        seen = []
        tools.weather_now("Mississauga, Ontario", fetch=fake_fetch(seen=seen))
        self.assertIn("name=Mississauga&", seen[0])
        self.assertIn("latitude=43.5789", seen[1])
        self.assertIn("longitude=-79.6583", seen[1])

    def test_no_feels_like_when_it_is_the_same(self):
        same = json.loads(json.dumps(FORECAST))
        same["current"]["apparent_temperature"] = 8.4
        self.assertEqual("It's 8 degrees and mostly clear in Mississauga right now.",
                         tools.weather_now("Mississauga", fetch=fake_fetch(forecast=same)))

    def test_failures_are_sentences_not_exceptions(self):
        def dead(url):
            raise OSError("network down")
        self.assertEqual("I couldn't reach the weather service just now.", tools.weather_now("Mississauga", fetch=dead))
        self.assertEqual("I couldn't reach the weather service just now.",
                         tools.weather_now("Mississauga", fetch=fake_fetch(forecast={"current": {}})))
        self.assertEqual("I couldn't find a place called Nowhereville.",
                         tools.weather_now("Nowhereville", fetch=fake_fetch(geocode={"generationtime_ms": 0.1})))
        self.assertEqual("I need a town or city to check the weather.", tools.weather_now("  ", fetch=fake_fetch()))


class TranscriptTest(unittest.TestCase):
    HISTORY = {"items": [{"type": "message", "role": "assistant", "content": ["Hi, this is Lou, an AI assistant."]},
                         {"type": "message", "role": "user", "content": ["What's the weather?"]}]}

    def record(self, room="C8J98G"):
        return tools.transcript_record(room, self.HISTORY, 1792193000.0, 1792193600.0, "lou-code")

    def test_the_record_carries_the_turns_and_the_times(self):
        r = self.record()
        self.assertEqual((2, 600.0, "lou-code"), (r["turns"], r["seconds"], r["agent"]))
        self.assertEqual(self.HISTORY["items"], r["items"])
        self.assertTrue(r["started_utc"].endswith("Z"))

    def test_it_goes_to_redis_under_the_room_with_a_ttl(self):
        class Client:
            def set(self, key, value, ex=None):
                self.call = (key, json.loads(value), ex)
        c = Client()
        self.assertEqual("redis:lou:transcript:C8J98G", tools.save_transcript(self.record(), client=c))
        self.assertEqual(("lou:transcript:C8J98G", 2, tools.TRANSCRIPT_TTL), (c.call[0], c.call[1]["turns"], c.call[2]))

    def test_redis_failing_still_leaves_a_file(self):
        class Broken:
            def set(self, *a, **k):
                raise ConnectionError("no route")
        with tempfile.TemporaryDirectory() as d:
            where = tools.save_transcript(self.record(), client=Broken(), fallback_dir=d)
            self.assertTrue(where.startswith("file:"))
            self.assertEqual(2, json.loads(Path(where[5:]).read_text(encoding="utf-8"))["turns"])

    def test_a_room_name_that_is_not_a_key_is_refused(self):
        for room in ("", "a b", "x" * 129, "room\nFLUSHALL"):
            with self.assertRaises(ValueError):
                tools.transcript_key(room)


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
        result.expect.next_event().is_function_call_output(
            output="It's 8 degrees and mostly clear in Mississauga right now, feels like 4.")
        result.expect.next_event().is_message(role="assistant")


if __name__ == "__main__":
    unittest.main()
