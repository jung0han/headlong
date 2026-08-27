"""Product fixtures for observed Memory Failures and lesser quality feedback."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from headlong_web.assistant import AssistantError, PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
MEMORY_EVENT_ID = "bbbbbbbb-2222-4222-8222-222222222222"
DOWNSTREAM_EVENT_ID = "cccccccc-3333-4333-8333-333333333333"
DOWNSTREAM_LOCATOR = {
    "kind": "activity_ledger_event",
    "event_id": "dddddddd-4444-4444-8444-444444444444",
}


def _assistant(tmp_path: Path) -> PersonalAssistant:
    root = tmp_path / "headlong"
    identity = root / ".identities" / "observer"
    trajectory = identity / "trajectories" / "aaaaaaaa-root"
    trajectory.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\nroot_trajectory={ROOT_TRAJ}\n"
    )
    memory = {
        "type": "native-memory-added",
        "step_id": MEMORY_EVENT_ID,
        "event_id": MEMORY_EVENT_ID,
        "source": "personal_assistant",
        "source_kind": "headlong_memory",
        "source_identity": "memory-1",
        "knowledge_scope": {"kind": "project", "project_id": "project-one"},
        "evidence_locators": [{"kind": "activity_ledger_event", "event_id": ROOT_TRAJ}],
        "content": "A learned project decision.",
    }
    downstream = {
        "type": "action",
        "step_id": DOWNSTREAM_EVENT_ID,
        "event_id": DOWNSTREAM_EVENT_ID,
        "source_kind": "assistant_observation",
        "evidence_locators": [DOWNSTREAM_LOCATOR],
        "content": "A concrete proposal produced from the stale memory.",
    }
    (trajectory / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ})
        + "\n"
        + json.dumps(memory)
        + "\n"
        + json.dumps(downstream)
        + "\n"
    )
    return PersonalAssistant(root, resolve_observer(root, "observer"))


@pytest.mark.parametrize(
    "classification",
    ["wrong_scope", "evidence_contradicting", "behavior_affecting"],
)
def test_public_operation_records_each_domain_memory_failure(
    tmp_path: Path, classification: str
) -> None:
    assistant = _assistant(tmp_path)

    reported = assistant.report_memory_issue(
        MEMORY_EVENT_ID,
        classification,
        f"Observed {classification.replace('_', ' ')} impact.",
        downstream_event_id=(
            DOWNSTREAM_EVENT_ID if classification == "behavior_affecting" else None
        ),
    )

    assert reported["record_kind"] == "memory_failure"
    assert reported["classification"] == classification
    assert reported["memory_event_id"] == MEMORY_EVENT_ID
    assert reported["knowledge_scope"] == {
        "kind": "project",
        "project_id": "project-one",
    }
    assert assistant.memory_failure_health()["by_classification"][classification] == 1
    if classification == "behavior_affecting":
        assert reported["downstream_event_id"] == DOWNSTREAM_EVENT_ID
        assert reported["downstream_event_type"] == "action"
        assert reported["downstream_evidence_locators"] == [DOWNSTREAM_LOCATOR]


def test_behavior_affecting_requires_reproducible_downstream_event(
    tmp_path: Path,
) -> None:
    assistant = _assistant(tmp_path)

    with pytest.raises(
        AssistantError, match="downstream proposal or action event is required"
    ):
        assistant.report_memory_issue(
            MEMORY_EVENT_ID,
            "behavior_affecting",
            "The memory changed downstream behavior.",
        )


@pytest.mark.parametrize("classification", ["duplicate", "wording_defect"])
def test_duplicate_and_wording_defect_remain_quality_observations(
    tmp_path: Path, classification: str
) -> None:
    assistant = _assistant(tmp_path)

    reported = assistant.report_memory_issue(
        MEMORY_EVENT_ID, classification, "This needs quality cleanup."
    )

    assert reported["record_kind"] == "quality_observation"
    assert assistant.memory_failures() == []
    assert len(assistant.memory_quality_observations()) == 1
    health = assistant.memory_failure_health()
    assert health["total"] == 0
    assert health["quality_observations"] == 1


def test_api_exposes_bounded_memory_failure_inspection(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path)
    client = TestClient(create_app(assistant.root))
    base = "/api/identities/.identities~observer/assistant/memory-failures"

    created = client.post(
        base,
        json={
            "memory_event_id": MEMORY_EVENT_ID,
            "classification": "behavior_affecting",
            "description": "The stale memory produced a downstream proposal.",
            "downstream_event_id": DOWNSTREAM_EVENT_ID,
        },
    )
    listed = client.get(base)
    health = client.get(f"{base}/health")

    assert created.status_code == 200
    assert created.json()["downstream_event_id"] == DOWNSTREAM_EVENT_ID
    assert created.json()["downstream_evidence_locators"] == [DOWNSTREAM_LOCATOR]
    assert len(listed.json()) == 1
    assert health.json()["total"] == 1
    assert "content" not in health.json()


def test_cli_reports_and_lists_memory_failures(tmp_path: Path, capsys) -> None:
    assistant = _assistant(tmp_path)
    common = ["--root", str(assistant.root), "--identity", "observer"]

    assert run(
        [
            *common,
            "memory-failure",
            "report",
            MEMORY_EVENT_ID,
            "--classification",
            "behavior_affecting",
            "--description",
            "A proposal acted on stale memory.",
            "--downstream-event-id",
            DOWNSTREAM_EVENT_ID,
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert run([*common, "memory-failure", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)

    assert created["classification"] == "behavior_affecting"
    assert created["downstream_event_id"] == DOWNSTREAM_EVENT_ID
    assert len(listed["memory_failures"]) == 1
    assert listed["memory_failures"][0]["downstream_evidence_locators"] == [
        DOWNSTREAM_LOCATOR
    ]
