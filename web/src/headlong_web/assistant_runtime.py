"""Continuous source-bridge loops and bounded public runtime health."""

from __future__ import annotations

import json
import os
import re
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from headlong_web import envfile
from headlong_web.assistant import AssistantError, PersonalAssistant
from headlong_web.discovery import IdentityInfo


RUNTIME_HEALTH_SCHEMA = "headlong.assistant-runtime-health/v1"
DEFAULT_CODEX_INTERVAL_SECONDS = 10.0
DEFAULT_WEB_INTERVAL_SECONDS = 900.0
DEFAULT_STORAGE_LIMIT_BYTES = 1_000_000_000
MAX_PUBLIC_CURSORS = 100
MAX_PUBLIC_WEB_SOURCES = 100
MAX_STORAGE_SCAN_ENTRIES = 10_000


def run_bridge(
    assistant: PersonalAssistant,
    bridge: str,
    *,
    interval_seconds: float,
    active_root: Path | None = None,
    archived_root: Path | None = None,
    stop_event: threading.Event | None = None,
) -> None:
    """Run one bridge until stopped, retaining source state between cycles.

    Domain failures are recorded as compact error codes and retried. Process-level
    failures are still visible to systemd, whose restart policy protects against
    interpreter crashes and external termination.
    """
    if bridge not in {"codex", "web"}:
        raise ValueError("bridge must be codex or web")
    if not 0.1 <= interval_seconds <= 86_400:
        raise ValueError("bridge interval must be between 0.1 and 86400 seconds")
    if bridge == "codex" and (active_root is None or archived_root is None):
        raise ValueError("Codex bridge requires active and archived roots")

    stopped = stop_event or threading.Event()
    _install_signal_handlers(stopped)
    _write_runtime_health(assistant, bridge, status="starting")
    while not stopped.is_set():
        started = _now()
        try:
            if bridge == "codex":
                assert active_root is not None and archived_root is not None
                result = assistant.process_codex_once(active_root, archived_root)
                degraded = any(
                    section.get("status") != "ok" for section in result.values()
                )
            else:
                result = assistant.observe_web_once()
                degraded = bool(result.get("failed"))
            _write_runtime_health(
                assistant,
                bridge,
                status="degraded" if degraded else "ok",
                last_attempt_at=started,
                last_success_at=None if degraded else _now(),
                current_error="source_cycle_degraded" if degraded else None,
            )
        except AssistantError as exc:
            # Expected source/model contract failures are retried in place.
            # Programming errors and storage/runtime failures escape so systemd
            # restarts the affected component instead of hiding a broken loop.
            _write_runtime_health(
                assistant,
                bridge,
                status="error",
                last_attempt_at=started,
                current_error=_safe_error_code(exc),
            )
        except Exception as exc:
            _write_runtime_health(
                assistant,
                bridge,
                status="error",
                last_attempt_at=started,
                current_error=_safe_error_code(exc),
            )
            raise
        stopped.wait(interval_seconds)
    _write_runtime_health(assistant, bridge, status="stopped")


def public_health(root: Path, identity: IdentityInfo) -> dict[str, Any]:
    """Return an allowlisted, bounded status document with no source bodies."""
    state = identity.path / "assistant"
    model = _model_route(
        identity.path / "run" / "model_route_health.json",
        _configured_secrets(root, identity.path),
    )
    cursors = _codex_cursors(state / "cursors" / "codex")
    codex_runtime = _runtime_marker(state, "codex")
    web_runtime = _runtime_marker(state, "web")
    web_sources = _web_source_health(state / "source-health")
    storage = _storage_health(state)
    sources = {
        "codex": {
            "runtime": codex_runtime,
            "cursor_count": cursors["total"],
            "cursors": cursors["items"],
            "truncated": cursors["truncated"],
        },
        "web": {
            "runtime": web_runtime,
            "source_count": web_sources["total"],
            "sources": web_sources["items"],
            "truncated": web_sources["truncated"],
        },
    }
    states = [
        model.get("status"),
        codex_runtime.get("status"),
        web_runtime.get("status"),
    ]
    if storage["status"] != "ok" or "error" in states:
        status = "error"
    elif any(item in {"degraded", "unknown", "starting", "stopped"} for item in states):
        status = "degraded"
    else:
        status = "ok"
    return {
        "schema": RUNTIME_HEALTH_SCHEMA,
        "status": status,
        "identity": {"id": identity.id, "name": identity.name},
        "model_route": model,
        "sources": sources,
        "storage": storage,
    }


def _install_signal_handlers(stop_event: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _write_runtime_health(
    assistant: PersonalAssistant,
    bridge: str,
    *,
    status: str,
    last_attempt_at: str | None = None,
    last_success_at: str | None = None,
    current_error: str | None = None,
) -> None:
    path = assistant.state_dir / "source-health" / f"{bridge}-runtime.json"
    previous = _read_object(path)
    value: dict[str, Any] = {
        "schema": RUNTIME_HEALTH_SCHEMA,
        "bridge": bridge,
        "status": status,
        "updated_at": _now(),
        "last_attempt_at": last_attempt_at or previous.get("last_attempt_at"),
        "last_success_at": last_success_at or previous.get("last_success_at"),
        "current_error": current_error,
    }
    assistant._write_state_json(path, value)


def _runtime_marker(state: Path, bridge: str) -> dict[str, Any]:
    value = _read_object(state / "source-health" / f"{bridge}-runtime.json")
    if value.get("schema") != RUNTIME_HEALTH_SCHEMA or value.get("bridge") != bridge:
        return {"status": "unknown", "last_success_at": None, "current_error": None}
    return {
        "status": _choice(
            value.get("status"),
            {"starting", "ok", "degraded", "error", "stopped"},
            "unknown",
        ),
        "updated_at": _timestamp(value.get("updated_at")),
        "last_attempt_at": _timestamp(value.get("last_attempt_at")),
        "last_success_at": _timestamp(value.get("last_success_at")),
        "current_error": _code(value.get("current_error")),
    }


def _model_route(path: Path, secrets: set[str]) -> dict[str, Any]:
    value = _read_object(path)
    route = value.get("route") if isinstance(value.get("route"), dict) else {}
    paths = value.get("paths") if isinstance(value.get("paths"), dict) else {}
    direct = paths.get("direct") if isinstance(paths.get("direct"), dict) else {}
    shellm = paths.get("shellm") if isinstance(paths.get("shellm"), dict) else {}
    if not value or not isinstance(value.get("ok"), bool):
        return {"status": "unknown", "checked_at": None, "route": {}, "paths": {}}
    return {
        "status": "ok" if value["ok"] else "error",
        "checked_at": _timestamp(value.get("checked_at")),
        "route": {
            "provider": _label(route.get("provider"), 80, secrets),
            "model": _label(route.get("model"), 160, secrets),
            "endpoint_ref": _choice(
                route.get("endpoint_ref"),
                {"provider-default", "LLM_API_URL", "SHELLM_API_URL"},
                "unknown",
            ),
        },
        "paths": {
            "direct": {
                "status": _choice(
                    direct.get("status"), {"ok", "failed", "skipped"}, "unknown"
                )
            },
            "shellm": {
                "status": _choice(
                    shellm.get("status"), {"ok", "failed", "skipped"}, "unknown"
                )
            },
        },
    }


def _codex_cursors(path: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for cursor_path in sorted(path.glob("*.json")) if path.is_dir() else ():
        value = _read_object(cursor_path)
        locator = value.get("last_complete_locator")
        locator = locator if isinstance(locator, dict) else {}
        session_id = _session_id(value.get("session_id"))
        if session_id is None:
            continue
        items.append(
            {
                "session_id": session_id,
                "source_root": _choice(
                    value.get("source_root"), {"active", "archived"}, "unknown"
                ),
                "byte_offset": _nonnegative_int(value.get("byte_offset")),
                "line": _nonnegative_int(value.get("line")),
                "last_complete_sha256": _digest(locator.get("sha256")),
            }
        )
    return {
        "total": len(items),
        "items": items[:MAX_PUBLIC_CURSORS],
        "truncated": len(items) > MAX_PUBLIC_CURSORS,
    }


def _web_source_health(path: Path) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for health_path in sorted(path.glob("web-*.json")) if path.is_dir() else ():
        value = _read_object(health_path)
        source_id = _web_source_id(value.get("source_id"))
        if source_id is None:
            continue
        items.append(
            {
                "source_id": source_id,
                "source_kind": _choice(
                    value.get("source_kind"),
                    {"url", "rss", "documentation", "hacker_news", "unknown"},
                    "unknown",
                ),
                "status": _choice(
                    value.get("status"), {"healthy", "error"}, "unknown"
                ),
                "last_attempt_at": _timestamp(value.get("last_attempt_at")),
                "last_success_at": _timestamp(value.get("last_success_at")),
                "current_error": _code(value.get("error_code")),
            }
        )
    return {
        "total": len(items),
        "items": items[:MAX_PUBLIC_WEB_SOURCES],
        "truncated": len(items) > MAX_PUBLIC_WEB_SOURCES,
    }


def _storage_health(state: Path) -> dict[str, Any]:
    raw_limit = os.environ.get("HEADLONG_ASSISTANT_STORAGE_LIMIT_BYTES", "")
    try:
        limit = int(raw_limit) if raw_limit else DEFAULT_STORAGE_LIMIT_BYTES
        if limit <= 0:
            raise ValueError
    except ValueError:
        limit = DEFAULT_STORAGE_LIMIT_BYTES
        configured = False
    else:
        configured = bool(raw_limit)
    try:
        used, truncated = _tree_bytes(
            (state / "references", state / "reference-rejections")
        )
        status = (
            "scan_limit"
            if truncated
            else "limit_reached" if used >= limit else "ok"
        )
        return {
            "status": status,
            "used_bytes": used,
            "limit_bytes": limit,
            "limit_configured": configured,
            "remaining_bytes": None if truncated else max(0, limit - used),
        }
    except OSError:
        return {
            "status": "unavailable",
            "used_bytes": None,
            "limit_bytes": limit,
            "limit_configured": configured,
            "remaining_bytes": None,
        }


def _tree_bytes(roots: tuple[Path, ...]) -> tuple[int, bool]:
    total = 0
    inspected = 0
    stack = [root for root in roots if root.is_dir()]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                inspected += 1
                if inspected > MAX_STORAGE_SCAN_ENTRIES:
                    return total, True
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    return total, False


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, AssistantError):
        return "assistant_error"
    if isinstance(exc, OSError):
        return "io_error"
    return "unexpected_error"


def _text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value[:limit]


def _label(value: Any, limit: int, secrets: set[str]) -> str | None:
    text = _text(value, limit)
    if (
        text is None
        or not re.fullmatch(r"[A-Za-z0-9_.:/+-]+", text)
        or any(secret and secret in text for secret in secrets)
    ):
        return None
    return text


def _configured_secrets(root: Path, identity_dir: Path) -> set[str]:
    result: set[str] = set()
    for path in (root / ".env", identity_dir / ".env"):
        for key, value in envfile.parse_env_file(path):
            upper = key.upper()
            if any(
                token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")
            ):
                if value:
                    result.add(value)
    for key, value in os.environ.items():
        upper = key.upper()
        if any(
            token in upper for token in ("KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            if value:
                result.add(value)
    return result


def _timestamp(value: Any) -> str | None:
    text = _text(value, 40)
    if text is None or not re.fullmatch(r"[0-9T:.+Z-]+", text):
        return None
    return text


def _code(value: Any) -> str | None:
    text = _text(value, 80)
    if text is None or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", text):
        return None
    return text


def _session_id(value: Any) -> str | None:
    text = _text(value, 36)
    if text is None or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        text,
    ):
        return None
    return text


def _web_source_id(value: Any) -> str | None:
    text = _text(value, 24)
    if text is None or not re.fullmatch(r"web-[0-9a-f]{20}", text):
        return None
    return text


def _choice(value: Any, allowed: set[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _digest(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(char in "0123456789abcdef" for char in value) else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
