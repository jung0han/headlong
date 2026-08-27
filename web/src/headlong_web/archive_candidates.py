"""Evidence-backed Codex Archive Candidate events and review projections."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Iterable


CANDIDATE_SCHEMA = "headlong.archive-candidate/v1"
REVIEW_SCHEMA = "headlong.archive-candidate-review/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
REVIEW_STATES = frozenset({"pending", "accepted", "rejected", "dismissed"})

_EVENT_NAMESPACE = uuid.UUID("775edeb3-1b03-494f-82b8-5bc034db6e86")
_MAX_RATIONALE = 1200


class ArchiveCandidateError(ValueError):
    """An Archive Candidate or review event violated the public contract."""


def candidate_events(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive stable pending candidates from one validated Codex analysis."""
    context = _analysis_context(analysis)
    if context is None:
        return []
    analysis_id, session_id, project_id, analysis_state = context
    values = analysis.get("archive_candidates", [])
    if not isinstance(values, list):
        raise ArchiveCandidateError("source analysis Archive Candidates must be an array")
    events: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            raise ArchiveCandidateError("source Archive Candidate must be an object")
        if value.get("completion_state") != "completed":
            continue
        rationale = _text(value.get("rationale"), "completion rationale")
        locators = _locators(value.get("evidence_locators"))
        if any(locator["source_identity"] != session_id for locator in locators):
            raise ArchiveCandidateError(
                "Archive Candidate cited a different Codex Session"
            )
        candidate_id = _candidate_id(
            analysis_id, session_id, project_id, rationale, locators
        )
        events.append(
            {
                "type": "archive-candidate",
                "step_id": candidate_id,
                "event_id": candidate_id,
                "event_schema": "headlong.activity-ledger/v1",
                "archive_candidate_schema": CANDIDATE_SCHEMA,
                "source": "personal_assistant",
                "source_kind": "codex_session",
                "source_identity": session_id,
                "session_id": session_id,
                "project_id": project_id,
                "knowledge_scope": {"kind": "project", "project_id": project_id},
                "evidence_kind": "model_inference",
                "verification": "observed",
                "authority": "candidate",
                "review_state": "pending",
                "archive_authority": "none",
                "execution_authority": "none",
                "completion_state": "completed",
                "completion_rationale": rationale,
                "analysis_state": analysis_state,
                "source_analysis_event_id": analysis_id,
                "causal_event_ids": [analysis_id],
                "supersedes_event_ids": [],
                "evidence_locators": locators,
                "title": "Codex Session Archive Candidate",
                "content": rationale,
            }
        )
    return events


def review_event(
    candidate: dict[str, Any], state: str, *, event_id: str | None = None
) -> dict[str, Any]:
    """Build one user review event without granting archive execution."""
    candidate = _reviewable_candidate(candidate)
    if state not in REVIEW_STATES:
        raise ArchiveCandidateError(f"unsupported Archive Candidate review state: {state}")
    review_id = _uuid(event_id or str(uuid.uuid4()), "review event id")
    authority = {
        "pending": "candidate",
        "accepted": "active",
        "rejected": "rejected",
        "dismissed": "dismissed",
    }[state]
    archive_authority = "authorized" if state == "accepted" else "none"
    return {
        "type": "archive-candidate-review",
        "step_id": review_id,
        "event_id": review_id,
        "event_schema": "headlong.activity-ledger/v1",
        "archive_candidate_review_schema": REVIEW_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "archive_candidate_inbox",
        "source_identity": candidate["candidate_id"],
        "candidate_id": candidate["candidate_id"],
        "session_id": candidate["session_id"],
        "project_id": candidate["project_id"],
        "knowledge_scope": candidate["knowledge_scope"],
        "evidence_kind": "user_statement",
        "verification": "observed",
        "authority": authority,
        "review_state": state,
        "archive_authority": archive_authority,
        "execution_authority": "none",
        "causal_event_ids": [candidate["candidate_id"]],
        "supersedes_event_ids": [],
        "evidence_locators": candidate["evidence_locators"],
        "title": f"Archive Candidate review: {state}",
        "content": (
            "The user set the Archive Candidate review state. "
            "No Codex archive execution was performed."
        ),
    }


def build_inbox(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild current Archive Candidate review state from ledger events."""
    inbox: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            continue
        seen.add(event_id)
        if event.get("archive_candidate_schema") == CANDIDATE_SCHEMA:
            candidate = _candidate(event)
            inbox[candidate["candidate_id"]] = candidate
        elif event.get("archive_candidate_review_schema") == REVIEW_SCHEMA:
            candidate_id = _uuid(event.get("candidate_id"), "review candidate id")
            if candidate_id not in inbox:
                raise ArchiveCandidateError(
                    "Archive Candidate review references an unknown candidate"
                )
            target = inbox[candidate_id]
            state = event.get("review_state")
            if state not in REVIEW_STATES:
                raise ArchiveCandidateError("Archive Candidate review has an invalid state")
            if (
                event.get("execution_authority") != "none"
                or event.get("archive_authority")
                != ("authorized" if state == "accepted" else "none")
                or event.get("source_identity") != candidate_id
                or event.get("source_kind") != "archive_candidate_inbox"
                or event.get("evidence_kind") != "user_statement"
                or event.get("causal_event_ids") != [candidate_id]
                or event.get("session_id") != target["session_id"]
                or event.get("project_id") != target["project_id"]
                or event.get("knowledge_scope") != target["knowledge_scope"]
                or event.get("evidence_locators") != target["evidence_locators"]
            ):
                raise ArchiveCandidateError(
                    "Archive Candidate review is not grounded in its target"
                )
            target["review_state"] = state
            target["archive_authority"] = event["archive_authority"]
            target["review_event_id"] = event_id
            target["reviewed_at"] = event.get("ts")
    return sorted(
        inbox.values(),
        key=lambda item: (str(item.get("created_at") or ""), item["candidate_id"]),
        reverse=True,
    )


def find_candidate(
    events: Iterable[dict[str, Any]], candidate_id: str
) -> dict[str, Any] | None:
    candidate_id = _uuid(candidate_id, "candidate id")
    return next(
        (item for item in build_inbox(events) if item["candidate_id"] == candidate_id),
        None,
    )


def _candidate(event: dict[str, Any]) -> dict[str, Any]:
    candidate_id = _uuid(event.get("event_id"), "candidate id")
    session_id = _uuid(event.get("session_id"), "Codex Session id")
    analysis_id = _uuid(event.get("source_analysis_event_id"), "analysis event id")
    project_id = _project_id(event.get("project_id"))
    scope = {"kind": "project", "project_id": project_id}
    locators = _locators(event.get("evidence_locators"))
    if (
        event.get("type") != "archive-candidate"
        or event.get("source_kind") != "codex_session"
        or event.get("source_identity") != session_id
        or event.get("knowledge_scope") != scope
        or event.get("completion_state") != "completed"
        or event.get("analysis_state") not in {"provisional", "final"}
        or event.get("review_state") != "pending"
        or event.get("archive_authority") != "none"
        or event.get("execution_authority") != "none"
        or event.get("causal_event_ids") != [analysis_id]
        or any(locator["source_identity"] != session_id for locator in locators)
    ):
        raise ArchiveCandidateError("Archive Candidate is not grounded in its analysis")
    return {
        "candidate_id": candidate_id,
        "session_id": session_id,
        "project_id": project_id,
        "knowledge_scope": scope,
        "completion_state": "completed",
        "completion_rationale": _text(
            event.get("completion_rationale"), "completion rationale"
        ),
        "analysis_state": event["analysis_state"],
        "source_analysis_event_id": analysis_id,
        "evidence_locators": locators,
        "review_state": "pending",
        "archive_authority": "none",
        "execution_authority": "none",
        "created_at": event.get("ts"),
        "review_event_id": None,
        "reviewed_at": None,
    }


def _reviewable_candidate(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArchiveCandidateError("Archive Candidate review target must be an object")
    candidate_id = _uuid(value.get("candidate_id"), "candidate id")
    session_id = _uuid(value.get("session_id"), "Codex Session id")
    project_id = _project_id(value.get("project_id"))
    scope = {"kind": "project", "project_id": project_id}
    locators = _locators(value.get("evidence_locators"))
    if (
        value.get("knowledge_scope") != scope
        or value.get("archive_authority")
        != (
            "authorized"
            if value.get("review_state") == "accepted"
            else "none"
        )
        or value.get("execution_authority") != "none"
        or any(locator["source_identity"] != session_id for locator in locators)
    ):
        raise ArchiveCandidateError("Archive Candidate review target is invalid")
    return {
        **value,
        "candidate_id": candidate_id,
        "session_id": session_id,
        "project_id": project_id,
        "knowledge_scope": scope,
        "evidence_locators": locators,
    }


def _analysis_context(
    analysis: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    if (
        analysis.get("type") != "observation"
        or analysis.get("source") != "personal_assistant"
        or analysis.get("source_kind") != "codex_session"
        or analysis.get("analysis_state") not in {"provisional", "final"}
    ):
        return None
    scope = analysis.get("knowledge_scope")
    if not isinstance(scope, dict) or scope.get("kind") != "project":
        raise ArchiveCandidateError("Archive Candidate analysis requires project scope")
    return (
        _uuid(analysis.get("event_id"), "analysis event id"),
        _uuid(analysis.get("source_identity"), "Codex Session id"),
        _project_id(scope.get("project_id")),
        str(analysis["analysis_state"]),
    )


def _candidate_id(
    analysis_id: str,
    session_id: str,
    project_id: str,
    rationale: str,
    locators: list[dict[str, Any]],
) -> str:
    identity = json.dumps(
        {
            "schema": CANDIDATE_SCHEMA,
            "analysis_id": analysis_id,
            "session_id": session_id,
            "project_id": project_id,
            "rationale": " ".join(rationale.lower().split()),
            "locator_digests": sorted(locator["sha256"] for locator in locators),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return str(uuid.uuid5(_EVENT_NAMESPACE, identity))


def _locators(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 50:
        raise ArchiveCandidateError("Archive Candidate requires Evidence Locators")
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
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for locator in value:
        if (
            not isinstance(locator, dict)
            or set(locator) != expected
            or locator.get("schema") != LOCATOR_SCHEMA
            or locator.get("kind") != "codex_event"
        ):
            raise ArchiveCandidateError("Archive Candidate has a malformed Evidence Locator")
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
            raise ArchiveCandidateError("Archive Candidate has invalid Evidence Locator coordinates")
        key = json.dumps(locator, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            output.append(dict(locator))
            seen.add(key)
    return output


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArchiveCandidateError(f"invalid {field}") from exc


def _project_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ArchiveCandidateError("invalid Registered Project id")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_RATIONALE:
        raise ArchiveCandidateError(f"Archive Candidate {field} is empty or too long")
    return value.strip()
