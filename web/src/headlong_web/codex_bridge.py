"""Incremental file protocol for the Codex Session Activity Source.

This module deliberately knows nothing about models, the Activity Ledger, or
projections. It selects one unambiguous stream per session identity and exposes
only complete byte records after a validated durable cursor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

CURSOR_SCHEMA = "headlong.codex-cursor/v1"
_READ_CHUNK = 64 * 1024


class CodexBridgeError(RuntimeError):
    """A local Codex source could not be followed safely."""


@dataclass(frozen=True)
class CodexSegment:
    """One physical append-only rollout shard of a logical Codex Session."""

    path: Path
    source_root: str
    relative_path: str
    device: int
    inode: int
    start_at: datetime | None = None
    end_at: datetime | None = None


@dataclass(frozen=True)
class CodexSource:
    """One logical Codex Session, possibly persisted as ordered rollout shards."""

    id: str
    cwd: Path
    path: Path
    source_root: str
    relative_path: str
    device: int
    inode: int
    parent_session_id: str | None
    task_root_id: str
    segments: tuple[CodexSegment, ...] = ()


def discover_sources(
    roots: dict[str, Path],
) -> tuple[list[CodexSource], list[str]]:
    """Resolve duplicate files into one unambiguous stream per session UUID."""
    found: list[CodexSource] = []
    for root_kind in ("archived", "active"):
        root = roots[root_kind]
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if not _contained(path.resolve(), root):
                continue
            first = _first_complete_line(path)
            if first is None:
                continue
            try:
                meta = json.loads(first)
                if meta.get("type") != "session_meta":
                    continue
                session_id = _canonical_uuid(meta["payload"]["id"])
                cwd = Path(meta["payload"]["cwd"]).expanduser().resolve(strict=False)
                parent_session_id = _parent_session_id(meta)
                stat = path.stat()
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            segment = CodexSegment(
                path=path,
                source_root=root_kind,
                relative_path=path.relative_to(root).as_posix(),
                device=stat.st_dev,
                inode=stat.st_ino,
                start_at=_event_time(first),
                end_at=_event_time(_last_complete_line(path)),
            )
            found.append(
                CodexSource(
                    id=session_id,
                    cwd=cwd,
                    path=segment.path,
                    source_root=segment.source_root,
                    relative_path=segment.relative_path,
                    device=segment.device,
                    inode=segment.inode,
                    parent_session_id=parent_session_id,
                    task_root_id=session_id,
                    segments=(segment,),
                )
            )

    grouped: dict[str, list[CodexSource]] = {}
    for source in found:
        grouped.setdefault(source.id, []).append(source)
    selected: list[CodexSource] = []
    errors: list[str] = []
    for session_id in sorted(grouped):
        candidates = grouped[session_id]
        first = candidates[0]
        if any(
            candidate.cwd != first.cwd
            or candidate.parent_session_id != first.parent_session_id
            for candidate in candidates[1:]
        ):
            errors.append(f"conflicting Codex Session identity: {session_id}")
            continue
        try:
            representatives: list[CodexSource] = []
            for candidate in sorted(candidates, key=_source_preference, reverse=True):
                if any(
                    _files_share_prefix(candidate.path, existing.path)
                    for existing in representatives
                ):
                    continue
                representatives.append(candidate)
            ordered = _ordered_shards(representatives)
            if ordered is None:
                errors.append(f"conflicting Codex Session identity: {session_id}")
                continue
            latest = ordered[-1]
            selected.append(
                replace(
                    latest,
                    segments=tuple(
                        source_segments(candidate)[0] for candidate in ordered
                    ),
                )
            )
        except OSError:
            errors.append(f"Codex Session changed during discovery: {session_id}")
    resolved: list[CodexSource] = []
    by_id = {source.id: source for source in selected}
    for source in selected:
        task_root_id, error = _task_root(source, by_id)
        if error:
            errors.append(error)
            continue
        resolved.append(replace(source, task_root_id=task_root_id))
    return resolved, errors


def source_segments(source: CodexSource) -> tuple[CodexSegment, ...]:
    """Return the source's physical shards in logical append order."""
    if source.segments:
        return source.segments
    return (
        CodexSegment(
            path=source.path,
            source_root=source.source_root,
            relative_path=source.relative_path,
            device=source.device,
            inode=source.inode,
        ),
    )


def segment_source(source: CodexSource, segment: CodexSegment) -> CodexSource:
    """Address one physical shard while preserving logical session identity."""
    return replace(
        source,
        path=segment.path,
        source_root=segment.source_root,
        relative_path=segment.relative_path,
        device=segment.device,
        inode=segment.inode,
        segments=source_segments(source),
    )


def cursor_segment_index(
    source: CodexSource, cursor: dict[str, Any] | None
) -> int | None:
    """Resolve a durable cursor to its physical shard without trusting filenames."""
    if cursor is None:
        return None
    canonical = cursor.get("canonical_path")
    source_root = cursor.get("source_root")
    relative_path = cursor.get("relative_path")
    for index, segment in enumerate(source_segments(source)):
        if canonical == str(segment.path.resolve()) or (
            source_root == segment.source_root
            and relative_path == segment.relative_path
        ):
            return index
    return None


def source_has_delta(source: CodexSource, cursor: dict[str, Any] | None) -> bool:
    """Return whether any physical shard has uncollected complete or partial bytes."""
    segments = source_segments(source)
    if cursor is None:
        return any(segment.path.stat().st_size for segment in segments)
    index = cursor_segment_index(source, cursor)
    if index is None:
        return True
    current = segment_source(source, segments[index])
    offset, _line = resume_position(current, cursor)
    return current.path.stat().st_size > offset or index < len(segments) - 1


def _parent_session_id(meta: dict[str, Any]) -> str | None:
    """Return the validated direct Codex task parent declared by session_meta."""
    payload = meta["payload"]
    parent = payload.get("parent_thread_id")
    forked_from = payload.get("forked_from_id")
    if parent is None and forked_from is None:
        return None
    canonical = _canonical_uuid(parent if parent is not None else forked_from)
    if forked_from is not None and _canonical_uuid(forked_from) != canonical:
        raise ValueError("Codex Session parent identities disagree")
    return canonical


def _task_root(
    source: CodexSource, sources: dict[str, CodexSource]
) -> tuple[str, str | None]:
    """Follow validated parent links so subagents from one task count once."""
    seen = {source.id}
    current = source
    while current.parent_session_id is not None:
        parent_id = current.parent_session_id
        if parent_id in seen:
            return source.id, f"cyclic Codex Session ancestry: {source.id}"
        seen.add(parent_id)
        parent = sources.get(parent_id)
        if parent is None:
            return parent_id, None
        current = parent
    return current.id, None


def resume_position(
    source: CodexSource, cursor: dict[str, Any] | None
) -> tuple[int, int]:
    """Return a verified append position, or zero when the source must replay."""
    if cursor is None:
        return (0, 0)
    try:
        offset = int(cursor["byte_offset"])
        line = int(cursor["line"])
        locator = cursor["last_complete_locator"]
        locator_offset = int(locator["byte_offset"])
        locator_length = int(locator["byte_length"])
        locator_line = int(locator["line"])
        locator_session = _canonical_uuid(locator["source_identity"])
        locator_digest = str(locator["sha256"])
        size = source.path.stat().st_size
    except (KeyError, TypeError, ValueError, OSError):
        return (0, 0)
    if (
        offset != locator_offset + locator_length
        or line != locator_line
        or locator_session != source.id
        or size < offset
        or locator_length < 1
        or len(locator_digest) != 64
    ):
        return (0, 0)
    try:
        with source.path.open("rb") as fh:
            fh.seek(locator_offset)
            raw = fh.read(locator_length)
    except OSError:
        return (0, 0)
    if hashlib.sha256(raw).hexdigest() != locator_digest:
        return (0, 0)
    return (offset, line)


def complete_rows(path: Path, offset: int) -> Iterator[tuple[int, bytes]]:
    """Read only the suffix and yield newline-terminated byte records."""
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            row_offset = offset
            pending = b""
            while True:
                chunk = fh.read(_READ_CHUNK)
                if not chunk:
                    return
                pending += chunk
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    raw = pending[: newline + 1]
                    pending = pending[newline + 1 :]
                    yield row_offset, raw
                    row_offset += len(raw)
    except OSError as exc:
        raise CodexBridgeError(f"cannot follow Codex Session: {path}") from exc


def _files_share_prefix(left: Path, right: Path) -> bool:
    """Compare only the common byte prefix without loading whole sessions."""
    try:
        remaining = min(left.stat().st_size, right.stat().st_size)
        with left.open("rb") as left_fh, right.open("rb") as right_fh:
            while remaining:
                amount = min(_READ_CHUNK, remaining)
                if left_fh.read(amount) != right_fh.read(amount):
                    return False
                remaining -= amount
    except OSError:
        return False
    return True


def _source_preference(source: CodexSource) -> tuple[int, bool, str]:
    return (
        source.path.stat().st_size,
        source.source_root == "archived",
        str(source.path),
    )


def _ordered_shards(sources: list[CodexSource]) -> list[CodexSource] | None:
    """Order only provably disjoint rollout files; ambiguity remains fail-closed."""
    if len(sources) == 1:
        return list(sources)
    if any(
        source_segments(source)[0].start_at is None
        or source_segments(source)[0].end_at is None
        or source_segments(source)[0].start_at
        > source_segments(source)[0].end_at
        for source in sources
    ):
        return None
    ordered = sorted(
        sources,
        key=lambda source: source_segments(source)[0].start_at or datetime.min.replace(
            tzinfo=timezone.utc
        ),
    )
    for earlier, later in zip(ordered, ordered[1:]):
        earlier_end = source_segments(earlier)[0].end_at
        later_start = source_segments(later)[0].start_at
        if earlier_end is None or later_start is None or earlier_end >= later_start:
            return None
    return ordered


def _first_complete_line(path: Path) -> bytes | None:
    try:
        with path.open("rb") as fh:
            raw = fh.readline()
    except OSError:
        return None
    return raw if raw.endswith(b"\n") else None


def _last_complete_line(path: Path) -> bytes | None:
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            position = fh.tell()
            pending = b""
            while position:
                amount = min(_READ_CHUNK, position)
                position -= amount
                fh.seek(position)
                pending = fh.read(amount) + pending
                complete = [
                    raw
                    for raw in pending.splitlines(keepends=True)
                    if raw.endswith(b"\n")
                ]
                if complete and (position == 0 or len(complete) > 1):
                    return complete[-1]
    except OSError:
        return None
    return None


def _event_time(raw: bytes | None) -> datetime | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw).get("timestamp")
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _canonical_uuid(value: Any) -> str:
    parsed = UUID(str(value))
    canonical = str(parsed)
    if str(value).lower() != canonical:
        raise ValueError("UUID is not canonical")
    return canonical


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
