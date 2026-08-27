"""DONGWOO-994 product tests for native HeadLong Memory recovery."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from headlong_web.assistant import AssistantError, PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"


def _identity(root: Path) -> Path:
    identity = root / ".identities" / "observer"
    trajectory = identity / "trajectories" / "aaaaaaaa-root"
    trajectory.mkdir(parents=True)
    (identity / "memories").mkdir()
    (identity / "info.txt").write_text(
        "name=observer\n"
        "created=2026-08-27T00:00:00Z\n"
        f"root_trajectory={ROOT_TRAJ}\n"
    )
    (trajectory / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"})
        + "\n"
    )
    return identity


def _mem(repo: Path, identity: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / "bin" / "mem"), *args],
        cwd=repo,
        env={"PATH": "/usr/bin:/bin", "MEM_DIR": str(identity / "memories")},
        text=True,
        capture_output=True,
        check=False,
    )


def _retrieve(
    repo: Path, identity: Path, tmp_path: Path, content: str
) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return subprocess.run(
        [str(repo / "thinkers" / "retrieval" / "step")],
        cwd=repo,
        env={
            "PATH": f"{repo / 'bin'}:/usr/bin:/bin",
            "HOME": str(home),
            "IDENTITY_DIR": str(identity),
            "IDENTITY_NAME": "observer",
            "MEM_DIR": str(identity / "memories"),
            "TRAJ_DIR": str(identity / "trajectories"),
            "TRAJ_ID": ROOT_TRAJ,
            "SKILLS_DIR": str(identity / "skills"),
            "SKILLS_KERNEL_DIR": str(identity / "kernel"),
        },
        input=json.dumps(
            {"type": "thought", "step_id": "trigger-1", "content": content}
        ),
        text=True,
        capture_output=True,
        check=False,
    )


def test_public_rebuild_replays_latest_edits_and_keeps_tombstones_absent(
    tmp_path: Path, capsys
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))

    kept = _mem(repo, identity, "add", "Keep the first form.")
    kept_id = kept.stdout.strip().split("_")[1]
    forgotten = _mem(repo, identity, "add", "Forget this value.")
    forgotten_id = forgotten.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 2

    assert (
        _mem(repo, identity, "edit", kept_id, "Keep the latest form.").returncode
        == 0
    )
    assert _mem(repo, identity, "forget", forgotten_id).returncode == 0
    assert service.capture_native_memory_mutations() == {
        "status": "ok",
        "added": 0,
        "edited": 1,
        "forgotten": 1,
    }

    for path in (identity / "memories").glob("*.md"):
        path.unlink()
    (identity / "memories" / "rogue.md").write_text("not ledger backed\n")

    args = [
        "--root",
        str(root),
        "--identity",
        "observer",
        "native-memory",
        "rebuild",
    ]
    assert run(args) == 0
    assert json.loads(capsys.readouterr().out) == {"active": 1, "tombstoned": 1}

    client = TestClient(create_app(root))
    rebuilt = client.post("/api/identities/.identities~observer/memories/rebuild")
    assert rebuilt.status_code == 200
    assert rebuilt.json() == {"active": 1, "tombstoned": 1}
    response = client.get("/api/identities/.identities~observer/memories")
    assert response.status_code == 200
    [memory] = response.json()
    assert memory["id"] == kept_id
    shown = _mem(repo, identity, "show", kept_id)
    assert shown.returncode == 0
    assert shown.stdout.endswith("Keep the latest form.\n")
    assert forgotten_id not in _mem(repo, identity, "list", "--short").stdout

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_llm = fake_bin / "llm"
    fake_llm.write_text(
        "#!/bin/sh\n"
        "input=$(cat)\n"
        "printf '%s' \"$input\" | grep -q 'Keep the latest form.' || exit 9\n"
        "printf 'matched rebuilt memory\\n'\n"
    )
    fake_llm.chmod(0o755)
    searched = subprocess.run(
        [str(repo / "bin" / "mem"), "search", "latest form"],
        cwd=repo,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "MEM_DIR": str(identity / "memories"),
        },
        text=True,
        capture_output=True,
        check=False,
    )
    assert searched.returncode == 0
    assert searched.stdout == "matched rebuilt memory\n"

    assert run(args) == 0
    assert json.loads(capsys.readouterr().out) == {"active": 1, "tombstoned": 1}


def test_forgotten_memory_can_be_restored_idempotently_and_edited(
    tmp_path: Path, capsys
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))

    added = _mem(
        repo,
        identity,
        "add",
        "--type",
        "lesson",
        "Restored native memories remain editable.",
    )
    memory_id = added.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 1
    assert _mem(repo, identity, "forget", memory_id).returncode == 0
    assert service.capture_native_memory_mutations()["forgotten"] == 1

    args = [
        "--root",
        str(root),
        "--identity",
        "observer",
        "native-memory",
        "restore",
        memory_id,
    ]
    assert run(args) == 0
    expected = {"memory_id": memory_id, "status": "active"}
    assert json.loads(capsys.readouterr().out) == expected

    restarted = PersonalAssistant(root, resolve_observer(root, "observer"))
    assert restarted.restore_native_memory(memory_id) == expected
    restored = _mem(repo, identity, "show", memory_id)
    assert restored.returncode == 0
    assert restored.stdout.endswith("Restored native memories remain editable.\n")

    edited = _mem(repo, identity, "edit", memory_id, "The restored value was edited.")
    assert edited.returncode == 0
    assert restarted.capture_native_memory_mutations()["edited"] == 1
    assert restarted.rebuild_native_memory() == {"active": 1, "tombstoned": 0}
    assert _mem(repo, identity, "show", memory_id).stdout.endswith(
        "The restored value was edited.\n"
    )

    response = TestClient(create_app(root)).post(
        f"/api/identities/.identities~observer/memories/{memory_id}/restore"
    )
    assert response.status_code == 200
    assert response.json() == expected


def test_delete_and_restore_refresh_native_retrieval(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))

    added = _mem(
        repo,
        identity,
        "add",
        "Colima docker bind mounts need shared paths.",
    )
    memory_id = added.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 1
    retrieval_dir = identity / "retrieval"
    retrieval_dir.mkdir()
    index = retrieval_dir / "index.tsv"
    seen = retrieval_dir / "seen"
    index.write_text(f"colima\t{memory_id}\tstale entry\n")
    seen.write_text(f"{memory_id}\n")

    assert _mem(repo, identity, "forget", memory_id).returncode == 0
    assert service.capture_native_memory_mutations()["forgotten"] == 1
    assert not index.exists()
    assert not seen.exists()

    index.write_text(f"wrong\t{memory_id}\tstale entry\n")
    seen.write_text(f"{memory_id}\n")
    assert service.restore_native_memory(memory_id)["status"] == "active"
    assert not index.exists()
    assert not seen.exists()

    retrieved = _retrieve(
        repo, identity, tmp_path, "Why do colima docker bind mounts fail?"
    )
    assert retrieved.returncode == 0, retrieved.stderr
    assert f"\t{memory_id}\t" in index.read_text()
    trajectory = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    event = json.loads(trajectory.read_text().splitlines()[-1])
    assert event["source"] == "retrieval"
    assert event["retrieved_mem"] == memory_id


def test_corrupt_ledger_fails_observably_without_changing_current_store(
    tmp_path: Path, capsys
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    added = _mem(repo, identity, "add", "Keep the current store intact.")
    assert added.returncode == 0
    assert service.capture_native_memory_mutations()["added"] == 1
    before = {
        path.name: path.read_bytes() for path in (identity / "memories").glob("*.md")
    }

    ledger = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    with ledger.open("a") as handle:
        handle.write('{"type":"native-memory-edited","source_kind":"headlong_memory"\n')

    result = run(
        [
            "--root",
            str(root),
            "--identity",
            "observer",
            "native-memory",
            "rebuild",
        ]
    )
    captured = capsys.readouterr()
    assert result == 2
    assert "Activity Ledger is corrupt" in captured.err
    after = {
        path.name: path.read_bytes() for path in (identity / "memories").glob("*.md")
    }
    assert after == before


def test_incomplete_mutation_chain_does_not_replace_current_store(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    added = _mem(repo, identity, "add", "Retain the last complete ledger value.")
    memory_id = added.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 1
    memory_path = next((identity / "memories").glob("*.md"))
    before = memory_path.read_bytes()

    ledger = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    events = [json.loads(line) for line in ledger.read_text().splitlines()]
    added_event = events[-1]
    incomplete_id = "bbbbbbbb-2222-4222-8222-222222222222"
    incomplete = {
        "type": "native-memory-edited",
        "step_id": incomplete_id,
        "event_id": incomplete_id,
        "source_kind": "headlong_memory",
        "source_identity": memory_id,
        "mutation_schema": "headlong.native-memory-mutation/v1",
        "memory_id": memory_id,
        "supersedes_event_ids": [added_event["event_id"]],
        "prior_value": None,
        "replacement_value": added_event["replacement_value"],
        "ts": "9999-12-31T23:59:59Z",
    }
    with ledger.open("a") as handle:
        handle.write(json.dumps(incomplete) + "\n")

    with pytest.raises(AssistantError, match="edit history is incomplete"):
        service.rebuild_native_memory()
    assert memory_path.read_bytes() == before


def test_empty_recovery_history_does_not_replace_a_valid_current_store(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    added = _mem(repo, identity, "add", "A valid memory must survive missing history.")
    assert added.returncode == 0
    assert service.capture_native_memory_mutations()["added"] == 1
    memory_path = next((identity / "memories").glob("*.md"))
    before = memory_path.read_bytes()

    ledger = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    ledger.write_text(json.dumps(rows[0]) + "\n")

    with pytest.raises(AssistantError, match="history is incomplete"):
        service.rebuild_native_memory()

    assert memory_path.read_bytes() == before


def test_partial_recovery_history_does_not_drop_an_unaccounted_memory(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    assert _mem(repo, identity, "add", "First retained memory.").returncode == 0
    assert _mem(repo, identity, "add", "Second retained memory.").returncode == 0
    assert service.capture_native_memory_mutations()["added"] == 2
    before = {
        path.name: path.read_bytes() for path in (identity / "memories").glob("*.md")
    }

    ledger = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    memory_rows = [row for row in rows if row.get("source_kind") == "headlong_memory"]
    removed_id = memory_rows[-1]["memory_id"]
    ledger.write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in rows
            if row.get("memory_id") != removed_id
        )
    )

    with pytest.raises(AssistantError, match="history is incomplete"):
        service.rebuild_native_memory()

    after = {
        path.name: path.read_bytes() for path in (identity / "memories").glob("*.md")
    }
    assert after == before


def test_recovery_history_cannot_overwrite_an_uncaptured_live_edit(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    added = _mem(repo, identity, "add", "Original complete value.")
    memory_id = added.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 1
    memory_path = next((identity / "memories").glob("*.md"))
    changed = memory_path.read_text().replace(
        "Original complete value.", "Uncaptured but valid live edit."
    )
    memory_path.write_text(changed)

    with pytest.raises(AssistantError, match="history is incomplete"):
        service.rebuild_native_memory()

    assert memory_id in memory_path.name
    assert "Uncaptured but valid live edit." in memory_path.read_text()


def test_restore_remains_reversible_across_repeated_forget_cycles(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    service = PersonalAssistant(root, resolve_observer(root, "observer"))
    added = _mem(repo, identity, "add", "Recovery can be reversed more than once.")
    memory_id = added.stdout.strip().split("_")[1]
    assert service.capture_native_memory_mutations()["added"] == 1

    assert _mem(repo, identity, "forget", memory_id).returncode == 0
    assert service.capture_native_memory_mutations()["forgotten"] == 1
    assert service.restore_native_memory(memory_id)["status"] == "active"

    assert _mem(repo, identity, "forget", memory_id).returncode == 0
    assert service.capture_native_memory_mutations()["forgotten"] == 1
    assert service.rebuild_native_memory() == {"active": 0, "tombstoned": 1}
    assert service.restore_native_memory(memory_id)["status"] == "active"
    assert _mem(repo, identity, "show", memory_id).returncode == 0

    with pytest.raises(AssistantError, match="native memory not found: missing-id"):
        service.restore_native_memory("missing-id")
