"""What the visitor picked on the homepage before Start (owner, 2026-09-25).

The page offers a few high-level topics so the right agents, templates and
framing are chosen without the visitor needing to know the backend. The topic
is stored on the session at creation and put in front of every lane's context
(talk, recap, analyst, builder), so each agent starts from the same brief.
"""
from __future__ import annotations

TOPICS = {
    "logo": "Design a logo - a brand mark, wordmark, palette and type",
    "website": "Build a website - pages, sections and the first thing a visitor should do",
    "app": "Develop an app - screens, flows and the data behind them",
    "salesforce_admin": "Salesforce admin - automation, routing, approvals and access in a Salesforce org",
    "salesforce_data": "Salesforce data - the data model, reports, dashboards, imports and data quality",
    "other": "Something else - listen first, then shape it",
}


# Which model talks, by what the visitor wants to do (owner, 2026-09-25: "design
# logo would be openAI and may be salesforce is claude ... also get Gemini and
# Meta to participate"). The visitor never picks a model.
TOPIC_AGENT = {
    "logo": "openai",
    "website": "gemini",
    "app": "meta",
    "salesforce_admin": "claude",
    "salesforce_data": "claude",
    "other": "claude",
}
FALLBACK = ("claude", "openai", "gemini", "meta")


def route_agent(topic, available) -> str:
    """The topic's agent when it is available, else the first available in
    FALLBACK order; "" when none is."""
    available = list(available or [])
    wanted = TOPIC_AGENT.get(topic if isinstance(topic, str) else "", "claude")
    if wanted in available:
        return wanted
    return next((a for a in FALLBACK if a in available), "")


def topic_line(state: dict | None) -> str:
    """One line naming the topic, or "" when the visitor did not pick one."""
    topic = (state or {}).get("topic") or ""
    if topic not in TOPICS:
        return ""
    return "The visitor picked this topic before starting: %s.\n" % TOPICS[topic]


def with_topic(state: dict | None, canvas: str) -> str:
    return topic_line(state) + (canvas or "")


__all__ = ["TOPICS", "TOPIC_AGENT", "FALLBACK", "route_agent", "topic_line", "with_topic"]
