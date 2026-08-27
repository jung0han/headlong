"""Append-only human evaluation and the proposal-only Shadow Gate report.

The Activity Ledger is the only input.  Reviews are events, while the report
and review queues are projections rebuilt on every request.  Passing this gate
is deliberately descriptive: it never enables another capability.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

OBSERVATION_EVALUATION_SCHEMA = "headlong.observation-evaluation/v1"
MEMORY_EVALUATION_SCHEMA = "headlong.active-memory-evaluation/v1"
REPORT_SCHEMA = "headlong.shadow-gate-report/v1"
MINIMUM_FINAL_CONSOLIDATIONS = 20
MINIMUM_DURATION = timedelta(days=7)
MINIMUM_USEFUL_ACCURATE_RATE = 0.8

# This is a product invariant, not a feature flag.  Nothing in this module
# mutates it when a report becomes ready.
PROPOSAL_ONLY_AUTHORITY = {
    "mode": "proposal_only",
    "external_writes_enabled": False,
    "hook_adapters_enabled": False,
    "project_mounts_enabled": False,
}


class ShadowGateError(ValueError):
    """A ledger target or evaluation violated the Shadow Gate contract."""


def observation_evaluation_event(
    events: Iterable[dict[str, Any]],
    observation_event_id: str,
    *,
    useful: bool,
    accurate: bool,
    reviewed_at: datetime,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one user judgment grounded in a Final Consolidation."""
    rows = _unique_events(events)
    target = _event(rows, observation_event_id)
    if not _is_final_observation(target):
        raise ShadowGateError("Observation review target is not a Final Consolidation")
    if type(useful) is not bool or type(accurate) is not bool:
        raise ShadowGateError("Observation review judgments must be boolean")
    return _evaluation_event(
        event_id=event_id,
        schema_field="observation_evaluation_schema",
        schema=OBSERVATION_EVALUATION_SCHEMA,
        target=target,
        target_field="observation_event_id",
        judgments={"useful": useful, "accurate": accurate},
        reviewed_at=reviewed_at,
        title="Final Consolidation evaluated",
        content="The user judged whether this Observation was useful and accurate.",
    )


def memory_evaluation_event(
    events: Iterable[dict[str, Any]],
    memory_event_id: str,
    *,
    correct: bool,
    reviewed_at: datetime,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build one user judgment of an Active Memory promotion."""
    rows = _unique_events(events)
    target = _event(rows, memory_event_id)
    if target.get("type") != "memory-activated" or target.get("authority") != "active":
        raise ShadowGateError("Active Memory review target is not a promotion event")
    if type(correct) is not bool:
        raise ShadowGateError("Active Memory correctness must be boolean")
    return _evaluation_event(
        event_id=event_id,
        schema_field="active_memory_evaluation_schema",
        schema=MEMORY_EVALUATION_SCHEMA,
        target=target,
        target_field="memory_event_id",
        judgments={"correct": correct},
        reviewed_at=reviewed_at,
        title="Active Memory promotion evaluated",
        content="The user judged whether this Active Memory was promoted correctly.",
    )


def observations(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return Final Consolidations with their latest append-only evaluation."""
    rows = _unique_events(events)
    latest = _latest_evaluations(
        rows, OBSERVATION_EVALUATION_SCHEMA, "observation_event_id"
    )
    output = []
    for event in rows:
        if not _is_final_observation(event):
            continue
        review = latest.get(event["event_id"])
        output.append(
            {
                "event_id": event["event_id"],
                "title": event.get("title"),
                "content": event.get("content"),
                "source_identity": event.get("source_identity"),
                "knowledge_scope": event.get("knowledge_scope"),
                "evidence_locators": event.get("evidence_locators", []),
                "observed_at": _observation_time(event),
                "evaluation": _public_observation_evaluation(review),
            }
        )
    return output


def active_memories(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every promotion with its latest correctness evaluation."""
    rows = _unique_events(events)
    latest = _latest_evaluations(rows, MEMORY_EVALUATION_SCHEMA, "memory_event_id")
    output = []
    for event in rows:
        if event.get("type") != "memory-activated" or event.get("authority") != "active":
            continue
        review = latest.get(event["event_id"])
        output.append(
            {
                "event_id": event["event_id"],
                "memory_key": event.get("memory_key"),
                "memory_kind": event.get("memory_kind"),
                "content": event.get("content"),
                "knowledge_scope": event.get("knowledge_scope"),
                "activated_at": event.get("ts"),
                "evaluation": _public_memory_evaluation(review),
            }
        )
    return output


def report(events: Iterable[dict[str, Any]], now: datetime) -> dict[str, Any]:
    """Rebuild the live Shadow Gate metric report from ledger events."""
    now = _aware_utc(now)
    rows = _unique_events(events)
    final_observations = observations(rows)
    promoted_memories = active_memories(rows)
    reviewed = [item for item in final_observations if item["evaluation"] is not None]
    useful_accurate = sum(
        1
        for item in reviewed
        if item["evaluation"]["useful"] and item["evaluation"]["accurate"]
    )
    rate = useful_accurate / len(reviewed) if reviewed else None
    incorrect = sum(
        1
        for item in promoted_memories
        if item["evaluation"] is not None and not item["evaluation"]["correct"]
    )

    started_at = _started_at(final_observations)
    elapsed_seconds = (
        max(0.0, (now - started_at).total_seconds()) if started_at is not None else 0.0
    )
    duration_threshold_reached = elapsed_seconds >= MINIMUM_DURATION.total_seconds()
    count_threshold_reached = len(final_observations) >= MINIMUM_FINAL_CONSOLIDATIONS
    threshold_reached = duration_threshold_reached or count_threshold_reached
    quality_met = rate is not None and rate >= MINIMUM_USEFUL_ACCURATE_RATE
    memory_safety_met = incorrect == 0
    ready = threshold_reached and quality_met and memory_safety_met

    if started_at is None:
        status = "not_started"
    elif ready:
        status = "ready"
    elif threshold_reached:
        status = "not_ready"
    else:
        status = "collecting"
    return {
        "schema": REPORT_SCHEMA,
        "status": status,
        "ready": ready,
        "shadow_started_at": _format_time(started_at) if started_at else None,
        "evaluated_at": _format_time(now),
        "elapsed_seconds": elapsed_seconds,
        "elapsed_days": elapsed_seconds / 86_400,
        "final_consolidation_count": len(final_observations),
        "reviewed_observation_count": len(reviewed),
        "useful_and_accurate_count": useful_accurate,
        "useful_and_accurate_rate": rate,
        "incorrect_active_memory_count": incorrect,
        "threshold": {
            "duration_days": 7,
            "final_consolidations": MINIMUM_FINAL_CONSOLIDATIONS,
            "rule": "whichever_occurs_first",
            "duration_reached": duration_threshold_reached,
            "final_count_reached": count_threshold_reached,
            "reached": threshold_reached,
        },
        "criteria": {
            "minimum_useful_and_accurate_rate": MINIMUM_USEFUL_ACCURATE_RATE,
            "quality_met": quality_met,
            "requires_zero_incorrect_active_memories": True,
            "memory_safety_met": memory_safety_met,
        },
        "authority": dict(PROPOSAL_ONLY_AUTHORITY),
    }


def _evaluation_event(
    *,
    event_id: str | None,
    schema_field: str,
    schema: str,
    target: dict[str, Any],
    target_field: str,
    judgments: dict[str, bool],
    reviewed_at: datetime,
    title: str,
    content: str,
) -> dict[str, Any]:
    review_id = _uuid(event_id or str(uuid.uuid4()), "evaluation event id")
    target_id = _uuid(target.get("event_id"), "evaluation target id")
    event = {
        "type": (
            "observation-evaluation"
            if target_field == "observation_event_id"
            else "active-memory-evaluation"
        ),
        "step_id": review_id,
        "event_id": review_id,
        "event_schema": "headlong.activity-ledger/v1",
        schema_field: schema,
        "source": "personal_assistant",
        "source_kind": "shadow_gate",
        "source_identity": target_id,
        "knowledge_scope": target.get("knowledge_scope"),
        "evidence_kind": "user_statement",
        "verification": "observed",
        "authority": "active",
        "execution_authority": "none",
        "causal_event_ids": [target_id],
        "supersedes_event_ids": [],
        "evidence_locators": target.get("evidence_locators", []),
        "title": title,
        "content": content,
        target_field: target_id,
        "reviewed_at": _format_time(_aware_utc(reviewed_at)),
        **judgments,
    }
    return event


def _latest_evaluations(
    events: list[dict[str, Any]], schema: str, target_field: str
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    known = {event.get("event_id"): event for event in events}
    for event in events:
        field = (
            "observation_evaluation_schema"
            if target_field == "observation_event_id"
            else "active_memory_evaluation_schema"
        )
        if event.get(field) != schema:
            continue
        target_id = event.get(target_field)
        target = known.get(target_id)
        if target is None or event.get("causal_event_ids") != [target_id]:
            raise ShadowGateError("Shadow Gate evaluation references an unknown target")
        if event.get("execution_authority") != "none":
            raise ShadowGateError("Shadow Gate evaluation attempted to grant execution")
        latest[target_id] = event
    return latest


def _public_observation_evaluation(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    if type(event.get("useful")) is not bool or type(event.get("accurate")) is not bool:
        raise ShadowGateError("Observation evaluation has invalid judgments")
    return {
        "event_id": event["event_id"],
        "useful": event["useful"],
        "accurate": event["accurate"],
        "reviewed_at": event.get("reviewed_at") or event.get("ts"),
    }


def _public_memory_evaluation(event: dict[str, Any] | None) -> dict[str, Any] | None:
    if event is None:
        return None
    if type(event.get("correct")) is not bool:
        raise ShadowGateError("Active Memory evaluation has invalid correctness")
    return {
        "event_id": event["event_id"],
        "correct": event["correct"],
        "reviewed_at": event.get("reviewed_at") or event.get("ts"),
    }


def _unique_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    seen: set[str] = set()
    for event in events:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event_id in seen:
            continue
        seen.add(event_id)
        output.append(event)
    return output


def _event(events: list[dict[str, Any]], event_id: str) -> dict[str, Any]:
    event_id = _uuid(event_id, "evaluation target id")
    try:
        return next(event for event in events if event.get("event_id") == event_id)
    except StopIteration as exc:
        raise ShadowGateError("Shadow Gate evaluation target was not found") from exc


def _is_final_observation(event: dict[str, Any]) -> bool:
    return event.get("type") == "observation" and event.get("analysis_state") == "final"


def _observation_time(event: dict[str, Any]) -> str | None:
    value = event.get("analysis_completed_at") or event.get("ts")
    return value if isinstance(value, str) else None


def _started_at(observations: list[dict[str, Any]]) -> datetime | None:
    values = [
        _parse_time(item["observed_at"])
        for item in observations
        if item.get("observed_at") is not None
    ]
    return min(values) if values else None


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ShadowGateError("Shadow Gate event has an invalid timestamp") from exc
    return _aware_utc(parsed)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ShadowGateError("Shadow Gate clock must return an aware datetime")
    return value.astimezone(timezone.utc)


def _format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ShadowGateError(f"{field} must be a UUID") from exc
