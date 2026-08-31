"""Observe the identity's native Markdown memory store by stable memory id."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from headlong_web.knowledge import KnowledgeScope, KnowledgeScopeError


SNAPSHOT_SCHEMA = "headlong.native-memory-snapshot/v1"
MUTATION_SCHEMA = "headlong.native-memory-mutation/v1"
_MEMORY_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class NativeMemoryError(ValueError):
    """A native memory file or durable snapshot is not safe to audit."""


def scan(
    memory_dir: Path,
    previous: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return current native memories keyed only by stable frontmatter id."""
    current: dict[str, dict[str, Any]] = {}
    prior_by_filename = {
        memory.get("filename"): memory
        for memory in (previous or {}).values()
        if isinstance(memory.get("filename"), str)
    }
    if not memory_dir.is_dir():
        return current
    if memory_dir.is_symlink():
        raise NativeMemoryError("native memory directory must not be a symlink")
    for path in sorted(memory_dir.glob("*.md")):
        if path.is_symlink():
            raise NativeMemoryError(f"native memory must not be a symlink: {path.name}")
        value = _read_memory(path)
        if value is None:
            # A file can be visible between truncate and close. Retain its
            # last stable-id value for this scan; an actually deleted file has
            # no filename to match and still becomes a tombstone.
            prior = prior_by_filename.get(path.name)
            if prior is not None:
                current[prior["memory_id"]] = prior
            continue
        memory_id = value["memory_id"]
        if memory_id in current:
            raise NativeMemoryError(f"duplicate native memory id: {memory_id}")
        current[memory_id] = value
    return current


def read_snapshot(path: Path) -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeMemoryError("cannot read native memory snapshot") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") != SNAPSHOT_SCHEMA
        or not isinstance(value.get("memories"), dict)
    ):
        raise NativeMemoryError("unsupported native memory snapshot")
    memories = value["memories"]
    for memory_id, memory in memories.items():
        if (
            not isinstance(memory_id, str)
            or not isinstance(memory, dict)
            or memory.get("memory_id") != memory_id
        ):
            raise NativeMemoryError("invalid native memory snapshot")
    return memories


def snapshot(current: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {"schema": SNAPSHOT_SCHEMA, "memories": current}


def digest(value: dict[str, Any] | None) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def replay(events: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], int]:
    """Fold captured native-memory mutations into their latest safe state."""
    current, tombstones, _last_events = replay_details(events)
    return current, len(tombstones)


def replay_details(
    events: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Return current values, retained forgotten values, and latest event ids."""
    current: dict[str, dict[str, Any]] = {}
    tombstones: dict[str, dict[str, Any]] = {}
    last_events: dict[str, str] = {}
    filenames: dict[str, str] = {}
    for event in events:
        if event.get("source_kind") != "headlong_memory":
            continue
        event_type = event.get("type")
        if event_type not in {
            "native-memory-added",
            "native-memory-edited",
            "native-memory-forgotten",
            "native-memory-restored",
        }:
            raise NativeMemoryError("unsupported native memory history event")
        if event.get("mutation_schema") != MUTATION_SCHEMA:
            raise NativeMemoryError("unsupported native memory mutation history")
        memory_id = event.get("memory_id")
        if not isinstance(memory_id, str) or not _MEMORY_ID_RE.fullmatch(memory_id):
            raise NativeMemoryError("native memory history has invalid identity")
        if event.get("source_identity") != memory_id:
            raise NativeMemoryError("native memory history identity does not match")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or event.get("step_id") != event_id:
            raise NativeMemoryError("native memory history has invalid event identity")
        previous_event_id = last_events.get(memory_id)
        expected_supersedes = [previous_event_id] if previous_event_id else []
        if event.get("supersedes_event_ids") != expected_supersedes:
            raise NativeMemoryError("native memory history chain is incomplete")
        prior = event.get("prior_value")
        replacement = event.get("replacement_value")
        if event_type == "native-memory-added":
            if prior is not None or memory_id in current:
                raise NativeMemoryError("native memory add history is incomplete")
            value = _validated_value(replacement, memory_id)
            tombstones.pop(memory_id, None)
        elif event_type == "native-memory-edited":
            if memory_id not in current or prior != current[memory_id]:
                raise NativeMemoryError("native memory edit history is incomplete")
            value = _validated_value(replacement, memory_id)
        elif event_type == "native-memory-forgotten":
            if (
                replacement is not None
                or memory_id not in current
                or prior != current[memory_id]
            ):
                raise NativeMemoryError("native memory forget history is incomplete")
            filenames.pop(current[memory_id]["filename"], None)
            tombstones[memory_id] = current[memory_id]
            del current[memory_id]
            last_events[memory_id] = event_id
            continue
        else:
            retained = tombstones.get(memory_id)
            if (
                prior is not None
                or memory_id in current
                or retained is None
                or replacement != retained
                or event.get("restored_from_event_id") != previous_event_id
            ):
                raise NativeMemoryError("native memory restore history is incomplete")
            value = _validated_value(replacement, memory_id)
            tombstones.pop(memory_id)
        old = current.get(memory_id)
        if old is not None:
            filenames.pop(old["filename"], None)
        other_id = filenames.get(value["filename"])
        if other_id is not None and other_id != memory_id:
            raise NativeMemoryError("native memory history has duplicate filename")
        current[memory_id] = value
        filenames[value["filename"]] = memory_id
        last_events[memory_id] = event_id
    return current, tombstones, last_events


def preflight_recovery(
    events: list[dict[str, Any]],
    *,
    live: dict[str, dict[str, Any]],
    snapshot_memories: dict[str, dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    """Validate that replay accounts for every memory known outside the ledger.

    A syntactically valid prefix (including an empty prefix) cannot prove that it
    is the whole Activity Ledger.  The live projection and durable snapshot are
    therefore a recovery watermark: every stable id seen by either must occur in
    the replay as an active value or retained tombstone before a store swap.
    """
    current, tombstones, last_events = replay_details(events)
    accounted = current.keys() | tombstones.keys()
    missing = sorted((live.keys() | snapshot_memories.keys()) - accounted)
    if missing:
        raise NativeMemoryError(
            "native memory history is incomplete; unaccounted memories: "
            + ", ".join(missing[:5])
        )
    conflicts = sorted(
        memory_id
        for memory_id, value in live.items()
        if current.get(memory_id) != value
    )
    if conflicts:
        raise NativeMemoryError(
            "native memory history is incomplete; live values are not replayed: "
            + ", ".join(conflicts[:5])
        )
    return current, tombstones, last_events


def rebuild_store(memory_dir: Path, current: dict[str, dict[str, Any]]) -> None:
    """Atomically replace the Markdown projection after history validates."""
    if memory_dir.is_symlink():
        raise NativeMemoryError("native memory directory must not be a symlink")
    memory_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".memories-rebuild-", dir=memory_dir.parent))
    backup = memory_dir.parent / f".memories-backup-{os.getpid()}"
    try:
        for memory_id in sorted(current):
            value = _validated_value(current[memory_id], memory_id)
            (stage / value["filename"]).write_text(_markdown(value), encoding="utf-8")
        if backup.exists():
            raise NativeMemoryError("native memory rebuild backup already exists")
        if memory_dir.exists():
            os.replace(memory_dir, backup)
        try:
            os.replace(stage, memory_dir)
        except OSError:
            if backup.exists() and not memory_dir.exists():
                os.replace(backup, memory_dir)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    except (OSError, UnicodeError) as exc:
        raise NativeMemoryError("cannot rebuild native memory store") from exc
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def invalidate_retrieval(identity_dir: Path) -> None:
    """Force the native retrieval thinker to rebuild after projection changes."""
    for path in (
        identity_dir / "retrieval" / "index.tsv",
        identity_dir / "retrieval" / "seen",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise NativeMemoryError("cannot refresh native memory retrieval") from exc


def _validated_value(value: Any, memory_id: str) -> dict[str, Any]:
    expected = {
        "memory_id",
        "filename",
        "memory_type",
        "knowledge_scope",
        "evidence_locators",
        "content",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise NativeMemoryError("native memory history has invalid value")
    filename = value.get("filename")
    memory_type = value.get("memory_type")
    content = value.get("content")
    if (
        value.get("memory_id") != memory_id
        or not isinstance(filename, str)
        or Path(filename).name != filename
        or not filename.endswith(".md")
        or filename == ".md"
        or any(char in filename for char in "\x00\r\n")
        or not isinstance(memory_type, str)
        or not memory_type.strip()
        or memory_type != memory_type.strip()
        or len(memory_type) > 128
        or any(char in memory_type for char in "\r\n")
        or not isinstance(content, str)
    ):
        raise NativeMemoryError("native memory history has unsafe value")
    try:
        scope = KnowledgeScope.parse(value.get("knowledge_scope"))
    except KnowledgeScopeError as exc:
        raise NativeMemoryError(str(exc)) from exc
    evidence = value.get("evidence_locators")
    if not isinstance(evidence, list) or not all(
        isinstance(item, dict) for item in evidence
    ):
        raise NativeMemoryError("native memory history has invalid evidence locators")
    return {**value, "knowledge_scope": scope.to_dict()}


def _markdown(value: dict[str, Any]) -> str:
    scope = KnowledgeScope.parse(value["knowledge_scope"])
    scope_value = (
        "global" if scope.kind == "global" else f"project:{scope.project_id}"
    )
    summary = (value["content"].splitlines() or [""])[0][:80].replace("\t", " ")
    evidence = json.dumps(
        value["evidence_locators"], ensure_ascii=False, separators=(",", ":")
    )
    return (
        "---\n"
        f"id: {value['memory_id']}\n"
        f"summary: {summary}\n"
        f"type: {value['memory_type']}\n"
        f"knowledge_scope: {scope_value}\n"
        f"evidence_locators: {evidence}\n"
        "---\n\n"
        f"{value['content']}\n"
    )


def _read_memory(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NativeMemoryError(f"cannot read native memory: {path.name}") from exc
    parsed = _frontmatter(text)
    if parsed is None:
        # A direct write may be observed between truncate and close. Keep the
        # last durable value until a complete native memory is available.
        return None
    fields, body = parsed
    memory_id = fields.get("id", "")
    if not _MEMORY_ID_RE.fullmatch(memory_id):
        return None
    memory_type = fields.get("type", "memory").strip() or "memory"
    scope = _scope(fields.get("knowledge_scope"))
    evidence = _evidence(fields.get("evidence_locators"))
    return {
        "memory_id": memory_id,
        "filename": path.name,
        "memory_type": memory_type,
        "knowledge_scope": scope.to_dict(),
        "evidence_locators": evidence,
        "content": body.strip(),
    }


def _frontmatter(text: str) -> tuple[dict[str, str], str] | None:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration:
        return None
    fields: dict[str, str] = {}
    for line in lines[1:end]:
        if line[:1].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields, "\n".join(lines[end + 1 :]).strip()


def _scope(value: str | None) -> KnowledgeScope:
    if not value or value == "global":
        return KnowledgeScope.global_scope()
    if value.startswith("project:"):
        try:
            return KnowledgeScope.project(value.removeprefix("project:"))
        except KnowledgeScopeError as exc:
            raise NativeMemoryError(str(exc)) from exc
    raise NativeMemoryError("native memory has invalid Knowledge Scope")


def _evidence(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise NativeMemoryError("native memory has invalid evidence locators") from exc
    if not isinstance(decoded, list) or not all(
        isinstance(item, dict) for item in decoded
    ):
        raise NativeMemoryError("native memory has invalid evidence locators")
    return decoded
