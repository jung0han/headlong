"""The web/CLI archive capability ends at a narrow authenticated boundary."""

from __future__ import annotations

import json
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


def test_boundary_requires_matching_signed_directive_before_execution(tmp_path: Path) -> None:
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
    executor = FakeExecutor()

    response = archive_boundary.handle_request(
        root,
        {
            "schema": archive_boundary.REQUEST_SCHEMA,
            "identity_id": identity_id,
            "authorization_event_id": AUTH_ID,
            "operation": "archive",
            "session_id": SESSION_ID,
        },
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
