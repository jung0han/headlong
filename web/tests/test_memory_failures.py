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
EARLY_ACTION_ID = "dddddddd-4444-4444-8444-444444444444"
WRONG_TRAJECTORY_ACTION_ID = "99999999-7777-4777-8777-777777777777"
PROPOSAL_EVENT_ID = "eeeeeeee-5555-4555-8555-555555555555"
PROPOSAL_LOCATOR = {
    "schema": "headlong.evidence-locator/v1",
    "kind": "codex_event",
    "source_identity": "ffffffff-6666-4666-8666-666666666666",
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
    early_action = {
        "type": "action",
        "step_id": EARLY_ACTION_ID,
        "source": "inner_monologue",
        "content": "An action recorded before the memory existed.",
    }
    downstream = {
        "type": "action",
        "step_id": DOWNSTREAM_EVENT_ID,
        "source": "inner_monologue",
        "content": "Act on the stale project decision.",
    }
    (trajectory / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ})
        + "\n"
        + json.dumps(early_action)
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
        downstream_step_id=(
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
        assert reported["downstream_event_id"] is None
        assert reported["downstream_event_type"] == "action"
        assert reported["downstream_step_id"] == DOWNSTREAM_EVENT_ID
        snapshot = reported["downstream_action_snapshot"]
        assert {key: snapshot[key] for key in ("type", "step_id", "source", "content")} == {
            "type": "action",
            "step_id": DOWNSTREAM_EVENT_ID,
            "source": "inner_monologue",
            "content": "Act on the stale project decision.",
        }
        assert len(snapshot["content_sha256"]) == 64
        locator = reported["downstream_evidence_locators"][0]
        assert locator["schema"] == "headlong.evidence-locator/v1"
        assert locator["kind"] == "trajectory_step"
        assert locator["source_identity"] == ".identities~observer"
        assert locator["trajectory_id"] == ROOT_TRAJ
        assert locator["step_id"] == DOWNSTREAM_EVENT_ID
        assert len(locator["sha256"]) == 64


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


def test_behavior_affecting_accepts_action_before_asynchronous_memory_capture(
    tmp_path: Path,
) -> None:
    assistant = _assistant(tmp_path)

    reported = assistant.report_memory_issue(
        MEMORY_EVENT_ID,
        "behavior_affecting",
        "The action exposed harm before asynchronous memory capture completed.",
        downstream_step_id=EARLY_ACTION_ID,
    )

    assert reported["downstream_step_id"] == EARLY_ACTION_ID
    assert reported["downstream_action_snapshot"]["content"] == (
        "An action recorded before the memory existed."
    )
    assert reported["downstream_evidence_locators"][0]["trajectory_id"] == ROOT_TRAJ


def test_behavior_affecting_rejects_action_outside_observer_root_trajectory(
    tmp_path: Path,
) -> None:
    assistant = _assistant(tmp_path)
    other = (
        assistant.identity.path
        / "trajectories"
        / "99999999-other"
        / "trajectory.jsonl"
    )
    other.parent.mkdir()
    other.write_text(
        json.dumps(
            {
                "type": "action",
                "step_id": WRONG_TRAJECTORY_ACTION_ID,
                "source": "inner_monologue",
                "content": "This action belongs to another trajectory.",
            }
        )
        + "\n"
    )

    with pytest.raises(AssistantError, match="not found"):
        assistant.report_memory_issue(
            MEMORY_EVENT_ID,
            "behavior_affecting",
            "A different trajectory cannot establish the effect.",
            downstream_step_id=WRONG_TRAJECTORY_ACTION_ID,
        )


def test_behavior_affecting_still_accepts_a_proposal_event_with_evidence(
    tmp_path: Path,
) -> None:
    assistant = _assistant(tmp_path)
    assistant._ledger.append(
        {
            "type": "work-improvement-proposal",
            "step_id": PROPOSAL_EVENT_ID,
            "event_id": PROPOSAL_EVENT_ID,
            "evidence_locators": [PROPOSAL_LOCATOR],
            "content": "A concrete downstream proposal.",
        }
    )

    reported = assistant.report_memory_issue(
        MEMORY_EVENT_ID,
        "behavior_affecting",
        "The stale memory changed a proposal.",
        downstream_event_id=PROPOSAL_EVENT_ID,
    )

    assert reported["downstream_event_id"] == PROPOSAL_EVENT_ID
    assert reported["downstream_event_type"] == "work-improvement-proposal"
    assert reported["downstream_evidence_locators"] == [PROPOSAL_LOCATOR]
    assert reported["downstream_step_id"] is None
    assert reported["downstream_action_snapshot"] is None


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
            "downstream_step_id": DOWNSTREAM_EVENT_ID,
        },
    )
    listed = client.get(base)
    health = client.get(f"{base}/health")

    assert created.status_code == 200
    assert created.json()["downstream_event_id"] is None
    assert created.json()["downstream_step_id"] == DOWNSTREAM_EVENT_ID
    assert created.json()["downstream_action_snapshot"]["content"] == (
        "Act on the stale project decision."
    )
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
            "--downstream-step-id",
            DOWNSTREAM_EVENT_ID,
        ]
    ) == 0
    created = json.loads(capsys.readouterr().out)
    assert run([*common, "memory-failure", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)

    assert created["classification"] == "behavior_affecting"
    assert created["downstream_event_id"] is None
    assert created["downstream_step_id"] == DOWNSTREAM_EVENT_ID
    assert len(listed["memory_failures"]) == 1
    assert listed["memory_failures"][0]["downstream_evidence_locators"][0][
        "kind"
    ] == "trajectory_step"
