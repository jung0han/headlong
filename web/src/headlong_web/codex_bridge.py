"""Incremental file protocol for the Codex Session Activity Source.

This module deliberately knows nothing about models, the Activity Ledger, or
projections. It selects one unambiguous stream per session identity and exposes
only complete byte records after a validated durable cursor.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from uuid import UUID

CURSOR_SCHEMA = "headlong.codex-cursor/v1"
_READ_CHUNK = 64 * 1024


class CodexBridgeError(RuntimeError):
    """A local Codex source could not be followed safely."""


@dataclass(frozen=True)
class CodexSource:
    """One validated local file carrying a Codex Session stream."""

    id: str
    cwd: Path
    path: Path
    source_root: str
    relative_path: str
    device: int
    inode: int


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
                stat = path.stat()
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
                continue
            found.append(
                CodexSource(
                    id=session_id,
                    cwd=cwd,
                    path=path,
                    source_root=root_kind,
                    relative_path=path.relative_to(root).as_posix(),
                    device=stat.st_dev,
                    inode=stat.st_ino,
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
            candidate.cwd != first.cwd or not _files_share_prefix(first.path, candidate.path)
            for candidate in candidates[1:]
        ):
            errors.append(f"conflicting Codex Session identity: {session_id}")
            continue
        try:
            selected.append(
                max(
                    candidates,
                    key=lambda candidate: (
                        candidate.path.stat().st_size,
                        candidate.source_root == "archived",
                        str(candidate.path),
                    ),
                )
            )
        except OSError:
            errors.append(f"Codex Session changed during discovery: {session_id}")
    return selected, errors


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


def _first_complete_line(path: Path) -> bytes | None:
    try:
        with path.open("rb") as fh:
            raw = fh.readline()
    except OSError:
        return None
    return raw if raw.endswith(b"\n") else None


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
