"""The web/CLI archive capability ends at a narrow authenticated boundary."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from headlong_web import (
    archive_boundary,
    archive_candidates,
    archive_execution,
    authority,
)


SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
AUTH_ID = "cccccccc-3333-4333-8333-333333333333"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def execute(self, operation: str, session_id: str) -> archive_execution.AdapterResult:
        self.calls.append((operation, session_id))
        return archive_execution.AdapterResult("succeeded", exit_code=0)


def _journal(root: Path, identity_id: str, event: dict) -> None:
    journal = authority.AuthorityJournal(root, identity_id)
    journal.initialize()
    journal.append(event)


def _request(identity_id: str) -> dict:
    return {
        "schema": archive_boundary.REQUEST_SCHEMA,
        "identity_id": identity_id,
        "authorization_event_id": AUTH_ID,
        "operation": "archive",
        "session_id": SESSION_ID,
    }


def _authorized_attempt(root: Path, identity_id: str) -> dict:
    directive = archive_execution.directive_event(
        "archive", SESSION_ID, event_id=AUTH_ID
    )
    attempt = archive_execution.attempt_event(
        operation="archive",
        session_id=SESSION_ID,
        authorization_event_id=AUTH_ID,
        candidate_id=None,
        attempt_number=1,
    )
    journal = authority.AuthorityJournal(root, identity_id)
    journal.initialize()
    journal.append(directive)
    journal.append(attempt)
    return attempt


def test_boundary_requires_matching_signed_directive_before_execution(tmp_path: Path) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_id = ".identities~observer"
    attempt = _authorized_attempt(root, identity_id)
    executor = FakeExecutor()

    response = archive_boundary.handle_request(
        root,
        _request(identity_id),
        executor=executor,
    )

    assert response == {
        "schema": archive_boundary.RESPONSE_SCHEMA,
        "state": "succeeded",
        "error_code": None,
        "message": None,
        "exit_code": 0,
    }
    assert executor.calls == [("archive", SESSION_ID)]
    history = archive_execution.execution_history(
        authority.AuthorityJournal(root, identity_id).read()
    )
    assert history[0]["attempt_id"] == attempt["event_id"]
    assert history[0]["execution_state"] == "succeeded"


@pytest.mark.parametrize(
    "request_change",
    [
        {"authorization_event_id": "dddddddd-4444-4444-8444-444444444444"},
        {"operation": "unarchive"},
        {"command": "sh -c id"},
    ],
)
def test_boundary_rejects_missing_mismatched_or_extra_capability(
    tmp_path: Path, request_change: dict
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_id = ".identities~observer"
    _journal(
        root,
        identity_id,
        archive_execution.directive_event(
            "archive", SESSION_ID, event_id=AUTH_ID
        ),
    )
    request = {
        "schema": archive_boundary.REQUEST_SCHEMA,
        "identity_id": identity_id,
        "authorization_event_id": AUTH_ID,
        "operation": "archive",
        "session_id": SESSION_ID,
    }
    request.update(request_change)
    executor = FakeExecutor()

    response = archive_boundary.handle_request(root, request, executor=executor)

    assert response["state"] == "failed"
    assert response["error_code"] == "unauthorized_request"
    assert executor.calls == []


def test_client_sends_only_allowlisted_request_and_bounds_response(
    tmp_path: Path, monkeypatch
) -> None:
    socket_path = tmp_path / "archive.sock"
    captured: dict = {}

    def fake_exchange(path: Path, payload: bytes, timeout: float) -> bytes:
        assert path == socket_path
        assert timeout == 7
        captured.update(json.loads(payload))
        return json.dumps(
            {
                "schema": archive_boundary.RESPONSE_SCHEMA,
                "state": "succeeded",
                "error_code": None,
                "message": None,
                "exit_code": 0,
            }
        ).encode()

    monkeypatch.setattr(archive_boundary, "_exchange", fake_exchange)
    client = archive_boundary.ArchiveBoundaryClient(
        ".identities~observer", socket_path=socket_path, timeout=7
    )

    result = client.execute("archive", SESSION_ID, AUTH_ID)

    assert result.state == "succeeded"
    assert set(captured) == {
        "schema",
        "identity_id",
        "authorization_event_id",
        "operation",
        "session_id",
    }


def test_default_transport_deadline_covers_probe_and_execution_budget() -> None:
    client = archive_boundary.ArchiveBoundaryClient(".identities~observer")

    # Two bounded 10-second capability probes plus one 30-second command,
    # with transport margin for durable journal I/O and scheduling.
    assert client.timeout >= 60


def test_boundary_accepts_only_current_candidate_acceptance_for_archive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_id = ".identities~observer"
    analysis_id = "eeeeeeee-5555-4555-8555-555555555555"
    locator = {
        "schema": "headlong.evidence-locator/v1",
        "kind": "codex_event",
        "source_identity": SESSION_ID,
        "source_root": "active",
        "relative_path": "session.jsonl",
        "line": 1,
        "byte_offset": 0,
        "byte_length": 10,
        "sha256": "a" * 64,
        "host": "test",
    }
    candidate = archive_candidates.candidate_events(
        {
            "type": "observation",
            "event_id": analysis_id,
            "source": "personal_assistant",
            "source_kind": "codex_session",
            "source_identity": SESSION_ID,
            "knowledge_scope": {"kind": "project", "project_id": "project-one"},
            "analysis_state": "final",
            "archive_candidates": [
                {
                    "completion_state": "completed",
                    "rationale": "Work is complete.",
                    "evidence_locators": [locator],
                }
            ],
        }
    )[0]
    projected = archive_candidates.build_inbox([candidate])[0]
    accepted = archive_candidates.review_event(projected, "accepted")
    journal = authority.AuthorityJournal(root, identity_id)
    journal.initialize()
    journal.append(candidate)
    journal.append(accepted)
    journal.append(
        archive_execution.attempt_event(
            operation="archive",
            session_id=SESSION_ID,
            authorization_event_id=accepted["event_id"],
            candidate_id=candidate["event_id"],
            attempt_number=1,
        )
    )
    executor = FakeExecutor()
    request = {
        "schema": archive_boundary.REQUEST_SCHEMA,
        "identity_id": identity_id,
        "authorization_event_id": accepted["event_id"],
        "operation": "archive",
        "session_id": SESSION_ID,
    }

    assert archive_boundary.handle_request(root, request, executor=executor)[
        "state"
    ] == "succeeded"
    request["operation"] = "unarchive"
    assert archive_boundary.handle_request(root, request, executor=executor)[
        "error_code"
    ] == "unauthorized_request"
    assert executor.calls == [("archive", SESSION_ID)]


def test_transport_loss_reconciles_durable_attempt_without_duplicate_mutation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_id = ".identities~observer"
    _authorized_attempt(root, identity_id)
    socket_path = tmp_path / "archive.sock"

    class DelayedExecutor(FakeExecutor):
        def execute(
            self, operation: str, session_id: str
        ) -> archive_execution.AdapterResult:
            time.sleep(0.08)
            return super().execute(operation, session_id)

    executor = DelayedExecutor()

    def one_shot() -> None:
        archive_boundary.serve(
            root, socket_path, executor=executor, max_requests=1
        )

    first_server = threading.Thread(target=one_shot, daemon=True)
    first_server.start()
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.002)
    client = archive_boundary.ArchiveBoundaryClient(
        identity_id, socket_path=socket_path, timeout=0.02
    )

    lost = client.execute("archive", SESSION_ID, AUTH_ID)
    first_server.join(timeout=1)
    assert not first_server.is_alive()

    assert lost.state == "indeterminate"
    assert lost.error_code == "archive_boundary_transport_lost"
    history = archive_execution.execution_history(
        authority.AuthorityJournal(root, identity_id).read()
    )
    assert history[0]["execution_state"] == "succeeded"
    assert executor.calls == [("archive", SESSION_ID)]

    socket_path.unlink(missing_ok=True)
    second_server = threading.Thread(target=one_shot, daemon=True)
    second_server.start()
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.002)
    reconciled = archive_boundary.ArchiveBoundaryClient(
        identity_id, socket_path=socket_path, timeout=0.2
    ).execute("archive", SESSION_ID, AUTH_ID)
    second_server.join(timeout=1)
    assert not second_server.is_alive()

    assert reconciled.state == "succeeded"
    assert executor.calls == [("archive", SESSION_ID)]


def test_production_boundary_spawns_codex_without_authority_access(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_id = ".identities~observer"
    _authorized_attempt(root, identity_id)
    authority_dir = root / ".assistant-authority"
    codex_home = tmp_path / "codex-home"
    fake_bin = tmp_path / "bin"
    codex_home.mkdir()
    fake_bin.mkdir()
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ -r "$HEADLONG_AUTHORITY_DIR" ]]; then exit 41; fi
if printf forged >"$HEADLONG_AUTHORITY_DIR/forged" 2>/dev/null; then exit 42; fi
if [[ "${2:-}" == "--help" ]]; then
    printf 'Usage: codex %s [OPTIONS] <SESSION>\n' "$1"
    exit 0
fi
printf '%s:%s\n' "$1" "$2" >"$CODEX_HOME/archive-result"
"""
    )
    fake_codex.chmod(0o755)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
    socket_path = tmp_path / "archive.sock"
    server = threading.Thread(
        target=archive_boundary.serve,
        args=(root, socket_path),
        kwargs={"max_requests": 1},
        daemon=True,
    )
    server.start()
    for _ in range(100):
        if socket_path.exists():
            break
        time.sleep(0.002)

    result = archive_boundary.ArchiveBoundaryClient(
        identity_id, socket_path=socket_path, timeout=5
    ).execute("archive", SESSION_ID, AUTH_ID)
    server.join(timeout=5)

    assert not server.is_alive()
    assert result.state == "succeeded"
    assert (codex_home / "archive-result").read_text() == f"archive:{SESSION_ID}\n"
    assert authority_dir.is_dir()
