"""Compact durable health markers written at Personal Assistant work boundaries."""

from __future__ import annotations

import fcntl
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from headlong_web import archive_candidates, archive_execution


NATIVE_MEMORY_SCHEMA = "headlong.native-memory-health/v1"
STRUCTURED_RESULT_SCHEMA = "headlong.structured-result-health/v1"
ARCHIVE_SCHEMA = "headlong.archive-health/v1"


def record_native_memory(
    state_dir: Path,
    *,
    active: int,
    mutations: dict[str, int] | None = None,
) -> None:
    kinds = ("added", "edited", "forgotten", "restored")
    path = state_dir / "source-health" / "native-memory.json"

    def update(previous: dict[str, Any]) -> dict[str, Any]:
        prior = previous.get("mutations") if isinstance(previous.get("mutations"), dict) else {}
        current = mutations or {}
        counts = {kind: _count(prior.get(kind)) + _count(current.get(kind)) for kind in kinds}
        progressed = any(_count(current.get(kind)) for kind in kinds)
        return {
            "schema": NATIVE_MEMORY_SCHEMA,
            "status": "ok",
            "active": active,
            "mutations": counts,
            "updated_at": _now(),
            "last_mutation_at": _now() if progressed else previous.get("last_mutation_at"),
        }

    _update(path, update)


def record_structured_result(
    identity_dir: Path,
    *,
    mode: str,
    success: bool,
    error_code: str | None = None,
) -> None:
    path = identity_dir / "assistant" / "source-health" / "structured-results.json"

    def update(previous: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        successes = _count(previous.get("successes")) + int(success)
        failures = _count(previous.get("failures")) + int(not success)
        return {
            "schema": STRUCTURED_RESULT_SCHEMA,
            "status": "ok" if success else "degraded",
            "mode": mode if mode in {"strict", "json_object"} else "unknown",
            "successes": successes,
            "failures": failures,
            "last_success_at": now if success else previous.get("last_success_at"),
            "last_failure_at": now if not success else previous.get("last_failure_at"),
            "last_error": None if success else error_code or "invalid_result",
        }

    _update(path, update)


def record_archive(state_dir: Path, events: list[dict[str, Any]]) -> None:
    candidates = archive_candidates.build_inbox(events)
    executions = archive_execution.execution_history(events)
    reviews = {state: 0 for state in ("pending", "accepted", "rejected", "dismissed")}
    for candidate in candidates:
        reviews[candidate["review_state"]] += 1
    states = {
        state: 0
        for state in (
            "succeeded",
            "already_done",
            "failed",
            "timeout",
            "unsupported",
            "indeterminate",
        )
    }
    for execution in executions:
        states[execution["execution_state"]] += 1
    review_times = [
        item["reviewed_at"] for item in candidates if isinstance(item.get("reviewed_at"), str)
    ]
    attempt_times = [
        item["attempted_at"] for item in executions if isinstance(item.get("attempted_at"), str)
    ]
    _write(
        state_dir / "source-health" / "archive.json",
        {
            "schema": ARCHIVE_SCHEMA,
            "candidate_review": {
                "status": "pending" if reviews["pending"] else "ok",
                "total": len(candidates),
                **reviews,
                "last_review_at": max(review_times, default=None),
            },
            "execution": {
                "status": (
                    "degraded"
                    if any(
                        states[state]
                        for state in (
                            "failed",
                            "timeout",
                            "unsupported",
                            "indeterminate",
                        )
                    )
                    else "ok"
                    if executions
                    else "idle"
                ),
                "attempts": len(executions),
                **states,
                "last_attempt_at": max(attempt_times, default=None),
            },
            "updated_at": _now(),
        },
    )


def _update(path: Path, updater: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            previous = {}
        _write(path, updater(previous if isinstance(previous, dict) else {}))


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
