"""Performance contracts for the deterministic assistant services."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from headlong_web import archive_candidates, assistant_services
from headlong_web.assistant import resolve_observer


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"


def _identity(root: Path) -> Path:
    identity = root / ".identities" / "observer"
    trajectory = identity / "trajectories" / "aaaaaaaa-root"
    trajectory.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\ncreated=2026-08-27T00:00:00Z\nroot_trajectory={ROOT_TRAJ}\n"
    )
    (trajectory / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"})
        + "\n"
    )
    return identity


def test_activity_ledger_reuses_unchanged_snapshot_and_invalidates_on_append(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_path = _identity(root)
    identity = resolve_observer(root, "observer")
    real_iter = assistant_services.trajectory.iter_jsonl
    scans = 0

    def counted_iter(path: Path):
        nonlocal scans
        scans += 1
        yield from real_iter(path)

    monkeypatch.setattr(assistant_services.trajectory, "iter_jsonl", counted_iter)

    first = assistant_services.ActivityLedger(root, identity).events()
    first[0]["type"] = "caller-mutation"
    second = assistant_services.ActivityLedger(root, identity).events()
    assert second[0]["type"] == "trajectory"
    assert scans == 1

    trajectory_path = (
        identity_path / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    )
    with trajectory_path.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "observation",
                    "step_id": "bbbbbbbb-2222-4222-8222-222222222222",
                    "ts": "2026-08-27T00:00:00Z",
                }
            )
            + "\n"
        )

    refreshed = assistant_services.ActivityLedger(root, identity).events()
    assert scans == 2
    assert refreshed[-1]["type"] == "observation"


class _CountingEvents(list[dict]):
    def __init__(self, values: list[dict]):
        super().__init__(values)
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


class _Ledger:
    def __init__(self, events: _CountingEvents):
        self._events = events

    def events(self) -> _CountingEvents:
        return self._events


class _UnusedArchiveAdapter:
    def execute(self, *_args):  # pragma: no cover - this projection never executes
        raise AssertionError("archive adapter should not be called")


def _candidate_event(index: int) -> dict:
    session_id = str(uuid.UUID(int=index + 1))
    analysis_id = str(uuid.UUID(int=10_000 + index))
    locator = {
        "schema": "headlong.evidence-locator/v1",
        "kind": "codex_event",
        "source_identity": session_id,
        "source_root": "archived",
        "relative_path": f"session-{index}.jsonl",
        "line": 1,
        "byte_offset": 0,
        "byte_length": 1,
        "sha256": f"{index:064x}",
        "host": "test-host",
    }
    analysis = {
        "type": "observation",
        "event_id": analysis_id,
        "source": "personal_assistant",
        "source_kind": "codex_session",
        "source_identity": session_id,
        "knowledge_scope": {"kind": "project", "project_id": "project-test"},
        "analysis_state": "final",
        "archive_candidates": [
            {
                "completion_state": "completed",
                "rationale": f"Session {index} is complete.",
                "evidence_locators": [locator],
            }
        ],
    }
    return archive_candidates.candidate_events(analysis)[0]


def test_archive_candidate_projection_scans_ledger_a_constant_number_of_times() -> None:
    events = _CountingEvents([_candidate_event(index) for index in range(100)])
    governance = assistant_services.GovernanceService(
        _Ledger(events),
        clock=lambda: None,
        archive_adapter=_UnusedArchiveAdapter(),
    )

    projected = governance.archive_candidates()

    assert len(projected) == 100
    assert all(item["execution_state"] == "not_requested" for item in projected)
    assert events.scans <= 3
