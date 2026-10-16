"""Lou as code: a LiveKit Agents worker (livekit-agents 1.8.8) on the OpenAI realtime model, with tools that fire.

Why code: the no-code Agent Builder never starts a function tool (zero "Function tools started" events, seen
twice in the LiveKit Console). In code the realtime session calls @function_tool methods. The Builder agent
stays as the fallback until this one passes rehearsals; this one registers under its own agent name
(LOU_AGENT_NAME, default lou-code), so a dispatch only reaches it when it asks for that name.

Tools in this cut: T1 get_weather (open-meteo, no key, no spend) and T6 the transcript saved at call end.
Nothing here dials, texts or contacts anyone: the caller is whoever the dispatch already put in the room.

The prompt is NOT in this repo (the repo is public). LOU_INSTRUCTIONS_FILE names the v4 prompt file on the
host; without it Lou gets a minimal prompt that still says she is an AI.

Run: python lou_agent.py dev (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, OPENAI_API_KEY in the env).
"""
import asyncio
import logging
import os
import time

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, RunContext, cli, function_tool
from livekit.plugins import openai

import lou_tools as tools

log = logging.getLogger("lou-agent")
AGENT_NAME = os.environ.get("LOU_AGENT_NAME", "lou-code")
MINIMAL_PROMPT = ("You are Lou, an AI assistant for Converspan. Say early in the call that you are an AI. "
                  "Be warm, brief and moderate in pace. Use get_weather when someone asks about the weather.")
OPENER = "Greet the person. Say you are Lou, an AI assistant from Converspan, and ask how you can help."


def instructions(environ=None) -> str:
    environ = os.environ if environ is None else environ
    path = environ.get("LOU_INSTRUCTIONS_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
            if text:
                return text
        except OSError:
            log.warning("LOU_INSTRUCTIONS_FILE unreadable; using the minimal prompt")
    return MINIMAL_PROMPT


def redis_client(environ=None):
    """The fleet's Redis when LOU_REDIS=on and redis_dual.py is beside this file; otherwise None (file fallback)."""
    environ = os.environ if environ is None else environ
    if environ.get("LOU_REDIS") != "on":
        return None
    try:
        import redis_dual  # noqa: PLC0415 - copied beside this file in the image
        return redis_dual.client(redis_dual.Settings(), precheck=False)
    except Exception:  # noqa: BLE001 - no Redis means the file fallback, never a dropped transcript
        log.warning("Redis unavailable; transcripts go to the file fallback")
        return None


class Lou(Agent):
    def __init__(self, prompt: str):
        super().__init__(instructions=prompt)

    @function_tool
    async def get_weather(self, context: RunContext, location: str) -> str:
        """Get the current weather for a town or city. Use it whenever the caller asks about the weather.

        Args:
            location: The town or city, for example Mississauga.
        """
        return await asyncio.to_thread(tools.weather_now, location)


server = AgentServer()


@server.rtc_session(agent_name=AGENT_NAME)
async def entrypoint(ctx: JobContext):
    started = time.time()
    session = AgentSession(llm=openai.realtime.RealtimeModel(
        model=os.environ.get("LOU_REALTIME_MODEL", "gpt-realtime"),
        voice=os.environ.get("LOU_VOICE", "marin")))

    async def save_transcript(reason: str = ""):
        record = tools.transcript_record(ctx.room.name, session.history.to_dict(), started, time.time(), AGENT_NAME)
        where = await asyncio.to_thread(tools.save_transcript, record, redis_client())
        log.info("transcript saved to %s (%d turns, %s)", where, record["turns"], reason or "ended")

    ctx.add_shutdown_callback(save_transcript)
    await ctx.connect()
    await ctx.wait_for_participant()   # on an outbound call, the callee; Lou speaks only once someone is there
    await session.start(agent=Lou(instructions()), room=ctx.room)
    await session.generate_reply(instructions=OPENER)


if __name__ == "__main__":
    cli.run_app(server)
