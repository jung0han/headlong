"""Observed Memory Failure records and bounded public projections."""

from __future__ import annotations

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


class MemoryFailureError(ValueError):
    """A Memory Failure record violated the domain contract."""


def issue_event(
    target: dict[str, Any], classification: str, description: str
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
        "causal_event_ids": [target_id],
        "supersedes_event_ids": [],
        "evidence_locators": evidence,
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
    if (
        classification not in allowed
        or event.get("record_kind") != expected_kind
        or event.get("source_kind") != "memory_health"
        or event.get("source_identity") != target_id
        or event.get("execution_authority") != "none"
        or event.get("causal_event_ids") != [target_id]
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


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise MemoryFailureError(f"{field} must be a UUID") from exc
