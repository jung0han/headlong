"""Append-only Work Improvement Proposal contracts and ledger projection.

The model may emit improvement signals while analysing a Codex Session.  This
module is the deterministic authority boundary that decides which signals are
direct evidence, turns them into proposals, and folds proposal/review events
into the current Proposal Inbox.  A review changes only that projection; this
module deliberately has no execution adapter.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

PROPOSAL_SCHEMA = "headlong.work-improvement-proposal/v1"
REVIEW_SCHEMA = "headlong.proposal-review/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
DIRECT_EVIDENCE_KINDS = frozenset(
    {"user_correction", "test_failure", "tool_failure", "reviewer_finding"}
)
REVIEW_STATES = frozenset({"pending", "accepted", "rejected", "dismissed"})

_EVENT_NAMESPACE = uuid.UUID("2e09ea75-52e3-49df-b191-dcdb931a62b5")
_MAX_CONTENT = 1200
_SPACE_RE = re.compile(r"\s+")


class ProposalError(ValueError):
    """A proposal or review event violated the public v1 contract."""


def work_proposal_events(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive stable Work Proposal events from supported direct evidence only."""
    if (
        analysis.get("type") != "observation"
        or analysis.get("source") != "personal_assistant"
        or analysis.get("analysis_state") not in {"provisional", "final"}
    ):
        return []
    analysis_id = _uuid(analysis.get("event_id"), "source analysis event id")
    session_id = _uuid(analysis.get("source_identity"), "Codex Session id")
    scope = _project_scope(analysis.get("knowledge_scope"))
    signals = analysis.get("improvement_signals")
    if not isinstance(signals, list):
        raise ProposalError("source analysis improvement_signals must be an array")

    events: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            raise ProposalError("source analysis improvement signal must be an object")
        evidence_kind = signal.get("kind")
        # Inferred patterns, open loops, and free-form agent suggestions are not
        # direct evidence.  DONGWOO-912 owns the distinct-session threshold.
        if evidence_kind not in DIRECT_EVIDENCE_KINDS:
            continue
        content = _content(signal.get("content"))
        locators = _locators(signal.get("evidence_locators"))
        if any(locator["source_identity"] != session_id for locator in locators):
            raise ProposalError("Work Proposal cited a different Codex Session")
        proposal_id = _proposal_id(
            scope, session_id, evidence_kind, content, locators
        )
        events.append(
            {
                "type": "work-improvement-proposal",
                "step_id": proposal_id,
                "event_id": proposal_id,
                "event_schema": "headlong.activity-ledger/v1",
                "proposal_schema": PROPOSAL_SCHEMA,
                "proposal_type": "work",
                "source": "personal_assistant",
                "source_kind": "codex_session",
                "source_identity": session_id,
                "knowledge_scope": scope,
                "evidence_kind": evidence_kind,
                "verification": "observed",
                "authority": "candidate",
                "review_state": "pending",
                "execution_authority": "none",
                "causal_event_ids": [analysis_id],
                "supersedes_event_ids": [],
                "evidence_locators": locators,
                "title": _title(evidence_kind),
                "content": content,
                "source_analysis_event_id": analysis_id,
            }
        )
    return events


def review_event(
    proposal: dict[str, Any], state: str, *, event_id: str | None = None
) -> dict[str, Any]:
    """Build one user authority event; it intentionally grants no execution."""
    proposal = _reviewable_proposal(proposal)
    if state not in REVIEW_STATES:
        raise ProposalError(f"unsupported Proposal Review State: {state}")
    review_id = _uuid(event_id or str(uuid.uuid4()), "proposal review event id")
    authority = {
        "pending": "candidate",
        "accepted": "active",
        "rejected": "rejected",
        "dismissed": "dismissed",
    }[state]
    return {
        "type": "proposal-review",
        "step_id": review_id,
        "event_id": review_id,
        "event_schema": "headlong.activity-ledger/v1",
        "proposal_review_schema": REVIEW_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "proposal_inbox",
        "source_identity": proposal["proposal_id"],
        "knowledge_scope": proposal["knowledge_scope"],
        "evidence_kind": "user_statement",
        "verification": "observed",
        "authority": authority,
        "review_state": state,
        "execution_authority": "none",
        "causal_event_ids": [proposal["proposal_id"]],
        "supersedes_event_ids": [],
        "evidence_locators": proposal["evidence_locators"],
        "title": f"Proposal review: {state}",
        "content": "The user set the Proposal Inbox review state. No execution was authorized.",
        "proposal_id": proposal["proposal_id"],
    }


def build_inbox(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the current Proposal Inbox solely from Activity Ledger events."""
    inbox: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            continue
        seen.add(event_id)
        if event.get("proposal_schema") == PROPOSAL_SCHEMA:
            proposal = _proposal(event)
            inbox[proposal["proposal_id"]] = proposal
        elif event.get("proposal_review_schema") == REVIEW_SCHEMA:
            proposal_id = _uuid(event.get("proposal_id"), "review proposal id")
            if proposal_id not in inbox:
                raise ProposalError("proposal review references an unknown proposal")
            target = inbox[proposal_id]
            state = event.get("review_state")
            if state not in REVIEW_STATES:
                raise ProposalError("proposal review has an invalid state")
            if event.get("execution_authority") != "none":
                raise ProposalError("proposal review attempted to grant execution authority")
            if (
                event.get("source_identity") != proposal_id
                or event.get("source_kind") != "proposal_inbox"
                or event.get("evidence_kind") != "user_statement"
                or event.get("causal_event_ids") != [proposal_id]
                or event.get("knowledge_scope") != target["knowledge_scope"]
                or event.get("evidence_locators") != target["evidence_locators"]
            ):
                raise ProposalError("proposal review is not grounded in its target")
            inbox[proposal_id]["review_state"] = state
            inbox[proposal_id]["review_event_id"] = event_id
            inbox[proposal_id]["reviewed_at"] = event.get("ts")
    return sorted(
        inbox.values(),
        key=lambda item: (str(item.get("created_at") or ""), item["proposal_id"]),
        reverse=True,
    )


def find_proposal(
    events: Iterable[dict[str, Any]], proposal_id: str
) -> dict[str, Any] | None:
    proposal_id = _uuid(proposal_id, "proposal id")
    return next(
        (item for item in build_inbox(events) if item["proposal_id"] == proposal_id),
        None,
    )


def _proposal(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") != "work-improvement-proposal":
        raise ProposalError("Proposal Inbox contains a non-work proposal")
    proposal_id = _uuid(event.get("event_id"), "proposal id")
    if (
        event.get("proposal_type") != "work"
        or event.get("source") != "personal_assistant"
        or event.get("source_kind") != "codex_session"
    ):
        raise ProposalError("Work Proposal has an invalid type or source")
    evidence_kind = event.get("evidence_kind")
    if evidence_kind not in DIRECT_EVIDENCE_KINDS:
        raise ProposalError("Work Proposal has an unsupported direct evidence kind")
    scope = _project_scope(event.get("knowledge_scope"))
    locators = _locators(event.get("evidence_locators"))
    if event.get("execution_authority") != "none":
        raise ProposalError("Work Proposal attempted to grant execution authority")
    if event.get("review_state") != "pending":
        raise ProposalError("Work Proposal must enter the Inbox as pending")
    session_id = _uuid(event.get("source_identity"), "Codex Session id")
    if any(locator["source_identity"] != session_id for locator in locators):
        raise ProposalError("Work Proposal cited a different Codex Session")
    analysis_id = _uuid(
        event.get("source_analysis_event_id"), "source analysis event id"
    )
    if event.get("causal_event_ids") != [analysis_id]:
        raise ProposalError("Work Proposal is not grounded in its source analysis")
    return {
        "proposal_id": proposal_id,
        "proposal_type": "work",
        "title": _content(event.get("title")),
        "content": _content(event.get("content")),
        "knowledge_scope": scope,
        "evidence_kind": evidence_kind,
        "evidence_locators": locators,
        "source_identity": session_id,
        "source_analysis_event_id": analysis_id,
        "review_state": "pending",
        "execution_authority": "none",
        "created_at": event.get("ts"),
        "review_event_id": None,
        "reviewed_at": None,
    }


def _reviewable_proposal(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProposalError("proposal review target must be an object")
    proposal_id = _uuid(value.get("proposal_id"), "proposal id")
    if value.get("evidence_kind") not in DIRECT_EVIDENCE_KINDS:
        raise ProposalError("proposal review target has unsupported evidence")
    if value.get("execution_authority") != "none":
        raise ProposalError("proposal review target grants execution authority")
    return {
        **value,
        "proposal_id": proposal_id,
        "knowledge_scope": _project_scope(value.get("knowledge_scope")),
        "evidence_locators": _locators(value.get("evidence_locators")),
    }


def _proposal_id(
    scope: dict[str, str],
    session_id: str,
    evidence_kind: str,
    content: str,
    locators: list[dict[str, Any]],
) -> str:
    locator_digests = sorted(locator["sha256"] for locator in locators)
    identity = json.dumps(
        {
            "schema": PROPOSAL_SCHEMA,
            "scope": scope,
            "session_id": session_id,
            "evidence_kind": evidence_kind,
            "content": _SPACE_RE.sub(" ", content).strip().casefold(),
            "locator_digests": locator_digests,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(_EVENT_NAMESPACE, identity))


def _title(kind: str) -> str:
    return {
        "user_correction": "Work improvement from user correction",
        "test_failure": "Work improvement from test failure",
        "tool_failure": "Work improvement from tool failure",
        "reviewer_finding": "Work improvement from reviewer finding",
    }[kind]


def _project_scope(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "project_id"}
        or value.get("kind") != "project"
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
    ):
        raise ProposalError("Work Proposal requires one exact project Knowledge Scope")
    return {"kind": "project", "project_id": value["project_id"]}


def _locators(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ProposalError("Work Proposal requires bounded Evidence Locators")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected = {
        "schema",
        "kind",
        "source_identity",
        "source_root",
        "relative_path",
        "line",
        "byte_offset",
        "byte_length",
        "sha256",
        "host",
    }
    for locator in value:
        if not isinstance(locator, dict) or set(locator) != expected:
            raise ProposalError("Work Proposal has a malformed Evidence Locator")
        if locator.get("schema") != LOCATOR_SCHEMA or locator.get("kind") != "codex_event":
            raise ProposalError("Work Proposal has an unsupported Evidence Locator")
        _uuid(locator.get("source_identity"), "Evidence Locator source identity")
        if (
            locator.get("source_root") not in {"active", "archived"}
            or not isinstance(locator.get("relative_path"), str)
            or Path(locator["relative_path"]).is_absolute()
            or ".." in Path(locator["relative_path"]).parts
            or not isinstance(locator.get("line"), int)
            or locator["line"] < 1
            or not isinstance(locator.get("byte_offset"), int)
            or locator["byte_offset"] < 0
            or not isinstance(locator.get("byte_length"), int)
            or locator["byte_length"] < 1
            or not isinstance(locator.get("sha256"), str)
            or len(locator["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in locator["sha256"])
            or not isinstance(locator.get("host"), str)
            or not locator["host"]
        ):
            raise ProposalError("Work Proposal has invalid Evidence Locator coordinates")
        key = hashlib.sha256(
            json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if key not in seen:
            output.append(dict(locator))
            seen.add(key)
    return output


def _content(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_CONTENT:
        raise ProposalError("Work Proposal text is empty or exceeds compact limits")
    return value.strip()


def _uuid(value: Any, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProposalError(f"invalid {field}") from exc
    return str(parsed)
