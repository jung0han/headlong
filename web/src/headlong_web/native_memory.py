"""Observe the identity's native Markdown memory store by stable memory id."""

from __future__ import annotations

import hashlib
import json
import re
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
