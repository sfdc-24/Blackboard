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


# The charter in disguise (owner, 2026-09-25: "a quality of context score ...
# scope, objectives, design sense, refinement, timeline and closing expectations
# ... basically a project charter and workplan"). Each topic has its own ordered
# frame of dimensions; the charter lane (workers/charter.py) scores how well
# each is covered and asks about the next open one. sfdc24.com leads with
# Salesforce; the website frame follows the owner's order (type, audience, the
# business, design sense). Ids are a contract with the homepage.
CHARTER_FRAMES = {
    "salesforce_admin": (
        ("org", "Your org", "the edition, how many users, and the clouds in use"),
        ("pain", "Pain points", "what slows the team down today"),
        ("process", "Process", "the flows to fix or automate"),
        ("data", "Data", "the objects and fields involved"),
        ("integrations", "Integrations", "the systems that connect to Salesforce"),
        ("timeline", "Timeline", "when it needs to be in place"),
        ("close", "Next step", "who decides, the budget range, and the next step"),
    ),
    "salesforce_data": (
        ("objects", "Objects", "the objects and records that matter"),
        ("quality", "Data quality", "duplicates, gaps and what cannot be trusted today"),
        ("reports", "Reports", "the reports and dashboards people need"),
        ("sources", "Sources", "where the data comes from"),
        ("access", "Access", "who may see and change what, and governance"),
        ("timeline", "Timeline", "when it needs to be in place"),
        ("close", "Next step", "who decides, the budget range, and the next step"),
    ),
    "website": (
        ("type", "Site type", "ecommerce, a blog or social site, or a company page"),
        ("audience", "Audience", "who will visit and why"),
        ("business", "The business", "who they are and what the site should represent"),
        ("design", "Design sense", "the look, feel and tone"),
        ("scope", "Scope", "the pages and components"),
        ("timeline", "Timeline", "when it needs to launch"),
        ("close", "Next step", "who decides, the budget range, and the next step"),
    ),
}
DEFAULT_CHARTER_FRAME = (
    ("objectives", "Objectives", "what success looks like"),
    ("scope", "Scope", "what is in and what is out"),
    ("design", "Design", "design sense or requirements"),
    ("refinement", "Refinement", "what changed as it took shape"),
    ("timeline", "Timeline", "when it needs to be ready"),
    ("close", "Next step", "who decides, the budget range, and the next step"),
)


def charter_frame(topic) -> tuple:
    """The topic's ordered (id, label, what it covers) dimensions; the default
    frame for logo, app, other and no topic."""
    return CHARTER_FRAMES.get(topic if isinstance(topic, str) else "", DEFAULT_CHARTER_FRAME)


def topic_line(state: dict | None) -> str:
    """One line naming the topic, or "" when the visitor did not pick one."""
    topic = (state or {}).get("topic") or ""
    if topic not in TOPICS:
        return ""
    return "The visitor picked this topic before starting: %s.\n" % TOPICS[topic]


def with_topic(state: dict | None, canvas: str) -> str:
    return topic_line(state) + (canvas or "")


__all__ = ["TOPICS", "TOPIC_AGENT", "FALLBACK", "route_agent", "topic_line", "with_topic",
           "CHARTER_FRAMES", "DEFAULT_CHARTER_FRAME", "charter_frame"]
