"""Continuous bridge supervision and bounded public health."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from headlong_web import assistant_runtime
from headlong_web.discovery import scan_identities
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION = "12345678-1234-4123-8123-123456789abc"
SOURCE = "web-0123456789abcdefabcd"
NOW = "2026-08-27T01:02:03+00:00"


def _identity(root: Path) -> Any:
    identity = root / ".identities" / "observer"
    identity.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\ncreated={NOW}\nroot_trajectory={ROOT_TRAJ}\n"
    )
    return scan_identities(root)[0]


def _write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_public_health_allowlists_runtime_state(tmp_path: Path, monkeypatch) -> None:
    identity = _identity(tmp_path)
    secret = "sk-private-complete-reference-body"
    _write(
        identity.path / "run" / "model_route_health.json",
        {
            "ok": True,
            "checked_at": NOW,
            "route": {
                "provider": "openai",
                "model": "deepseek-flash-v4",
                "endpoint_ref": "LLM_API_URL",
                "credential_ref": "OPENAI_API_KEY",
                "structured_results": {"mode": "strict", "source": "configured"},
                "endpoint": f"https://example.test/?key={secret}",
            },
            "paths": {
                "direct": {"status": "ok", "error": secret},
                "shellm": {"status": "ok"},
            },
        },
    )
    state = identity.path / "assistant"
    _write(
        state / "source-health" / "codex-runtime.json",
        {
            "schema": assistant_runtime.RUNTIME_HEALTH_SCHEMA,
            "bridge": "codex",
            "status": "ok",
            "updated_at": NOW,
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "current_error": None,
            "raw": secret,
        },
    )
    _write(
        state / "source-health" / "web-runtime.json",
        {
            "schema": assistant_runtime.RUNTIME_HEALTH_SCHEMA,
            "bridge": "web",
            "status": "error",
            "updated_at": NOW,
            "last_attempt_at": NOW,
            "current_error": "fetch_failed",
        },
    )
    _write(
        state / "source-health" / f"{SOURCE}.json",
        {
            "source_id": SOURCE,
            "source_kind": "url",
            "status": "error",
            "last_attempt_at": NOW,
            "last_success_at": NOW,
            "error_code": "fetch_failed",
            "source_url": f"https://example.test/{secret}",
            "body": secret,
        },
    )
    _write(
        state / "cursors" / "codex" / f"{SESSION}.json",
        {
            "session_id": SESSION,
            "source_root": "active",
            "byte_offset": 42,
            "line": 3,
            "canonical_path": f"/home/{secret}/session.jsonl",
            "last_complete_locator": {"sha256": "a" * 64, "raw": secret},
        },
    )
    content = state / "references" / SOURCE / ("a" * 64) / "content.txt"
    content.parent.mkdir(parents=True)
    content.write_text("bounded text")
    monkeypatch.setenv("HEADLONG_ASSISTANT_STORAGE_LIMIT_BYTES", "1000")

    payload = assistant_runtime.public_health(tmp_path, identity)
    encoded = json.dumps(payload)
    assert secret not in encoded
    assert payload["model_route"]["route"] == {
        "provider": "openai",
        "model": "deepseek-flash-v4",
        "endpoint_ref": "LLM_API_URL",
        "structured_results": {"mode": "strict", "source": "configured"},
    }
    assert payload["sources"]["codex"]["cursors"][0]["byte_offset"] == 42
    assert payload["sources"]["web"]["sources"][0]["current_error"] == "fetch_failed"
    assert payload["storage"]["used_bytes"] == len("bounded text")
    assert payload["status"] == "error"


def test_public_health_route_matches_cli_boundary(tmp_path: Path) -> None:
    identity = _identity(tmp_path)
    expected = assistant_runtime.public_health(tmp_path, identity)
    client = TestClient(create_app(tmp_path))
    response = client.get(f"/api/identities/{identity.id}/assistant/health")
    assert response.status_code == 200
    assert response.json() == expected


def test_bridge_loop_reuses_state_and_records_success(tmp_path: Path) -> None:
    stop = threading.Event()

    class FakeAssistant:
        state_dir = tmp_path / "assistant"
        calls = 0

        def schedule_codex_once(
            self,
            active: Path,
            archived: Path,
            *,
            capacity: int | None = None,
        ) -> dict[str, Any]:
            assert active == tmp_path / "sessions"
            assert archived == tmp_path / "archived"
            assert capacity is None
            self.calls += 1
            stop.set()
            return {"collection": {"status": "ok"}, "analysis": {"status": "ok"}}

        def _write_state_json(self, path: Path, value: dict[str, Any]) -> None:
            _write(path, value)

    assistant = FakeAssistant()
    assistant_runtime.run_bridge(
        assistant,  # type: ignore[arg-type]
        "codex",
        interval_seconds=0.1,
        active_root=tmp_path / "sessions",
        archived_root=tmp_path / "archived",
        stop_event=stop,
    )
    assert assistant.calls == 1
    marker = json.loads(
        (assistant.state_dir / "source-health" / "codex-runtime.json").read_text()
    )
    assert marker["status"] == "stopped"
    assert marker["last_success_at"] is not None
    assert marker["current_error"] is None
