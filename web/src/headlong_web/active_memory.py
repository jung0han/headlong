"""Fold authority events into HeadLong's flat Active Memory projection.

The Activity Ledger is canonical.  Files written here are deliberately plain,
replaceable Markdown projections so a deleted or corrupt view never becomes a
second source of truth.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

PROJECTION_SCHEMA = "headlong.active-memory-projection/v1"
MEMORY_KINDS = frozenset({"decision", "preference", "constraint"})
AUTHORITY_BASES = frozenset({"explicit_user_statement", "user_accepted_candidate"})
_METADATA_PREFIX = "headlong_active_memory: "


class ActiveMemoryError(ValueError):
    """A ledger record or replaceable projection violated the memory contract."""


def projection_dir(identity_dir: Path) -> Path:
    return identity_dir / "assistant" / "projections" / "active-memory"


def active_records(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold ledger history into current authorized memories, in ledger order."""
    rows = list(events)
    superseded = _superseded_ids(rows)
    active: list[dict[str, Any]] = []
    for position, event in enumerate(rows):
        if event.get("type") != "memory-activated":
            continue
        if event.get("event_id") in superseded:
            continue
        positioned = {**event, "ledger_position": position}
        active.append(_public_memory(positioned))
    return active


def candidate_records(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return current model-inferred candidates without granting authority."""
    rows = list(events)
    superseded = _superseded_ids(rows)
    candidates: list[dict[str, Any]] = []
    for event in rows:
        if event.get("type") != "memory-candidate":
            continue
        event_id = event.get("event_id")
        causes = event.get("causal_event_ids")
        if event_id in superseded or (
            isinstance(causes, list) and any(cause in superseded for cause in causes)
        ):
            continue
        if (
            event.get("authority") != "candidate"
            or event.get("evidence_kind") != "model_inference"
        ):
            raise ActiveMemoryError("Memory Candidate has invalid authority")
        candidates.append(_public_candidate(event))
    return candidates


def select_scope(
    records: Iterable[dict[str, Any]],
    *,
    project_id: str | None = None,
    global_only: bool = False,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    """Filter scope before any future retrieval or model ranking occurs."""
    if global_only and project_id is not None:
        raise ActiveMemoryError("choose project or global scope, not both")
    selected: list[dict[str, Any]] = []
    for record in records:
        scope = record["knowledge_scope"]
        if global_only:
            keep = scope == {"kind": "global"}
        elif project_id is not None:
            keep = scope == {"kind": "project", "project_id": project_id} or (
                include_global and scope == {"kind": "global"}
            )
        else:
            keep = True
        if keep:
            selected.append(record)
    return selected


def rebuild_projection(
    identity_dir: Path, events: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Rebuild all current memory files and swap the directory as one view."""
    records = active_records(events)
    target = projection_dir(identity_dir)
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp = parent / f".{target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    backup = parent / f".{target.name}.old.{os.getpid()}.{uuid.uuid4().hex}"
    try:
        temp.mkdir()
        for record in records:
            path = temp / f"{record['event_id']}.md"
            metadata = json.dumps(
                {"schema": PROJECTION_SCHEMA, **record},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            with path.open("w", encoding="utf-8") as fh:
                fh.write(f"---\n{_METADATA_PREFIX}{metadata}\n---\n\n")
                fh.write(record["content"])
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        directory_fd = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if target.exists():
            os.replace(target, backup)
        os.replace(temp, target)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        shutil.rmtree(backup, ignore_errors=True)
    except OSError as exc:
        if backup.exists() and not target.exists():
            try:
                os.replace(backup, target)
            except OSError:
                pass
        raise ActiveMemoryError("cannot rebuild Active Memory projection") from exc
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    return records


def read_projection(identity_dir: Path) -> list[dict[str, Any]]:
    target = projection_dir(identity_dir)
    if not target.is_dir():
        return []
    records: list[dict[str, Any]] = []
    try:
        paths = sorted(target.glob("*.md"))
        for path in paths:
            lines = path.read_text(encoding="utf-8").splitlines()
            if len(lines) < 3 or lines[0] != "---" or lines[2] != "---":
                raise ActiveMemoryError("invalid Active Memory projection file")
            if not lines[1].startswith(_METADATA_PREFIX):
                raise ActiveMemoryError("invalid Active Memory projection metadata")
            metadata = json.loads(lines[1][len(_METADATA_PREFIX) :])
            if (
                not isinstance(metadata, dict)
                or metadata.pop("schema", None) != PROJECTION_SCHEMA
            ):
                raise ActiveMemoryError("unsupported Active Memory projection schema")
            expected = _validate_public_record(metadata)
            body = "\n".join(lines[4:]).rstrip("\n")
            if body != expected["content"] or path.stem != expected["event_id"]:
                raise ActiveMemoryError(
                    "Active Memory projection does not match metadata"
                )
            records.append(expected)
    except (OSError, json.JSONDecodeError) as exc:
        raise ActiveMemoryError("cannot read Active Memory projection") from exc
    records.sort(key=lambda item: item["ledger_position"])
    return records


def _superseded_ids(events: Iterable[dict[str, Any]]) -> set[str]:
    result: set[str] = set()
    for event in events:
        values = event.get("supersedes_event_ids", [])
        if not isinstance(values, list):
            raise ActiveMemoryError("ledger supersession ids must be an array")
        result.update(value for value in values if isinstance(value, str))
    return result


def _public_memory(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("authority") != "active":
        raise ActiveMemoryError("Active Memory event has invalid authority")
    if event.get("evidence_kind") != "user_statement":
        raise ActiveMemoryError("Active Memory lacks user authority evidence")
    if (
        event.get("source_kind") != "user_action"
        or event.get("source_identity") != "headlong-assistant"
    ):
        raise ActiveMemoryError("Active Memory lacks an authorized user-action source")
    memory_kind = event.get("memory_kind")
    authority_basis = event.get("authority_basis")
    if memory_kind not in MEMORY_KINDS or authority_basis not in AUTHORITY_BASES:
        raise ActiveMemoryError(
            "Active Memory has an unsupported kind or authority basis"
        )
    record = {
        "event_id": event.get("event_id"),
        "memory_key": event.get("memory_key"),
        "memory_kind": memory_kind,
        "content": event.get("content"),
        "knowledge_scope": event.get("knowledge_scope"),
        "authority": "active",
        "authority_basis": authority_basis,
        "evidence_kind": "user_statement",
        "verification": event.get("verification"),
        "evidence_locators": event.get("evidence_locators"),
        "causal_event_ids": event.get("causal_event_ids"),
        "supersedes_event_ids": event.get("supersedes_event_ids"),
        "ledger_position": event.get("ledger_position", 0),
    }
    return _validate_public_record(record)


def _public_candidate(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "content": event["content"],
        "knowledge_scope": event["knowledge_scope"],
        "evidence_kind": event["evidence_kind"],
        "verification": event["verification"],
        "authority": event["authority"],
        "evidence_locators": event["evidence_locators"],
        "causal_event_ids": event["causal_event_ids"],
        "supersedes_event_ids": event["supersedes_event_ids"],
        "source_kind": event["source_kind"],
        "source_identity": event["source_identity"],
    }


def _validate_public_record(record: dict[str, Any]) -> dict[str, Any]:
    required = {
        "event_id",
        "memory_key",
        "memory_kind",
        "content",
        "knowledge_scope",
        "authority",
        "authority_basis",
        "evidence_kind",
        "verification",
        "evidence_locators",
        "causal_event_ids",
        "supersedes_event_ids",
        "ledger_position",
    }
    if set(record) != required:
        raise ActiveMemoryError("Active Memory projection fields do not match v1")
    if not all(
        isinstance(record[key], str) and record[key].strip()
        for key in ("event_id", "memory_key", "content")
    ):
        raise ActiveMemoryError(
            "Active Memory projection has empty identity or content"
        )
    scope = record["knowledge_scope"]
    if not isinstance(scope, dict) or scope.get("kind") not in {"project", "global"}:
        raise ActiveMemoryError("Active Memory projection has invalid Knowledge Scope")
    if scope["kind"] == "project" and not isinstance(scope.get("project_id"), str):
        raise ActiveMemoryError("project Active Memory requires project_id")
    if record["memory_kind"] not in MEMORY_KINDS:
        raise ActiveMemoryError("Active Memory projection has invalid memory kind")
    if (
        record["authority"] != "active"
        or record["authority_basis"] not in AUTHORITY_BASES
    ):
        raise ActiveMemoryError("Active Memory projection has invalid authority")
    if record["evidence_kind"] != "user_statement":
        raise ActiveMemoryError(
            "Active Memory projection lacks user statement evidence"
        )
    if not isinstance(record["ledger_position"], int):
        raise ActiveMemoryError("Active Memory projection has invalid ledger position")
    for key in ("evidence_locators", "causal_event_ids", "supersedes_event_ids"):
        if not isinstance(record[key], list):
            raise ActiveMemoryError(f"Active Memory projection {key} must be an array")
    return record
