"""Observed Memory Failure records and bounded public projections."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from typing import Any, Iterable

from headlong_web.knowledge import KnowledgeScope, KnowledgeScopeError


FAILURE_SCHEMA = "headlong.memory-failure/v1"
QUALITY_SCHEMA = "headlong.memory-quality-observation/v1"
FAILURE_CLASSIFICATIONS = frozenset(
    {"wrong_scope", "evidence_contradicting", "behavior_affecting"}
)
QUALITY_CLASSIFICATIONS = frozenset({"duplicate", "wording_defect"})
MAX_PUBLIC_RECORDS = 100
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
DOWNSTREAM_EVENT_TYPES = frozenset(
    {"work-improvement-proposal", "observer-improvement-proposal", "action"}
)


class MemoryFailureError(ValueError):
    """A Memory Failure record violated the domain contract."""


def issue_event(
    target: dict[str, Any],
    classification: str,
    description: str,
    *,
    downstream_event: dict[str, Any] | None = None,
    downstream_locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_id = _uuid(target.get("event_id"), "memory event id")
    if target.get("type") not in {
        "memory-activated",
        "native-memory-added",
        "native-memory-edited",
        "native-memory-restored",
    }:
        raise MemoryFailureError("Memory Failure target is not an Active Memory")
    description = _description(description)
    try:
        scope = KnowledgeScope.parse(target.get("knowledge_scope")).to_dict()
    except KnowledgeScopeError as exc:
        raise MemoryFailureError(str(exc)) from exc
    evidence = target.get("evidence_locators")
    if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
        raise MemoryFailureError("Memory Failure target has invalid evidence")
    event_id = str(uuid.uuid4())
    is_failure = classification in FAILURE_CLASSIFICATIONS
    if not is_failure and classification not in QUALITY_CLASSIFICATIONS:
        raise MemoryFailureError("unsupported memory issue classification")
    downstream_id = None
    downstream_type = None
    downstream_evidence: list[dict[str, Any]] = []
    downstream_step_id = None
    downstream_action_snapshot = None
    if classification == "behavior_affecting":
        if downstream_event is None:
            raise MemoryFailureError(
                "a downstream proposal or action event is required for behavior_affecting"
            )
        downstream_type = downstream_event.get("type")
        if downstream_type not in DOWNSTREAM_EVENT_TYPES:
            raise MemoryFailureError(
                "behavior_affecting requires a downstream proposal or action event"
            )
        if downstream_type == "action":
            downstream_step_id = _uuid(
                downstream_event.get("step_id"), "downstream action step id"
            )
            downstream_id = downstream_step_id
            downstream_action_snapshot = _action_snapshot(downstream_event)
            if downstream_locator != action_locator(
                downstream_event,
                source_identity=str(downstream_locator.get("source_identity"))
                if isinstance(downstream_locator, dict)
                else "",
                trajectory_id=str(downstream_locator.get("trajectory_id"))
                if isinstance(downstream_locator, dict)
                else "",
            ):
                raise MemoryFailureError("downstream action locator is invalid")
            downstream_evidence = [downstream_locator]
        else:
            downstream_id = _uuid(
                downstream_event.get("event_id"), "downstream event id"
            )
            raw_downstream_evidence = downstream_event.get("evidence_locators")
            if (
                not isinstance(raw_downstream_evidence, list)
                or not raw_downstream_evidence
                or not all(
                    isinstance(item, dict) and item
                    for item in raw_downstream_evidence
                )
            ):
                raise MemoryFailureError(
                    "downstream proposal or action event requires an evidence locator"
                )
            downstream_evidence = raw_downstream_evidence
    elif downstream_event is not None:
        raise MemoryFailureError(
            "downstream event is only valid for behavior_affecting"
        )
    return {
        "type": "memory-failure" if is_failure else "memory-quality-observation",
        "step_id": event_id,
        "event_id": event_id,
        "event_schema": "headlong.activity-ledger/v1",
        (
            "memory_failure_schema"
            if is_failure
            else "memory_quality_observation_schema"
        ): FAILURE_SCHEMA if is_failure else QUALITY_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "memory_health",
        "source_identity": target_id,
        "memory_event_id": target_id,
        "record_kind": "memory_failure" if is_failure else "quality_observation",
        "classification": classification,
        "knowledge_scope": scope,
        "evidence_kind": "user_statement",
        "verification": "observed",
        "authority": "active",
        "execution_authority": "none",
        "causal_event_ids": [
            target_id,
            *([downstream_id] if downstream_id is not None else []),
        ],
        "supersedes_event_ids": [],
        "evidence_locators": evidence,
        "downstream_event_id": (
            downstream_id if downstream_type != "action" else None
        ),
        "downstream_event_type": downstream_type,
        "downstream_evidence_locators": downstream_evidence,
        "downstream_step_id": downstream_step_id,
        "downstream_action_snapshot": downstream_action_snapshot,
        "title": (
            f"Memory Failure: {classification.replace('_', ' ')}"
            if is_failure
            else f"Memory quality observation: {classification.replace('_', ' ')}"
        ),
        "content": description,
    }


def failures(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return _records(events, failures_only=True)


def issues(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return bounded Memory Failures and lesser quality observations."""
    return _records(events, failures_only=False)


def quality_observations(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in issues(events)
        if record["record_kind"] == "quality_observation"
    ]


def health(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    failure_records: list[dict[str, Any]] = []
    quality = 0
    for event in events:
        if event.get("memory_failure_schema") == FAILURE_SCHEMA:
            failure_records.append(_public(event, failure=True))
        elif event.get("memory_quality_observation_schema") == QUALITY_SCHEMA:
            _public(event, failure=False)
            quality += 1
    counts = Counter(record["classification"] for record in failure_records)
    return {
        "schema": "headlong.memory-failure-health/v1",
        "status": "degraded" if failure_records else "ok",
        "total": len(failure_records),
        "by_classification": {
            classification: counts.get(classification, 0)
            for classification in sorted(FAILURE_CLASSIFICATIONS)
        },
        "quality_observations": quality,
        "last_failure_at": (
            failure_records[-1].get("reported_at") if failure_records else None
        ),
    }


def _records(
    events: Iterable[dict[str, Any]], *, failures_only: bool
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for event in events:
        if event.get("memory_failure_schema") == FAILURE_SCHEMA:
            rows.append(_public(event, failure=True))
        elif not failures_only and event.get("memory_quality_observation_schema") == QUALITY_SCHEMA:
            rows.append(_public(event, failure=False))
    return list(reversed(rows[-MAX_PUBLIC_RECORDS:]))


def _public(event: dict[str, Any], *, failure: bool) -> dict[str, Any]:
    event_id = _uuid(event.get("event_id"), "memory issue event id")
    target_id = _uuid(event.get("memory_event_id"), "memory event id")
    allowed = FAILURE_CLASSIFICATIONS if failure else QUALITY_CLASSIFICATIONS
    classification = event.get("classification")
    expected_kind = "memory_failure" if failure else "quality_observation"
    downstream_event_id = event.get("downstream_event_id")
    downstream_type = event.get("downstream_event_type")
    downstream_evidence = event.get("downstream_evidence_locators")
    downstream_step_id = event.get("downstream_step_id")
    downstream_action_snapshot = event.get("downstream_action_snapshot")
    expected_causal = [target_id]
    if classification == "behavior_affecting":
        if downstream_type not in DOWNSTREAM_EVENT_TYPES:
            raise MemoryFailureError("Memory Failure downstream event is invalid")
        if (
            not isinstance(downstream_evidence, list)
            or not downstream_evidence
            or not all(isinstance(item, dict) and item for item in downstream_evidence)
        ):
            raise MemoryFailureError("Memory Failure downstream evidence is invalid")
        if downstream_type == "action":
            downstream_step_id = _uuid(
                downstream_step_id, "downstream action step id"
            )
            downstream_id = downstream_step_id
            if downstream_event_id is not None:
                raise MemoryFailureError("Memory Failure downstream action is invalid")
            downstream_action_snapshot = _stored_action_snapshot(
                downstream_action_snapshot, downstream_step_id
            )
            locator = downstream_evidence[0] if len(downstream_evidence) == 1 else None
            if (
                not isinstance(locator, dict)
                or locator.get("schema") != LOCATOR_SCHEMA
                or locator.get("kind") != "trajectory_step"
                or not isinstance(locator.get("source_identity"), str)
                or not locator.get("source_identity")
                or not isinstance(locator.get("trajectory_id"), str)
                or not locator.get("trajectory_id")
                or locator.get("step_id") != downstream_step_id
                or locator.get("sha256") != _digest(downstream_action_snapshot)
            ):
                raise MemoryFailureError("Memory Failure action locator is invalid")
        else:
            downstream_id = _uuid(downstream_event_id, "downstream event id")
            downstream_step_id = None
            downstream_action_snapshot = None
        expected_causal.append(downstream_id)
    else:
        downstream_id = None
        downstream_type = None
        downstream_evidence = []
        downstream_step_id = None
        downstream_action_snapshot = None
    if (
        classification not in allowed
        or event.get("record_kind") != expected_kind
        or event.get("source_kind") != "memory_health"
        or event.get("source_identity") != target_id
        or event.get("execution_authority") != "none"
        or event.get("causal_event_ids") != expected_causal
    ):
        raise MemoryFailureError("Memory Failure record is invalid")
    try:
        scope = KnowledgeScope.parse(event.get("knowledge_scope")).to_dict()
    except KnowledgeScopeError as exc:
        raise MemoryFailureError(str(exc)) from exc
    return {
        "event_id": event_id,
        "memory_event_id": target_id,
        "record_kind": expected_kind,
        "classification": classification,
        "knowledge_scope": scope,
        "evidence_locators": event.get("evidence_locators", []),
        "downstream_event_id": (
            downstream_id if downstream_type != "action" else None
        ),
        "downstream_event_type": downstream_type,
        "downstream_evidence_locators": downstream_evidence,
        "downstream_step_id": downstream_step_id,
        "downstream_action_snapshot": downstream_action_snapshot,
        "description": _description(event.get("content")),
        "reported_at": event.get("ts"),
    }


def _description(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 1200
        or any(char == "\x00" for char in value)
    ):
        raise MemoryFailureError("memory issue description is empty or exceeds compact limits")
    return value.strip()


def action_locator(
    action: dict[str, Any], *, source_identity: str, trajectory_id: str
) -> dict[str, str]:
    """Address one immutable native action step in the public trajectory."""
    snapshot = _action_snapshot(action)
    if not source_identity or not trajectory_id:
        raise MemoryFailureError("downstream action locator context is missing")
    return {
        "schema": LOCATOR_SCHEMA,
        "kind": "trajectory_step",
        "source_identity": source_identity,
        "trajectory_id": trajectory_id,
        "step_id": snapshot["step_id"],
        "sha256": _digest(snapshot),
    }


def _action_snapshot(action: dict[str, Any]) -> dict[str, str]:
    if action.get("type") != "action" or action.get("source") != "inner_monologue":
        raise MemoryFailureError("downstream action is not a native action step")
    step_id = _uuid(action.get("step_id"), "downstream action step id")
    content = action.get("content")
    if not isinstance(content, str) or not content.strip() or "\x00" in content:
        raise MemoryFailureError("downstream action content is invalid")
    normalized = content.strip()
    return {
        "type": "action",
        "step_id": step_id,
        "source": "inner_monologue",
        "content": normalized[:800],
        "content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
    }


def _stored_action_snapshot(value: Any, step_id: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {
        "type",
        "step_id",
        "source",
        "content",
        "content_sha256",
    }:
        raise MemoryFailureError("Memory Failure action snapshot is invalid")
    if (
        value.get("type") != "action"
        or value.get("step_id") != step_id
        or value.get("source") != "inner_monologue"
        or not isinstance(value.get("content"), str)
        or len(value["content"]) > 800
        or not isinstance(value.get("content_sha256"), str)
        or len(value["content_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in value["content_sha256"])
    ):
        raise MemoryFailureError("Memory Failure action snapshot is invalid")
    return value


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MemoryFailureError(f"{field} must be a UUID") from exc
