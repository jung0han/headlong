"""Evidence thresholds and append-only Improvement Proposal projections."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

WORK_PROPOSAL_SCHEMA = "headlong.work-improvement-proposal/v1"
OBSERVER_PROPOSAL_SCHEMA = "headlong.observer-improvement-proposal/v1"
PROPOSAL_SCHEMA = WORK_PROPOSAL_SCHEMA  # Backward-compatible public import.
EVIDENCE_UPDATE_SCHEMA = "headlong.proposal-evidence-update/v1"
REVIEW_SCHEMA = "headlong.proposal-review/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
DIRECT_EVIDENCE_KINDS = frozenset(
    {"user_correction", "test_failure", "tool_failure", "reviewer_finding"}
)
OBSERVER_DIRECT_EVIDENCE_KINDS = frozenset(
    {"user_correction", "observer_failure", "observer_regression"}
)
PROPOSAL_EVIDENCE_KINDS = (
    DIRECT_EVIDENCE_KINDS | OBSERVER_DIRECT_EVIDENCE_KINDS | {"inferred_pattern"}
)
REVIEW_STATES = frozenset({"pending", "accepted", "rejected", "dismissed"})

_EVENT_NAMESPACE = uuid.UUID("2e09ea75-52e3-49df-b191-dcdb931a62b5")
_MAX_CONTENT = 1200
_SPACE_RE = re.compile(r"\s+")


class ProposalError(ValueError):
    """A proposal or review event violated the public v1 contract."""


def work_proposal_events(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive stable direct-evidence Work Proposal events from one analysis."""
    return [
        event
        for event in direct_proposal_events(analysis)
        if event["proposal_type"] == "work"
    ]


def direct_proposal_events(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive Work or Observer proposals from supported concrete evidence."""
    context = _analysis_context(analysis)
    if context is None:
        return []
    analysis_id, session_id, task_root_id, scope = context
    signals = analysis.get("improvement_signals")
    if not isinstance(signals, list):
        raise ProposalError("source analysis improvement_signals must be an array")

    events: list[dict[str, Any]] = []
    for signal in signals:
        if not isinstance(signal, dict):
            raise ProposalError("source analysis improvement signal must be an object")
        proposal_type = _proposal_type(signal.get("proposal_type", "work"))
        evidence_kind = signal.get("kind")
        allowed = (
            DIRECT_EVIDENCE_KINDS
            if proposal_type == "work"
            else OBSERVER_DIRECT_EVIDENCE_KINDS
        )
        # Patterns use the cross-session gate below. Open loops, unsupported
        # self-evaluation, and design preference are deliberately not evidence.
        if evidence_kind not in allowed:
            continue
        content = _content(signal.get("content"))
        locators = _locators(signal.get("evidence_locators"))
        if any(locator["source_identity"] != session_id for locator in locators):
            raise ProposalError("Improvement Proposal cited a different Codex Session")
        proposal_id = _direct_proposal_id(
            proposal_type, scope, session_id, evidence_kind, content, locators
        )
        events.append(
            _proposal_event(
                proposal_id=proposal_id,
                proposal_type=proposal_type,
                scope=scope,
                evidence_kind=evidence_kind,
                content=content,
                locators=locators,
                source_identities=[session_id],
                task_root_ids=[task_root_id],
                analysis_ids=[analysis_id],
                pattern=False,
            )
        )
    return events


def inferred_pattern_proposal_events(
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Create one proposal per claim after three distinct current task roots."""
    ledger = list(events)
    superseded = {
        replaced
        for event in ledger
        for replaced in event.get("supersedes_event_ids", [])
        if isinstance(replaced, str)
    }
    groups: dict[str, dict[str, Any]] = {}
    for analysis in ledger:
        context = _analysis_context(analysis)
        if context is None or analysis.get("event_id") in superseded:
            continue
        analysis_id, session_id, task_root_id, scope = context
        signals = analysis.get("improvement_signals")
        if not isinstance(signals, list):
            raise ProposalError("source analysis improvement_signals must be an array")
        for signal in signals:
            if not isinstance(signal, dict):
                raise ProposalError("source analysis improvement signal must be an object")
            if signal.get("kind") != "inferred_pattern":
                continue
            proposal_type = _proposal_type(signal.get("proposal_type", "work"))
            content = _content(signal.get("content"))
            locators = _locators(signal.get("evidence_locators"))
            if any(locator["source_identity"] != session_id for locator in locators):
                raise ProposalError("inferred pattern cited a different Codex Session")
            key = _pattern_key(proposal_type, scope, _normalize_claim(content))
            group = groups.setdefault(
                key,
                {
                    "proposal_id": key,
                    "proposal_type": proposal_type,
                    "scope": scope,
                    "content": content,
                    "roots": set(),
                    "sessions": set(),
                    "analysis_ids": set(),
                    "locators": {},
                },
            )
            group["roots"].add(task_root_id)
            group["sessions"].add(session_id)
            group["analysis_ids"].add(analysis_id)
            for locator in locators:
                group["locators"][_locator_key(locator)] = locator

    output: list[dict[str, Any]] = []
    for group in groups.values():
        roots = sorted(group["roots"])
        if len(roots) < 3:
            continue
        output.append(
            _proposal_event(
                proposal_id=group["proposal_id"],
                proposal_type=group["proposal_type"],
                scope=group["scope"],
                evidence_kind="inferred_pattern",
                content=group["content"],
                locators=[group["locators"][key] for key in sorted(group["locators"])],
                source_identities=sorted(group["sessions"]),
                task_root_ids=roots,
                analysis_ids=sorted(group["analysis_ids"]),
                pattern=True,
            )
        )
    return sorted(output, key=lambda event: event["event_id"])


def evidence_update_event(
    desired: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any] | None:
    """Return an append-only enrichment event when a pattern gains evidence."""
    if desired.get("evidence_kind") != "inferred_pattern":
        return None
    fields = (
        "evidence_locators",
        "source_identities",
        "task_root_ids",
        "source_analysis_event_ids",
    )
    if all(desired.get(field) == current.get(field) for field in fields):
        return None
    proposal_id = _uuid(desired.get("event_id"), "proposal id")
    identity = json.dumps(
        {"proposal_id": proposal_id, **{field: desired[field] for field in fields}},
        sort_keys=True,
        separators=(",", ":"),
    )
    event_id = str(uuid.uuid5(_EVENT_NAMESPACE, f"evidence-update:{identity}"))
    return {
        "type": "proposal-evidence-update",
        "step_id": event_id,
        "event_id": event_id,
        "event_schema": "headlong.activity-ledger/v1",
        "proposal_evidence_schema": EVIDENCE_UPDATE_SCHEMA,
        "proposal_id": proposal_id,
        "proposal_type": desired["proposal_type"],
        "source": "personal_assistant",
        "source_kind": "proposal_inbox",
        "source_identity": proposal_id,
        "knowledge_scope": desired["knowledge_scope"],
        "evidence_kind": "inferred_pattern",
        "verification": "corroborated",
        "authority": "candidate",
        "execution_authority": "none",
        "causal_event_ids": desired["source_analysis_event_ids"],
        "supersedes_event_ids": [],
        "evidence_locators": desired["evidence_locators"],
        "source_identities": desired["source_identities"],
        "task_root_ids": desired["task_root_ids"],
        "source_analysis_event_ids": desired["source_analysis_event_ids"],
        "title": "Improvement Proposal evidence updated",
        "content": "The recurring pattern gained additional validated evidence.",
    }


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
        if event.get("proposal_schema") in {
            WORK_PROPOSAL_SCHEMA,
            OBSERVER_PROPOSAL_SCHEMA,
        }:
            proposal = _proposal(event)
            inbox[proposal["proposal_id"]] = proposal
        elif event.get("proposal_evidence_schema") == EVIDENCE_UPDATE_SCHEMA:
            _apply_evidence_update(inbox, event)
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
            target["review_state"] = state
            target["review_event_id"] = event_id
            target["reviewed_at"] = event.get("ts")
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


def _proposal_event(
    *,
    proposal_id: str,
    proposal_type: str,
    scope: dict[str, str],
    evidence_kind: str,
    content: str,
    locators: list[dict[str, Any]],
    source_identities: list[str],
    task_root_ids: list[str],
    analysis_ids: list[str],
    pattern: bool,
) -> dict[str, Any]:
    event_type = f"{proposal_type}-improvement-proposal"
    schema = (
        WORK_PROPOSAL_SCHEMA
        if proposal_type == "work"
        else OBSERVER_PROPOSAL_SCHEMA
    )
    label = (
        "Work Improvement Proposal"
        if proposal_type == "work"
        else "Observer Improvement Proposal"
    )
    title = (
        f"{label} from recurring pattern"
        if evidence_kind == "inferred_pattern"
        else f"{label} from {evidence_kind.replace('_', ' ')}"
    )
    return {
        "type": event_type,
        "step_id": proposal_id,
        "event_id": proposal_id,
        "event_schema": "headlong.activity-ledger/v1",
        "proposal_schema": schema,
        "proposal_type": proposal_type,
        "proposal_label": label,
        "source": "personal_assistant",
        "source_kind": "codex_session_pattern" if pattern else "codex_session",
        "source_identity": proposal_id if pattern else source_identities[0],
        "knowledge_scope": scope,
        "evidence_kind": evidence_kind,
        "verification": "corroborated" if pattern else "observed",
        "authority": "candidate",
        "review_state": "pending",
        "execution_authority": "none",
        "causal_event_ids": analysis_ids,
        "supersedes_event_ids": [],
        "evidence_locators": locators,
        "title": title,
        "content": content,
        "source_identities": source_identities,
        "task_root_ids": task_root_ids,
        "source_analysis_event_id": analysis_ids[0] if len(analysis_ids) == 1 else None,
        "source_analysis_event_ids": analysis_ids,
    }


def _proposal(event: dict[str, Any]) -> dict[str, Any]:
    proposal_type = _proposal_type(event.get("proposal_type"))
    if event.get("type") != f"{proposal_type}-improvement-proposal":
        raise ProposalError("Improvement Proposal has an invalid event type")
    schema = (
        WORK_PROPOSAL_SCHEMA
        if proposal_type == "work"
        else OBSERVER_PROPOSAL_SCHEMA
    )
    if event.get("proposal_schema") != schema:
        raise ProposalError("Improvement Proposal has an invalid public schema")
    proposal_id = _uuid(event.get("event_id"), "proposal id")
    evidence_kind = event.get("evidence_kind")
    allowed = (
        DIRECT_EVIDENCE_KINDS | {"inferred_pattern"}
        if proposal_type == "work"
        else OBSERVER_DIRECT_EVIDENCE_KINDS | {"inferred_pattern"}
    )
    if evidence_kind not in allowed:
        raise ProposalError("Improvement Proposal has unsupported evidence")
    scope = _project_scope(event.get("knowledge_scope"))
    locators = _locators(event.get("evidence_locators"))
    sessions = _uuid_list(event.get("source_identities"), "source session ids")
    roots = _uuid_list(event.get("task_root_ids"), "task root ids")
    analyses = _uuid_list(
        event.get("source_analysis_event_ids"), "source analysis event ids"
    )
    if event.get("execution_authority") != "none":
        raise ProposalError("Improvement Proposal attempted to grant execution authority")
    if event.get("review_state") != "pending":
        raise ProposalError("Improvement Proposal must enter the Inbox as pending")
    if {locator["source_identity"] for locator in locators} != set(sessions):
        raise ProposalError("Improvement Proposal does not expose its complete source set")
    if event.get("causal_event_ids") != analyses:
        raise ProposalError("Improvement Proposal is not grounded in its source analyses")
    if evidence_kind == "inferred_pattern":
        if len(roots) < 3 or event.get("source_kind") != "codex_session_pattern":
            raise ProposalError("inferred pattern lacks three distinct task roots")
    elif len(sessions) != 1 or len(roots) != 1 or event.get("source_kind") != "codex_session":
        raise ProposalError("direct Improvement Proposal must cite one session")
    return {
        "proposal_id": proposal_id,
        "proposal_type": proposal_type,
        "proposal_label": event.get("proposal_label"),
        "title": _content(event.get("title")),
        "content": _content(event.get("content")),
        "knowledge_scope": scope,
        "evidence_kind": evidence_kind,
        "evidence_locators": locators,
        "source_identity": sessions[0] if len(sessions) == 1 else None,
        "source_identities": sessions,
        "task_root_ids": roots,
        "source_analysis_event_id": analyses[0] if len(analyses) == 1 else None,
        "source_analysis_event_ids": analyses,
        "review_state": "pending",
        "execution_authority": "none",
        "created_at": event.get("ts"),
        "review_event_id": None,
        "reviewed_at": None,
    }


def _apply_evidence_update(
    inbox: dict[str, dict[str, Any]], event: dict[str, Any]
) -> None:
    proposal_id = _uuid(event.get("proposal_id"), "evidence update proposal id")
    if proposal_id not in inbox:
        raise ProposalError("proposal evidence update references an unknown proposal")
    target = inbox[proposal_id]
    locators = _locators(event.get("evidence_locators"))
    sessions = _uuid_list(event.get("source_identities"), "source session ids")
    roots = _uuid_list(event.get("task_root_ids"), "task root ids")
    analyses = _uuid_list(
        event.get("source_analysis_event_ids"), "source analysis event ids"
    )
    if (
        event.get("source_identity") != proposal_id
        or event.get("source_kind") != "proposal_inbox"
        or event.get("proposal_type") != target["proposal_type"]
        or event.get("knowledge_scope") != target["knowledge_scope"]
        or event.get("evidence_kind") != "inferred_pattern"
        or event.get("execution_authority") != "none"
        or event.get("causal_event_ids") != analyses
        or len(roots) < 3
        or {locator["source_identity"] for locator in locators} != set(sessions)
    ):
        raise ProposalError("proposal evidence update is not grounded in its target")
    target["evidence_locators"] = locators
    target["source_identity"] = sessions[0] if len(sessions) == 1 else None
    target["source_identities"] = sessions
    target["task_root_ids"] = roots
    target["source_analysis_event_id"] = analyses[0] if len(analyses) == 1 else None
    target["source_analysis_event_ids"] = analyses


def _reviewable_proposal(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProposalError("proposal review target must be an object")
    proposal_id = _uuid(value.get("proposal_id"), "proposal id")
    if value.get("evidence_kind") not in PROPOSAL_EVIDENCE_KINDS:
        raise ProposalError("proposal review target has unsupported evidence")
    if value.get("execution_authority") != "none":
        raise ProposalError("proposal review target grants execution authority")
    return {
        **value,
        "proposal_id": proposal_id,
        "proposal_type": _proposal_type(value.get("proposal_type")),
        "knowledge_scope": _project_scope(value.get("knowledge_scope")),
        "evidence_locators": _locators(value.get("evidence_locators")),
    }


def _analysis_context(
    analysis: dict[str, Any],
) -> tuple[str, str, str, dict[str, str]] | None:
    if (
        analysis.get("type") != "observation"
        or analysis.get("source") != "personal_assistant"
        or analysis.get("analysis_state") not in {"provisional", "final"}
    ):
        return None
    analysis_id = _uuid(analysis.get("event_id"), "source analysis event id")
    session_id = _uuid(analysis.get("source_identity"), "Codex Session id")
    task_root_id = _uuid(
        analysis.get("task_root_id", session_id), "Codex task root id"
    )
    return analysis_id, session_id, task_root_id, _project_scope(
        analysis.get("knowledge_scope")
    )


def _direct_proposal_id(
    proposal_type: str,
    scope: dict[str, str],
    session_id: str,
    evidence_kind: str,
    content: str,
    locators: list[dict[str, Any]],
) -> str:
    identity = json.dumps(
        {
            "schema": "headlong.direct-improvement-proposal/v1",
            "proposal_type": proposal_type,
            "scope": scope,
            "session_id": session_id,
            "evidence_kind": evidence_kind,
            "content": _normalize_claim(content),
            "locator_digests": sorted(locator["sha256"] for locator in locators),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(_EVENT_NAMESPACE, identity))


def _pattern_key(
    proposal_type: str, scope: dict[str, str], normalized_claim: str
) -> str:
    identity = json.dumps(
        {
            "schema": "headlong.inferred-pattern/v1",
            "proposal_type": proposal_type,
            "scope": scope,
            "claim": normalized_claim,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(_EVENT_NAMESPACE, identity))


def _proposal_type(value: Any) -> str:
    if value not in {"work", "observer"}:
        raise ProposalError("unsupported Improvement Proposal type")
    return str(value)


def _project_scope(value: Any) -> dict[str, str]:
    if (
        not isinstance(value, dict)
        or set(value) != {"kind", "project_id"}
        or value.get("kind") != "project"
        or not isinstance(value.get("project_id"), str)
        or not value["project_id"]
    ):
        raise ProposalError("Improvement Proposal requires one exact project Knowledge Scope")
    return {"kind": "project", "project_id": value["project_id"]}


def _locators(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ProposalError("Improvement Proposal requires Evidence Locators")
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected = {
        "schema", "kind", "source_identity", "source_root", "relative_path",
        "line", "byte_offset", "byte_length", "sha256", "host",
    }
    for locator in value:
        if not isinstance(locator, dict) or set(locator) != expected:
            raise ProposalError("Improvement Proposal has a malformed Evidence Locator")
        if locator.get("schema") != LOCATOR_SCHEMA or locator.get("kind") != "codex_event":
            raise ProposalError("Improvement Proposal has an unsupported Evidence Locator")
        _uuid(locator.get("source_identity"), "Evidence Locator source identity")
        if (
            locator.get("source_root") not in {"active", "archived"}
            or not isinstance(locator.get("relative_path"), str)
            or Path(locator["relative_path"]).is_absolute()
            or ".." in Path(locator["relative_path"]).parts
            or not isinstance(locator.get("line"), int) or locator["line"] < 1
            or not isinstance(locator.get("byte_offset"), int) or locator["byte_offset"] < 0
            or not isinstance(locator.get("byte_length"), int) or locator["byte_length"] < 1
            or not isinstance(locator.get("sha256"), str) or len(locator["sha256"]) != 64
            or any(char not in "0123456789abcdef" for char in locator["sha256"])
            or not isinstance(locator.get("host"), str) or not locator["host"]
        ):
            raise ProposalError("Improvement Proposal has invalid Evidence Locator coordinates")
        key = _locator_key(locator)
        if key not in seen:
            output.append(dict(locator))
            seen.add(key)
    return output


def _locator_key(locator: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(locator, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _uuid_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ProposalError(f"{field} must be a non-empty array")
    values = [_uuid(item, field) for item in value]
    if values != sorted(set(values)):
        raise ProposalError(f"{field} must contain sorted distinct UUIDs")
    return values


def _normalize_claim(value: str) -> str:
    return _SPACE_RE.sub(" ", value).strip().casefold()


def _content(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_CONTENT:
        raise ProposalError("Improvement Proposal text is empty or exceeds compact limits")
    return value.strip()


def _uuid(value: Any, field: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ProposalError(f"invalid {field}") from exc
    return str(parsed)
