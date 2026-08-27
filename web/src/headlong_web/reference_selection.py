"""Structured contract for model-assisted web Reference selection."""

from __future__ import annotations

from typing import Any

from headlong_web.model_gateway import StructuredResultSchema

SELECTION_SCHEMA = "headlong.web-reference-selection/v1"
MAX_TITLE = 160
MAX_SUMMARY = 1200


class ReferenceSelectionContractError(ValueError):
    """A model result violated the Reference selection contract."""


def result_schema() -> StructuredResultSchema:
    """Return the provider and local contract for one Reference judgment."""
    document = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selected": {"type": "boolean"},
            "title": {"type": "string", "maxLength": MAX_TITLE},
            "summary": {"type": "string", "maxLength": MAX_SUMMARY},
        },
        "required": ["selected", "title", "summary"],
    }
    return StructuredResultSchema(
        name="web_reference_selection",
        document=document,
        validate=validate_result,
    )


def validate_result(value: Any) -> dict[str, str | bool]:
    """Validate and normalize one locally bounded Reference judgment."""
    if not isinstance(value, dict) or set(value) != {
        "selected",
        "title",
        "summary",
    }:
        raise ReferenceSelectionContractError(
            "model Reference selection does not match the required schema"
        )
    selected = value["selected"]
    title = value["title"]
    summary = value["summary"]
    if (
        not isinstance(selected, bool)
        or not isinstance(title, str)
        or not isinstance(summary, str)
        or len(title) > MAX_TITLE
        or len(summary) > MAX_SUMMARY
        or (selected and (not title.strip() or not summary.strip()))
    ):
        raise ReferenceSelectionContractError(
            "model Reference selection is empty or exceeds compact limits"
        )
    return {"selected": selected, "title": title.strip(), "summary": summary.strip()}
