"""DONGWOO-908 contract tests for the incremental Codex Session bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web.assistant import EvidenceLocator
from headlong_web.assistant_cli import run
from headlong_web.server import create_app

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
ROTATED_ID = "cccccccc-3333-4333-8333-333333333333"
CONFLICT_ID = "dddddddd-4444-4444-8444-444444444444"


def _identity(root: Path) -> Path:
    identity = root / ".identities" / "observer"
    traj = identity / "trajectories" / "aaaaaaaa-root"
    traj.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\ncreated=2026-08-27T00:00:00Z\nroot_trajectory={ROOT_TRAJ}\n"
    )
    (traj / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"}) + "\n"
    )
    return identity


def _row(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _meta(session_id: str, cwd: Path) -> bytes:
    return _row(
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd), "git": {"branch": "test"}},
        }
    )


def _command(root: Path, *args: str) -> int:
    return run(["--root", str(root), "--identity", "observer", *args])


def _follow(root: Path, active: Path, archived: Path) -> int:
    return _command(
        root,
        "follow-codex",
        "--sessions-root",
        str(active),
        "--archived-sessions-root",
        str(archived),
    )


def test_large_session_backlog_yields_after_one_bounded_collection_batch(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    session = active / "large.jsonl"
    rows = [_meta(SESSION_ID, project)] + [
        _row({"type": "event_msg", "payload": {"sequence": index}})
        for index in range(34)
    ]
    session.write_bytes(b"\n".join(rows) + b"\n")

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    assert _follow(root, active, archived) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["appended"] == 32
    assert first["deferred"] == 1
    cursor = json.loads(
        (
            identity
            / "assistant"
            / "cursors"
            / "codex"
            / f"{SESSION_ID}.json"
        ).read_text()
    )
    assert cursor["line"] == 32

    assert _follow(root, active, archived) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["appended"] == 3
    assert second["deferred"] == 0
    ledger = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    source_events = [
        event
        for event in map(json.loads, ledger.read_text().splitlines())
        if event.get("type") == "activity-source-event"
    ]
    assert len(source_events) == 35
    assert len({event["event_id"] for event in source_events}) == 35


def test_active_session_append_partial_line_and_restart_are_lossless_and_idempotent(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    session = active / "active.jsonl"

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()

    future = _row({"future_schema": {"message": "café", "version": 99}})
    split = future.index("é".encode()) + 1
    complete_prefix = _meta(SESSION_ID, project) + b"\n"
    session.write_bytes(complete_prefix + future[:split])

    assert _follow(root, active, archived) == 0
    first = json.loads(capsys.readouterr().out)
    assert first == {
        "appended": 1,
        "deferred": 1,
        "discovered": 1,
        "duplicate": 0,
        "eligible": 1,
        "errors": [],
        "recovered": 0,
        "status": "ok",
    }

    cursor_file = identity / "assistant" / "cursors" / "codex" / f"{SESSION_ID}.json"
    cursor = json.loads(cursor_file.read_text())
    assert cursor["host"]
    assert cursor["session_id"] == SESSION_ID
    assert cursor["source_root"] == "active"
    assert Path(cursor["canonical_path"]) == session.resolve()
    assert cursor["device"] == session.stat().st_dev
    assert cursor["inode"] == session.stat().st_ino
    assert cursor["byte_offset"] == len(complete_prefix)
    assert cursor["line"] == 1
    assert cursor["last_complete_locator"]["sha256"]

    with session.open("ab") as fh:
        fh.write(future[split:] + b"\n")

    # A new public invocation is a bridge restart and must use the durable cursor.
    assert _follow(root, active, archived) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["appended"] == 1
    assert second["duplicate"] == 0
    assert second["deferred"] == 0

    assert _follow(root, active, archived) == 0
    third = json.loads(capsys.readouterr().out)
    assert third["appended"] == 0
    assert third["duplicate"] == 0

    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    source_events = [
        row
        for row in map(json.loads, ledger.read_text().splitlines())
        if row["type"] == "activity-source-event"
    ]
    assert len(source_events) == 2
    assert source_events[1]["source_event_type"] == "unknown"
    assert source_events[1]["evidence_locators"][0]["byte_offset"] == len(
        complete_prefix
    )
    assert "café" not in ledger.read_text()

    mindlog = TestClient(create_app(root)).get(
        "/api/identities/.identities~observer/mindlog?tail=20"
    )
    assert mindlog.status_code == 200
    visible = [
        step
        for step in mindlog.json()["steps"]
        if step["type"] == "activity-source-event"
    ]
    assert len(visible) == 2


def test_file_replacement_truncation_archival_and_rotation_recover_deterministically(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    session = active / "session.jsonl"
    meta = _meta(SESSION_ID, project) + b"\n"
    first = _row({"type": "event_msg", "payload": {"type": "turn_started"}}) + b"\n"
    second = _row({"type": "response_item", "payload": {"type": "message"}}) + b"\n"
    replacement = _row({"type": "event_msg", "payload": {"type": "replaced"}}) + b"\n"
    rotated_event = _row({"type": "event_msg", "payload": {"type": "rotated"}}) + b"\n"
    session.write_bytes(meta + first)

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    assert _follow(root, active, archived) == 0
    assert json.loads(capsys.readouterr().out)["appended"] == 2

    # Atomic inode replacement with the exact consumed prefix resumes at the offset.
    old_inode = session.stat().st_ino
    temp = active / "replacement.tmp"
    temp.write_bytes(meta + first + second)
    os.replace(temp, session)
    assert session.stat().st_ino != old_inode
    assert _follow(root, active, archived) == 0
    replaced = json.loads(capsys.readouterr().out)
    assert replaced["appended"] == 1
    assert replaced["duplicate"] == 0
    assert replaced["recovered"] == 0

    # Truncation replays from zero. Stable event ids suppress the repeated metadata.
    session.write_bytes(meta + replacement)
    assert _follow(root, active, archived) == 0
    truncated = json.loads(capsys.readouterr().out)
    assert truncated["recovered"] == 1
    assert truncated["duplicate"] == 1
    assert truncated["appended"] == 1

    # The same bytes moving to the archive root are one stream, not a new delivery.
    archived_session = archived / "moved.jsonl"
    session.rename(archived_session)
    assert _follow(root, active, archived) == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["appended"] == 0
    assert moved["duplicate"] == 0
    assert moved["recovered"] == 0
    moved_cursor = json.loads(
        (
            identity
            / "assistant"
            / "cursors"
            / "codex"
            / f"{SESSION_ID}.json"
        ).read_text()
    )
    assert moved_cursor["source_root"] == "archived"
    assert Path(moved_cursor["canonical_path"]) == archived_session.resolve()
    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    replacement_source = next(
        row
        for row in map(json.loads, ledger.read_text().splitlines())
        if row.get("type") == "activity-source-event"
        and row["evidence_locators"][0]["sha256"] == replacement_digest
    )
    moved_locator = EvidenceLocator.decode(replacement_source["evidence_locators"][0])
    assert _command(
        root,
        "resolve-evidence",
        moved_locator.encode(),
        "--sessions-root",
        str(active),
        "--archived-sessions-root",
        str(archived),
    ) == 0
    assert json.loads(capsys.readouterr().out)["raw"].encode() == replacement

    # A new session appearing at the rotated active path is independently collected.
    session.write_bytes(_meta(ROTATED_ID, project) + b"\n" + rotated_event)
    assert _follow(root, active, archived) == 0
    rotated = json.loads(capsys.readouterr().out)
    assert rotated["discovered"] == 2
    assert rotated["eligible"] == 2
    assert rotated["appended"] == 2

    source_events = [
        row
        for row in map(json.loads, ledger.read_text().splitlines())
        if row["type"] == "activity-source-event"
    ]
    assert len(source_events) == 6


def test_duplicate_session_identity_uses_one_compatible_stream_and_reports_conflicts(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    meta = _meta(SESSION_ID, project) + b"\n"
    first = _row({"type": "event_msg", "payload": {"type": "turn_started"}}) + b"\n"
    final = _row({"type": "event_msg", "payload": {"type": "task_complete"}}) + b"\n"
    (active / "same-active.jsonl").write_bytes(meta + first)
    (archived / "same-archived.jsonl").write_bytes(meta + first + final)

    conflict_meta = _meta(CONFLICT_ID, project) + b"\n"
    (active / "conflict.jsonl").write_bytes(
        conflict_meta + _row({"type": "event_msg", "payload": {"value": "active"}}) + b"\n"
    )
    (archived / "conflict.jsonl").write_bytes(
        conflict_meta
        + _row({"type": "event_msg", "payload": {"value": "archived"}})
        + b"\n"
    )

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    assert _follow(root, active, archived) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["discovered"] == 2
    assert result["eligible"] == 1
    assert result["appended"] == 3
    assert result["status"] == "degraded"
    assert result["errors"] == [f"conflicting Codex Session identity: {CONFLICT_ID}"]

    health = json.loads(
        (identity / "assistant" / "source-health" / "codex.json").read_text()
    )
    assert health["status"] == "degraded"
    assert health["errors"] == result["errors"]
    assert not (
        identity / "assistant" / "cursors" / "codex" / f"{CONFLICT_ID}.json"
    ).exists()

    # Polling the two compatible locations again cannot regress the cursor.
    assert _follow(root, active, archived) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["appended"] == 0
    assert repeated["duplicate"] == 0


def test_non_overlapping_rollout_shards_form_one_lossless_session(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)

    def timed_meta(timestamp: str) -> bytes:
        return _row(
            {
                "timestamp": timestamp,
                "type": "session_meta",
                "payload": {"id": SESSION_ID, "cwd": str(project)},
            }
        ) + b"\n"

    def timed_event(timestamp: str, value: str) -> bytes:
        return _row(
            {
                "timestamp": timestamp,
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": value},
            }
        ) + b"\n"

    first_path = active / "rollout-first.jsonl"
    second_path = active / "rollout-second.jsonl"
    first_bytes = timed_meta("2026-08-30T00:00:00Z") + timed_event(
        "2026-08-30T00:01:00Z", "first shard"
    )
    second_bytes = timed_meta("2026-08-30T00:02:00Z") + timed_event(
        "2026-08-30T00:03:00Z", "second shard"
    )
    first_path.write_bytes(first_bytes)
    second_path.write_bytes(second_bytes)

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    assert _follow(root, active, archived) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["errors"] == []
    assert first["discovered"] == 1
    assert first["eligible"] == 1
    assert first["appended"] == 4

    cursor_path = (
        identity / "assistant" / "cursors" / "codex" / f"{SESSION_ID}.json"
    )
    cursor = json.loads(cursor_path.read_text())
    assert cursor["relative_path"] == second_path.name
    assert cursor["byte_offset"] == len(second_bytes)
    assert cursor["line"] == 2

    state_path = (
        identity / "assistant" / "analysis" / "codex" / f"{SESSION_ID}.json"
    )
    state = json.loads(state_path.read_text())
    assert state["source_revision_digest"] == hashlib.sha256(
        first_bytes + second_bytes
    ).hexdigest()

    appended = timed_event("2026-08-30T00:04:00Z", "later append")
    with second_path.open("ab") as stream:
        stream.write(appended)
    assert _follow(root, active, archived) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["errors"] == []
    assert repeated["appended"] == 1
    assert repeated["duplicate"] == 0

    state = json.loads(state_path.read_text())
    assert state["source_revision_digest"] == hashlib.sha256(
        first_bytes + second_bytes + appended
    ).hexdigest()


def test_overlapping_rollout_shards_remain_a_conflict(tmp_path: Path, capsys):
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)

    def row(timestamp: str, *, meta: bool, value: str = "") -> bytes:
        payload = (
            {"id": CONFLICT_ID, "cwd": str(project)}
            if meta
            else {"type": "agent_message", "message": value}
        )
        return _row(
            {
                "timestamp": timestamp,
                "type": "session_meta" if meta else "event_msg",
                "payload": payload,
            }
        ) + b"\n"

    (active / "overlap-first.jsonl").write_bytes(
        row("2026-08-30T00:00:00Z", meta=True)
        + row("2026-08-30T00:03:00Z", meta=False, value="first")
    )
    (active / "overlap-second.jsonl").write_bytes(
        row("2026-08-30T00:02:00Z", meta=True)
        + row("2026-08-30T00:04:00Z", meta=False, value="second")
    )

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    assert _follow(root, active, archived) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["errors"] == [
        f"conflicting Codex Session identity: {CONFLICT_ID}"
    ]
    assert result["status"] == "degraded"


def test_restart_after_ledger_append_before_cursor_save_replays_idempotently(
    tmp_path: Path, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    (active / "session.jsonl").write_bytes(_meta(SESSION_ID, project) + b"\n")

    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()

    # A read-only cursor directory makes the durable save fail only after the
    # ledger append has succeeded.
    cursor_dir = identity / "assistant" / "cursors" / "codex"
    cursor_dir.mkdir(parents=True)
    cursor_dir.chmod(0o500)
    try:
        assert _follow(root, active, archived) == 2
        assert "cannot write durable assistant state" in capsys.readouterr().err
    finally:
        cursor_dir.chmod(0o700)

    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    source_events = [
        row
        for row in map(json.loads, ledger.read_text().splitlines())
        if row["type"] == "activity-source-event"
    ]
    assert len(source_events) == 1

    assert _follow(root, active, archived) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["appended"] == 0
    assert recovered["duplicate"] == 1
    assert (
        identity / "assistant" / "cursors" / "codex" / f"{SESSION_ID}.json"
    ).is_file()
    assert len(
        [
            row
            for row in map(json.loads, ledger.read_text().splitlines())
            if row["type"] == "activity-source-event"
        ]
    ) == 1
