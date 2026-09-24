"""Offline-only metadata proposals. Nothing in this module authorizes a write.

The closed contract is deliberately separate from Salesforce provider code.
Confirmation checks exercise a future UI contract, not an execution capability.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re

POLICY = "lead-text-proposal-only-v1"
ORG_RE = re.compile(r"00D[A-Za-z0-9]{15}")
STEM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
HASH_RE = re.compile(r"[a-f0-9]{64}")
ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
PLAN_PREFIX = r"(?:please )?plan (?:a )?(?:new )?(?:text )?field on Lead for\b"
PLAN_INTENT_RE = re.compile(PLAN_PREFIX, re.IGNORECASE)
PLAN_REQUEST_RE = re.compile(PLAN_PREFIX + r" ([A-Za-z][A-Za-z0-9 ]{0,39})[.]?", re.IGNORECASE)
COMPONENT = r"\b(?:fields?|objects?|metadata|schema)(?:\s+(?:named|called)\s+[A-Za-z][A-Za-z0-9_]*)?"
STANDARD_OBJECT = r"(?:Lead|Account|Contact|Opportunity|Case|Campaign|Task|Event|User)"
SALESFORCE_DESTINATION = (
    r"Salesforce\b(?!-)(?=$|[.,;!?]|\s+(?:org\b|object\b|custom\s+object\b|schema\b|metadata\b|"
    r"and\b|then\b|now\b|for\b|with\b|named\b|called\b|" + STANDARD_OBJECT + r"\s+object\b))"
)
FIELD_KEYS = {"parent", "name", "label", "type", "length", "required", "unique", "external_id"}
CONFIRM_KEYS = {"plan_id", "plan_revision", "plan_hash", "confirmation_nonce"}
NOTICE = (
    "Proposal only: Salesforce has not been contacted and no field has been created. "
    "Confirmation only validates this contract; execution is not implemented."
)


class MetadataContractError(ValueError):
    pass


def validate_field(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != FIELD_KEYS:
        raise MetadataContractError("field requires exactly the Lead Text proposal fields")
    if value["parent"] != "Lead" or value["type"] != "Text":
        raise MetadataContractError("only a new Text field on Lead is supported")
    stem = value["name"]
    if (not isinstance(stem, str) or not STEM_RE.fullmatch(stem)
            or "__" in stem or stem.endswith("_")):
        raise MetadataContractError("name must be a 1-32 character ASCII developer-name stem without a suffix")
    label = value["label"]
    if (not isinstance(label, str) or not 1 <= len(label) <= 40
            or label != label.strip() or not label.isprintable()
            or any(char in label for char in "<>&")):
        raise MetadataContractError("label must be 1-40 trimmed printable characters without markup")
    if type(value["length"]) is not int or not 1 <= value["length"] <= 255:
        raise MetadataContractError("Text length must be an integer from 1 to 255")
    if any(value[key] is not False for key in ("required", "unique", "external_id")):
        raise MetadataContractError("required, unique and external_id must be false")
    return copy.deepcopy(value)


def parse_request(text: str) -> dict | None:
    """Recognize one anchored grammar; never infer consent from natural language.

    Unsupported metadata requests get local guidance rather than model fallback.
    Unrelated prototype requests retain their existing route.
    """
    match = PLAN_REQUEST_RE.fullmatch(text.strip())
    if match:
        label = " ".join(match[1].split()).title()
        return validate_field({"parent": "Lead", "name": label.replace(" ", "_"),
                               "label": label, "type": "Text", "length": 80,
                               "required": False, "unique": False, "external_id": False})
    return None


def is_metadata_request(text: str) -> bool:
    # "lead" and "custom field" also describe ordinary prototype form inputs.
    # Recognize the anchored intent even if its label/suffix is malformed. Only
    # parse_request's fullmatch can create a proposal; the rest gets guidance.
    if PLAN_INTENT_RE.match(text.strip()):
        return True
    if not (re.search(r"\b(?:plan|create|add|delete|remove|update|change|deploy)\b", text, re.I)
            and re.search(r"\b(?:fields?|objects?|metadata|schema)\b", text, re.I)):
        return False
    # A prepositional standard-object target is explicit without the vendor
    # name; "lead form" or "lead field" still does not imply an org operation.
    if re.search(
        COMPONENT + r"\s+(?:on|to|in|within)\s+(?:(?:the|my|our)\s+)?"
        + STANDARD_OBJECT + r"\s+object\b",
        text, re.I,
    ):
        return True
    # "from Salesforce" normally identifies an existing UI data source.
    # Treat it as an org mutation only when delete/remove acts on that noun,
    # not when a later source clause happens to follow another action.
    if re.search(
        r"\b(?:delete|remove)\s+(?:(?:the|a|an|all|existing|custom|Text)\s+)*"
        r"(?:[A-Za-z][A-Za-z0-9_]*\s+)?" + COMPONENT
        + r"\s+from\s+(?:(?:the|my|our)\s+)?(?:" + SALESFORCE_DESTINATION + r"|"
        + STANDARD_OBJECT + r"\s+object\b)", text, re.I,
    ):
        return True
    if not re.search(r"\bSalesforce\b(?!-)", text, re.I):
        return False
    # Bind the destination to the component noun, not a later reference/source
    # clause such as "form using labels from Salesforce". Explicit operations
    # stay local even if they also mention UI consequences.
    if re.search(
        COMPONENT + r"(?:\s+on\s+(?:the\s+)?" + STANDARD_OBJECT + r"(?:\s+object)?)?"
        r"\s+(?:in|within|on|to)\s+(?:(?:the|my|our)\s+)?" + SALESFORCE_DESTINATION,
        text, re.I,
    ):
        return True
    # A later branded noun in heading/copy instructions is only a reference.
    # Require the action to directly govern the Salesforce component instead.
    branded_target = re.search(
        r"\b(?:plan|create|add|delete|remove|update|change|deploy)\s+"
        r"(?:(?:a|an|the|new|existing)\s+)*Salesforce\s+"
        r"(?:(?:Lead|Account|Contact|Opportunity|custom|Text)\s+)*"
        r"(?:fields?|objects?|metadata|schema)\b", text, re.I,
    )
    if not branded_target:
        return False
    # "Add a Salesforce field to our form" targets the prototype. Conversely,
    # "Create a Salesforce object, then show it in the app" first targets the
    # org: do not let a subsequent UI consequence hide that explicit operation.
    primary_clause = re.split(r"\b(?:and|then)\b", text[branded_target.start():], maxsplit=1, flags=re.I)[0]
    return not bool(re.search(
        r"\b(?:website|webpage|web page|page|app|application|prototype|mockup|form|screen)\b",
        primary_clause, re.I,
    ))


def validate_confirmation(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != CONFIRM_KEYS:
        raise MetadataContractError("confirmation requires exactly plan_id, plan_revision, plan_hash and confirmation_nonce")
    for key in ("plan_id", "confirmation_nonce"):
        if not isinstance(value[key], str) or not ID_RE.fullmatch(value[key]):
            raise MetadataContractError("confirmation requires contract identifiers")
    if type(value["plan_revision"]) is not int or value["plan_revision"] < 1:
        raise MetadataContractError("plan_revision must be a positive integer")
    if not isinstance(value["plan_hash"], str) or not HASH_RE.fullmatch(value["plan_hash"]):
        raise MetadataContractError("plan_hash must be a SHA-256 digest")


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def make_plan(field: dict, *, session_id: str, subject: str, org_id: str,
              plan_id: str, nonce: str, revision: int, now: int, expires_at: int) -> dict:
    if not subject or not isinstance(org_id, str) or not ORG_RE.fullmatch(org_id):
        raise MetadataContractError("proposal requires an operator and configured exact organization binding")
    field = validate_field(field)
    binding = {"policy": POLICY, "session_id": session_id, "operator_subject": subject,
               "org_id": org_id, "plan_id": plan_id, "revision": revision,
               "confirmation_nonce": nonce, "field": field, "created_at": now,
               "expires_at": min(expires_at, now + 600)}
    return {"binding": binding, "plan_hash": _digest(binding), "status": "proposal_only"}


def check_confirmation(plan: dict, confirmation: dict, *, session_id: str,
                       subject: str, org_id: str, now: int) -> None:
    validate_confirmation(confirmation)
    if not isinstance(plan, dict) or plan.get("status") != "proposal_only":
        raise MetadataContractError("no unconfirmed proposal is available")
    binding = plan["binding"]
    if (binding["policy"] != POLICY or binding["session_id"] != session_id
            or not subject or binding["operator_subject"] != subject or binding["org_id"] != org_id):
        raise MetadataContractError("proposal binding does not match this session, operator or configuration")
    if now >= binding["expires_at"]:
        raise MetadataContractError("proposal has expired; request a new proposal")
    expected = {"plan_id": binding["plan_id"], "plan_revision": binding["revision"],
                "plan_hash": plan["plan_hash"], "confirmation_nonce": binding["confirmation_nonce"]}
    if confirmation != expected or not hmac.compare_digest(plan["plan_hash"], _digest(binding)):
        raise MetadataContractError("confirmation does not match the exact current proposal")
    validate_field(binding["field"])


def public_plan(plan: dict) -> dict:
    binding = plan["binding"]
    return {"plan_id": binding["plan_id"], "plan_revision": binding["revision"],
            "plan_hash": plan["plan_hash"], "confirmation_nonce": binding["confirmation_nonce"],
            "policy": POLICY, "status": plan["status"], "execution_available": False,
            "target": "Configured developer org (identity not checked)",
            "expires_at": binding["expires_at"], "field": copy.deepcopy(binding["field"]),
            "full_name": "Lead." + binding["field"]["name"] + "__c",
            "notice": NOTICE}
