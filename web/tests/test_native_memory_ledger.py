"""DONGWOO-989 product tests for native HeadLong Memory auditing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from headlong_web.assistant import AssistantError, PersonalAssistant, resolve_observer
from headlong_web.assistant_services import ActivityLedger


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
        '{"type":"trajectory","step_id":"'
        f'{ROOT_TRAJ}","ts":"t0"}}\n'
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


def _memory_events(root: Path) -> list[dict]:
    identity = resolve_observer(root, "observer")
    return [
        event
        for event in ActivityLedger(root, identity).events()
        if event.get("type", "").startswith("native-memory-")
    ]


def test_mem_add_is_recorded_with_stable_identity_and_available_value(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)

    added = _mem(repo, identity, "add", "--type", "fact", "Use red-green slices.")
    assert added.returncode == 0
    memory_name = added.stdout.strip()

    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    assert assistant.capture_native_memory_mutations() == {
        "status": "ok",
        "added": 1,
        "edited": 0,
        "forgotten": 0,
    }

    [event] = _memory_events(root)
    memory_id = memory_name.split("_")[1]
    assert event["type"] == "native-memory-added"
    assert event["source_identity"] == memory_id
    assert event["knowledge_scope"] == {"kind": "global"}
    assert event["evidence_locators"] == []
    assert event["content"] == "Use red-green slices."
    assert event["memory_id"] == memory_id
    assert event["memory_type"] == "fact"
    assert event["replacement_value"] == {
        "content": "Use red-green slices.",
        "evidence_locators": [],
        "filename": f"{memory_name}.md",
        "knowledge_scope": {"kind": "global"},
        "memory_id": memory_id,
        "memory_type": "fact",
    }
    assert event["prior_value"] is None


def test_mem_edit_records_prior_and_replacement_across_filename_change(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))

    added = _mem(repo, identity, "add", "--type", "preference", "Prefer tabs.")
    memory_name = added.stdout.strip()
    memory_id = memory_name.split("_")[1]
    assert added.returncode == 0
    assert assistant.capture_native_memory_mutations()["added"] == 1

    edited = _mem(
        repo,
        identity,
        "edit",
        memory_id,
        "--slug",
        "spaces-now",
        "Prefer spaces.",
    )
    assert edited.returncode == 0
    assert edited.stdout == ""
    assert edited.stderr.startswith("Updated: ")
    assert assistant.capture_native_memory_mutations() == {
        "status": "ok",
        "added": 0,
        "edited": 1,
        "forgotten": 0,
    }

    added_event, edited_event = _memory_events(root)
    assert edited_event["type"] == "native-memory-edited"
    assert edited_event["source_identity"] == memory_id
    assert edited_event["supersedes_event_ids"] == [added_event["event_id"]]
    assert edited_event["prior_value"] == added_event["replacement_value"]
    assert edited_event["replacement_value"]["memory_id"] == memory_id
    assert edited_event["replacement_value"]["filename"].endswith("_spaces-now.md")
    assert edited_event["replacement_value"]["content"] == "Prefer spaces."


def test_mem_forget_records_tombstone_with_prior_value(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))

    added = _mem(repo, identity, "add", "--type", "value", "Keep evidence.")
    memory_id = added.stdout.strip().split("_")[1]
    assert added.returncode == 0
    assert assistant.capture_native_memory_mutations()["added"] == 1

    forgotten = _mem(repo, identity, "forget", memory_id)
    assert forgotten.returncode == 0
    assert forgotten.stdout == ""
    assert forgotten.stderr.startswith("Forgotten: ")
    assert assistant.capture_native_memory_mutations() == {
        "status": "ok",
        "added": 0,
        "edited": 0,
        "forgotten": 1,
    }

    added_event, tombstone = _memory_events(root)
    assert tombstone["type"] == "native-memory-forgotten"
    assert tombstone["source_identity"] == memory_id
    assert tombstone["authority"] == "superseded"
    assert tombstone["supersedes_event_ids"] == [added_event["event_id"]]
    assert tombstone["content"] == "Keep evidence."
    assert tombstone["prior_value"] == added_event["replacement_value"]
    assert tombstone["replacement_value"] is None


def test_direct_file_add_edit_rename_and_delete_are_observed(tmp_path: Path) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    memory_id = "native-lesson-1"
    locator = {
        "kind": "activity_ledger_event",
        "event_id": "bbbbbbbb-2222-4222-8222-222222222222",
    }
    original = identity / "memories" / "actor-draft.md"
    original.write_text(
        "---\n"
        f"id: {memory_id}\n"
        "type: lesson\n"
        "knowledge_scope: project:project-native\n"
        f"evidence_locators: [{json.dumps(locator)}]\n"
        "---\n\n"
        "Retain audit evidence.\n"
    )

    assert assistant.capture_native_memory_mutations()["added"] == 1
    [added] = _memory_events(root)
    assert added["knowledge_scope"] == {
        "kind": "project",
        "project_id": "project-native",
    }
    assert added["evidence_locators"] == [locator]

    renamed = identity / "memories" / "actor-final.md"
    original.rename(renamed)
    renamed.write_text(
        "---\n"
        f"id: {memory_id}\n"
        "type: lesson\n"
        "knowledge_scope: project:project-native\n"
        f"evidence_locators: [{json.dumps(locator)}]\n"
        "---\n\n"
        "Retain prior values and audit evidence.\n"
    )
    assert assistant.capture_native_memory_mutations()["edited"] == 1
    added, edited = _memory_events(root)
    assert edited["source_identity"] == added["source_identity"] == memory_id
    assert edited["prior_value"]["filename"] == "actor-draft.md"
    assert edited["replacement_value"]["filename"] == "actor-final.md"

    renamed.unlink()
    assert assistant.capture_native_memory_mutations()["forgotten"] == 1
    added, edited, forgotten = _memory_events(root)
    assert forgotten["prior_value"] == edited["replacement_value"]
    assert forgotten["supersedes_event_ids"] == [edited["event_id"]]


def test_filename_only_rename_keeps_frontmatter_memory_identity(tmp_path: Path) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    original = identity / "memories" / "old-slug.md"
    original.write_text("---\nid: stable-rename-1\ntype: note\n---\n\nSame body.\n")
    assert assistant.capture_native_memory_mutations()["added"] == 1

    original.rename(identity / "memories" / "new-slug.md")
    assert assistant.capture_native_memory_mutations()["edited"] == 1

    added, renamed = _memory_events(root)
    assert [event["type"] for event in (added, renamed)] == [
        "native-memory-added",
        "native-memory-edited",
    ]
    assert renamed["memory_id"] == "stable-rename-1"
    assert renamed["prior_value"]["content"] == "Same body."
    assert renamed["replacement_value"]["content"] == "Same body."


def test_in_progress_direct_write_does_not_look_like_forgetting(tmp_path: Path) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    memory = identity / "memories" / "direct.md"
    memory.write_text("---\nid: stable-write-1\ntype: fact\n---\n\nComplete.\n")
    assert assistant.capture_native_memory_mutations()["added"] == 1

    memory.write_text("---\nid: stable-write-1\ntype: fact\n")
    assert assistant.capture_native_memory_mutations() == {
        "status": "ok",
        "added": 0,
        "edited": 0,
        "forgotten": 0,
    }
    assert len(_memory_events(root)) == 1


def test_actor_memory_symlink_cannot_make_observer_read_protected_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    protected = root / ".assistant-authority" / "secret.md"
    protected.parent.mkdir()
    protected.write_text(
        "---\nid: protected-secret-1\ntype: fact\n---\n\nNever expose this.\n"
    )
    (identity / "memories" / "actor-link.md").symlink_to(protected)
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))

    with pytest.raises(AssistantError, match="symlink"):
        assistant.capture_native_memory_mutations()
    assert _memory_events(root) == []


def test_codex_observer_cycle_and_restart_do_not_duplicate_memory_events(
    tmp_path: Path,
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    (identity / "memories" / "native.md").write_text(
        "---\nid: durable-native-1\ntype: fact\n---\n\nRestart safely.\n"
    )
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)

    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    first = assistant.process_codex_once(active, archived)
    assert first["memory"] == {
        "status": "ok",
        "added": 1,
        "edited": 0,
        "forgotten": 0,
    }

    restarted = PersonalAssistant(root, resolve_observer(root, "observer"))
    second = restarted.process_codex_once(active, archived)
    assert second["memory"] == {
        "status": "ok",
        "added": 0,
        "edited": 0,
        "forgotten": 0,
    }
    assert len(_memory_events(root)) == 1
