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


def topic_line(state: dict | None) -> str:
    """One line naming the topic, or "" when the visitor did not pick one."""
    topic = (state or {}).get("topic") or ""
    if topic not in TOPICS:
        return ""
    return "The visitor picked this topic before starting: %s.\n" % TOPICS[topic]


def with_topic(state: dict | None, canvas: str) -> str:
    return topic_line(state) + (canvas or "")


__all__ = ["TOPICS", "topic_line", "with_topic"]
