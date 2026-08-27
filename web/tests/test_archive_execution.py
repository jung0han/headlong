"""DONGWOO-995 contract tests for authorized Codex archival."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from headlong_web import archive_candidates, archive_execution
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app

SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
ANALYSIS_ID = "cccccccc-3333-4333-8333-333333333333"
ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
PROJECT_ID = "project-test"
LOCATOR = {
    "schema": "headlong.evidence-locator/v1",
    "kind": "codex_event",
    "source_identity": SESSION_ID,
    "source_root": "active",
    "relative_path": "session.jsonl",
    "line": 2,
    "byte_offset": 100,
    "byte_length": 20,
    "sha256": "a" * 64,
    "host": "test-host",
}


class FakeArchiveAdapter:
    def __init__(self, results: list[archive_execution.AdapterResult] | None = None):
        self.calls: list[tuple[str, str]] = []
        self.results = results or [archive_execution.AdapterResult("succeeded")]

    def execute(self, operation: str, session_id: str) -> archive_execution.AdapterResult:
        self.calls.append((operation, session_id))
        return self.results.pop(0)


def _identity(root: Path) -> None:
    identity = root / ".identities" / "observer"
    traj = identity / "trajectories" / "aaaaaaaa-root"
    traj.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\ncreated=2026-08-27T00:00:00Z\nroot_trajectory={ROOT_TRAJ}\n"
    )
    (traj / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"}) + "\n"
    )


def _assistant(tmp_path: Path, adapter: FakeArchiveAdapter) -> tuple[PersonalAssistant, str]:
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    service = PersonalAssistant(
        root,
        resolve_observer(root, "observer"),
        archive_adapter=adapter,
    )
    analysis = {
        "type": "observation",
        "event_id": ANALYSIS_ID,
        "source": "personal_assistant",
        "source_kind": "codex_session",
        "source_identity": SESSION_ID,
        "knowledge_scope": {"kind": "project", "project_id": PROJECT_ID},
        "analysis_state": "final",
        "archive_candidates": [
            {
                "completion_state": "completed",
                "rationale": "The requested work and verification completed.",
                "evidence_locators": [LOCATOR],
            }
        ],
    }
    candidate_event = archive_candidates.candidate_events(analysis)[0]
    service._ledger.append(candidate_event)
    return service, candidate_event["event_id"]


def test_accepting_signed_candidate_archives_once_by_stable_session_identity(
    tmp_path: Path,
):
    adapter = FakeArchiveAdapter()
    assistant, candidate_id = _assistant(tmp_path, adapter)

    accepted = assistant.review_archive_candidates([candidate_id], "accepted")
    replay = assistant.review_archive_candidates([candidate_id], "accepted")

    assert adapter.calls == [("archive", SESSION_ID)]
    candidate = accepted["archive_candidates"][0]
    assert candidate["review_state"] == "accepted"
    assert candidate["execution_state"] == "succeeded"
    assert candidate["execution_attempts"] == 1
    assert replay == accepted


def test_unapproved_candidate_states_never_call_archive_adapter(tmp_path: Path):
    adapter = FakeArchiveAdapter()
    assistant, candidate_id = _assistant(tmp_path, adapter)

    assert assistant.archive_candidate(candidate_id)["execution_state"] == "not_requested"
    for state in ("pending", "rejected", "dismissed"):
        reviewed = assistant.review_archive_candidates([candidate_id], state)
        assert reviewed["archive_candidates"][0]["review_state"] == state

    assert adapter.calls == []


def test_direct_archive_directive_and_unarchive_are_idempotent(tmp_path: Path):
    adapter = FakeArchiveAdapter(
        [
            archive_execution.AdapterResult("succeeded"),
            archive_execution.AdapterResult("succeeded"),
        ]
    )
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    assistant = PersonalAssistant(
        root,
        resolve_observer(root, "observer"),
        archive_adapter=adapter,
    )

    archived = assistant.archive_codex_session(SESSION_ID)
    replay = assistant.archive_codex_session(SESSION_ID)
    restored = assistant.unarchive_codex_session(SESSION_ID)
    restored_replay = assistant.unarchive_codex_session(SESSION_ID)

    assert adapter.calls == [("archive", SESSION_ID), ("unarchive", SESSION_ID)]
    assert archived["execution_state"] == "succeeded"
    assert replay["execution_state"] == "succeeded"
    assert restored["execution_state"] == "succeeded"
    assert restored_replay["execution_state"] == "succeeded"
    assert archived["authorization_kind"] == "direct"
    assert restored["authorization_kind"] == "direct"

    # An intervening recovery changes the state, so a later archive is a new
    # direct authorization rather than a replay of the original directive.
    adapter.results.append(archive_execution.AdapterResult("succeeded"))
    assistant.archive_codex_session(SESSION_ID)
    directives = [
        event
        for event in assistant._ledger.events()
        if event.get("archive_directive_schema") == archive_execution.DIRECTIVE_SCHEMA
    ]
    assert len(directives) == 3


def test_failed_directive_retries_without_a_second_authorization(tmp_path: Path):
    adapter = FakeArchiveAdapter(
        [
            archive_execution.AdapterResult(
                "failed", error_code="command_failed", message="Codex unavailable"
            ),
            archive_execution.AdapterResult("succeeded"),
        ]
    )
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"), archive_adapter=adapter)

    failed = assistant.archive_codex_session(SESSION_ID)
    retried = assistant.archive_codex_session(SESSION_ID)
    events = assistant._ledger.events()

    assert failed["execution_state"] == "failed"
    assert retried["execution_state"] == "succeeded"
    assert adapter.calls == [("archive", SESSION_ID), ("archive", SESSION_ID)]
    assert (
        sum(event.get("archive_directive_schema") == archive_execution.DIRECTIVE_SCHEMA for event in events)
        == 1
    )
    assert retried["attempt_number"] == 2


class FakeCommandExecutor:
    def __init__(self, results: list[archive_execution.CommandResult | Exception]):
        self.results = list(results)
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, command: tuple[str, ...], *, timeout: float) -> archive_execution.CommandResult:
        self.calls.append((command, timeout))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _help(operation: str) -> archive_execution.CommandResult:
    return archive_execution.CommandResult(
        returncode=0,
        stdout=f"Usage: codex {operation} [OPTIONS] <SESSION>\n",
        stderr="",
    )


def test_codex_adapter_probes_both_commands_before_narrow_execution():
    executor = FakeCommandExecutor(
        [
            _help("archive"),
            _help("unarchive"),
            archive_execution.CommandResult(0, "", ""),
        ]
    )
    adapter = archive_execution.CodexArchiveAdapter(executor=executor, binary="codex-test")

    result = adapter.execute("archive", SESSION_ID)

    assert result == archive_execution.AdapterResult("succeeded", exit_code=0)
    assert [call[0] for call in executor.calls] == [
        ("codex-test", "archive", "--help"),
        ("codex-test", "unarchive", "--help"),
        ("codex-test", "archive", SESSION_ID),
    ]


def test_codex_adapter_fails_closed_for_incomplete_or_missing_contract():
    executor = FakeCommandExecutor(
        [
            _help("archive"),
            archive_execution.CommandResult(0, "Usage: codex restore <SESSION>\n", ""),
        ]
    )
    adapter = archive_execution.CodexArchiveAdapter(executor=executor, binary="codex-test")

    result = adapter.execute("archive", SESSION_ID)

    assert result.state == "unsupported"
    assert result.error_code == "unsupported_contract"
    assert len(executor.calls) == 2

    unavailable = archive_execution.CodexArchiveAdapter(executor=executor, binary="")
    assert unavailable.execute("archive", SESSION_ID).error_code == "codex_unavailable"
    with pytest.raises(archive_execution.ArchiveExecutionError):
        unavailable.execute("archive", "not-a-session-id")


def test_codex_adapter_treats_already_archived_as_idempotent_success():
    executor = FakeCommandExecutor(
        [
            _help("archive"),
            _help("unarchive"),
            archive_execution.CommandResult(1, "", "Session is already archived"),
        ]
    )

    result = archive_execution.CodexArchiveAdapter(executor=executor, binary="codex-test").execute(
        "archive", SESSION_ID
    )

    assert result.state == "already_done"
    assert result.error_code is None


@pytest.mark.parametrize(
    ("command_result", "state", "code"),
    [
        (
            archive_execution.CommandResult(7, "", "permission denied"),
            "failed",
            "command_failed",
        ),
        (
            archive_execution.CommandResult(None, "partial", ""),
            "indeterminate",
            "partial_response",
        ),
        (TimeoutError("slow"), "timeout", "command_timeout"),
    ],
)
def test_codex_adapter_returns_actionable_bounded_failures(command_result, state, code):
    executor = FakeCommandExecutor([_help("archive"), _help("unarchive"), command_result])
    result = archive_execution.CodexArchiveAdapter(executor=executor, binary="codex-test").execute(
        "unarchive", SESSION_ID
    )

    assert result.state == state
    assert result.error_code == code
    assert result.message


def test_failed_candidate_execution_survives_restart_and_retries_same_authority(
    tmp_path: Path,
):
    first_adapter = FakeArchiveAdapter(
        [
            archive_execution.AdapterResult(
                "failed",
                error_code="command_failed",
                message="Codex daemon unavailable",
            )
        ]
    )
    assistant, candidate_id = _assistant(tmp_path, first_adapter)

    failed = assistant.review_archive_candidates([candidate_id], "accepted")["archive_candidates"][0]

    assert failed["review_state"] == "accepted"
    assert failed["archive_authority"] == "authorized"
    assert failed["execution_state"] == "failed"
    assert failed["execution_error"] == {
        "code": "command_failed",
        "message": "Codex daemon unavailable",
    }

    retry_adapter = FakeArchiveAdapter([archive_execution.AdapterResult("succeeded")])
    restarted = PersonalAssistant(
        assistant.root,
        resolve_observer(assistant.root, "observer"),
        archive_adapter=retry_adapter,
    )
    before_retry = restarted.archive_candidate(candidate_id)
    retried = restarted.retry_archive_candidate(candidate_id)
    replay = restarted.retry_archive_candidate(candidate_id)

    assert before_retry["execution_state"] == "failed"
    assert retry_adapter.calls == [("archive", SESSION_ID)]
    assert retried["execution_state"] == "succeeded"
    assert retried["execution_attempts"] == 2
    assert replay == retried


def test_missing_result_after_process_restart_is_observable_and_retryable(
    tmp_path: Path,
):
    seed_adapter = FakeArchiveAdapter()
    assistant, candidate_id = _assistant(tmp_path, seed_adapter)
    candidate = assistant.archive_candidate(candidate_id)
    review = archive_candidates.review_event(candidate, "accepted")
    assistant._ledger.append(review)
    assistant._ledger.append(
        archive_execution.attempt_event(
            operation="archive",
            session_id=SESSION_ID,
            authorization_event_id=review["event_id"],
            candidate_id=candidate_id,
            attempt_number=1,
        )
    )

    retry_adapter = FakeArchiveAdapter([archive_execution.AdapterResult("already_done")])
    restarted = PersonalAssistant(
        assistant.root,
        resolve_observer(assistant.root, "observer"),
        archive_adapter=retry_adapter,
    )

    interrupted = restarted.archive_candidate(candidate_id)
    recovered = restarted.retry_archive_candidate(candidate_id)

    assert interrupted["execution_state"] == "indeterminate"
    assert interrupted["execution_error"]["code"] == "result_missing"
    assert retry_adapter.calls == [("archive", SESSION_ID)]
    assert recovered["execution_state"] == "already_done"
    assert recovered["execution_attempts"] == 2


def test_cli_exposes_direct_archive_and_unarchive_with_separate_execution_state(tmp_path: Path, capsys):
    adapter = FakeArchiveAdapter(
        [
            archive_execution.AdapterResult("succeeded"),
            archive_execution.AdapterResult("succeeded"),
        ]
    )
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    common = ["--root", str(root), "--identity", "observer", "archive-session"]

    assert run([*common, "archive", SESSION_ID], archive_adapter=adapter) == 0
    archived = json.loads(capsys.readouterr().out)
    assert run([*common, "unarchive", SESSION_ID], archive_adapter=adapter) == 0
    restored = json.loads(capsys.readouterr().out)

    assert archived["execution_state"] == "succeeded"
    assert restored["execution_state"] == "succeeded"
    assert adapter.calls == [("archive", SESSION_ID), ("unarchive", SESSION_ID)]


def test_dashboard_executes_accepted_candidate_and_blocks_read_only_controls(
    tmp_path: Path,
):
    seed_adapter = FakeArchiveAdapter()
    assistant, candidate_id = _assistant(tmp_path, seed_adapter)
    adapter = FakeArchiveAdapter([archive_execution.AdapterResult("succeeded")])
    base = f"/api/identities/.identities~observer/archive-candidates/{candidate_id}"

    client = TestClient(create_app(assistant.root, archive_adapter=adapter))
    accepted = client.post(f"{base}/review", json={"state": "accepted"})

    assert accepted.status_code == 200
    assert accepted.json()["review_state"] == "accepted"
    assert accepted.json()["execution_state"] == "succeeded"
    adapter.results.append(archive_execution.AdapterResult("succeeded"))
    restored = client.post(
        f"/api/identities/.identities~observer/codex-sessions/{SESSION_ID}/unarchive",
        json={},
    )
    invalid = client.post(
        "/api/identities/.identities~observer/codex-sessions/not-a-uuid/archive",
        json={},
    )

    assert restored.status_code == 200
    assert restored.json()["execution_state"] == "succeeded"
    assert invalid.status_code == 422
    assert adapter.calls == [("archive", SESSION_ID), ("unarchive", SESSION_ID)]

    read_only = TestClient(create_app(assistant.root, read_only=True, archive_adapter=adapter))
    assert read_only.post(f"{base}/retry", json={}).status_code == 403
    assert (
        read_only.post(
            f"/api/identities/.identities~observer/codex-sessions/{SESSION_ID}/unarchive",
            json={},
        ).status_code
        == 403
    )
