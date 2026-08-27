"""Deterministic contract for revisable Codex Session analysis results.

The model may propose interpretations, candidates, and improvement evidence.
This module validates their shape and source grounding without granting memory
or proposal authority.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

ANALYSIS_SCHEMA = "headlong.codex-observation/v1"
ANALYSIS_STATE_SCHEMA = "headlong.codex-analysis-state/v1"
PROVISIONAL_AFTER = timedelta(minutes=5)
FINAL_AFTER = timedelta(minutes=30)
MAX_TITLE = 160
MAX_CONTENT = 1200


class AnalysisContractError(ValueError):
    """Model output or persisted lifecycle input violated the v1 contract."""


def validate_result(value: Any, allowed: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate a model result and replace locator tokens with canonical objects."""
    expected = {
        "title",
        "observation",
        "evidence_locators",
        "memory_candidates",
        "improvement_signals",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise AnalysisContractError("model analysis does not match the required schema")
    return {
        "title": _compact_text(value["title"], MAX_TITLE, "analysis title"),
        "observation": _compact_text(
            value["observation"], MAX_CONTENT, "analysis observation"
        ),
        "evidence_locators": _locators(
            value["evidence_locators"], allowed, "analysis observation"
        ),
        "memory_candidates": _findings(
            value["memory_candidates"], allowed, signal=False
        ),
        "improvement_signals": _findings(
            value["improvement_signals"], allowed, signal=True
        ),
    }


def due_kind(state: dict[str, Any], source_root: str, now: datetime) -> str | None:
    """Return the one analysis kind due for this revision, if any."""
    inactive = now - parse_time(state["last_activity_at"])
    if source_root == "archived" or inactive >= FINAL_AFTER:
        return None if state.get("final_event_id") else "final"
    if inactive >= PROVISIONAL_AFTER:
        return None if state.get("provisional_event_id") else "provisional"
    return None


def supersedes(state: dict[str, Any], analysis_kind: str) -> list[str]:
    """Return prior lifecycle records replaced by a successful analysis."""
    values: list[str | None] = []
    if analysis_kind == "final":
        values.append(state.get("provisional_event_id"))
    values.extend((state.get("latest_success_event_id"), state.get("failure_event_id")))
    return list(dict.fromkeys(value for value in values if value))


def health(state: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded public lifecycle view for one session revision."""
    return {
        "session_id": state["session_id"],
        "project_id": state["project_id"],
        "source_revision_digest": state["source_revision_digest"],
        "last_activity_at": state["last_activity_at"],
        "status": state["status"],
        "last_error": state.get("last_error"),
    }


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AnalysisContractError("invalid Codex analysis timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AnalysisContractError("Codex analysis timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _findings(
    values: Any,
    allowed: dict[str, dict[str, Any]],
    *,
    signal: bool,
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > 20:
        raise AnalysisContractError("model analysis findings must be a bounded array")
    output: list[dict[str, Any]] = []
    allowed_kinds = {
        "user_correction",
        "test_failure",
        "tool_failure",
        "reviewer_finding",
        "observer_failure",
        "observer_regression",
        "inferred_pattern",
        "open_loop",
    }
    finding_fields = {"kind", "content", "evidence_locators"}
    expected = finding_fields if signal else {
        "content",
        "evidence_locators",
    }
    for value in values:
        if not isinstance(value, dict) or (
            set(value) != expected
            and (not signal or set(value) != finding_fields | {"proposal_type"})
        ):
            raise AnalysisContractError("model analysis finding does not match the schema")
        item = {
            "content": _compact_text(value["content"], MAX_CONTENT, "analysis finding"),
            "evidence_locators": _locators(
                value["evidence_locators"], allowed, "analysis finding"
            ),
        }
        if signal:
            if value["kind"] not in allowed_kinds:
                raise AnalysisContractError(
                    "model analysis signal has an unsupported kind"
                )
            item["kind"] = value["kind"]
            proposal_type = value.get("proposal_type", "work")
            if proposal_type not in {"work", "observer"}:
                raise AnalysisContractError(
                    "model analysis signal has an unsupported proposal type"
                )
            item["proposal_type"] = proposal_type
        output.append(item)
    return output


def _locators(
    values: Any,
    allowed: dict[str, dict[str, Any]],
    field: str,
) -> list[dict[str, Any]]:
    if not isinstance(values, list) or not values or len(values) > 50:
        raise AnalysisContractError(
            f"{field} requires a bounded Evidence Locator array"
        )
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or value not in allowed:
            raise AnalysisContractError(f"{field} cited an unknown Evidence Locator")
        if value not in seen:
            resolved.append(allowed[value])
            seen.add(value)
    return resolved


def _compact_text(value: Any, limit: int, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise AnalysisContractError(
            f"model {field} is empty or exceeds compact limits"
        )
    return value.strip()
