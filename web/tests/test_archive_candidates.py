"""DONGWOO-991 product tests for reviewable Codex Archive Candidates."""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from headlong_web import archive_candidates, assistant_services, codex_analysis
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app


LOCATOR_TOKEN = "locator-1"
LOCATOR = {
    "schema": "headlong.evidence-locator/v1",
    "kind": "codex_event",
    "source_identity": "bbbbbbbb-2222-4222-8222-222222222222",
    "source_root": "archived",
    "relative_path": "session.jsonl",
    "line": 2,
    "byte_offset": 100,
    "byte_length": 20,
    "sha256": "a" * 64,
    "host": "test-host",
}
ANALYSIS_ID = "cccccccc-3333-4333-8333-333333333333"
PROJECT_ID = "project-test"
ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"


def _result(claim: str = "completed") -> dict:
    return {
        "title": "Work appears complete",
        "observation": "The requested change and verification are complete.",
        "evidence_locators": [LOCATOR_TOKEN],
        "memory_candidates": [],
        "improvement_signals": [],
        "archive_candidates": [
            {
                "completion_state": claim,
                "rationale": "The final response reports the tested change complete.",
                "evidence_locators": [LOCATOR_TOKEN],
            }
        ],
    }


def test_analysis_schema_accepts_only_evidence_backed_completed_archive_claims():
    schema = codex_analysis.result_schema({LOCATOR_TOKEN: LOCATOR})

    validated = schema.validate(_result())

    assert validated["archive_candidates"] == [
        {
            "completion_state": "completed",
            "rationale": "The final response reports the tested change complete.",
            "evidence_locators": [LOCATOR],
        }
    ]
    with pytest.raises(codex_analysis.AnalysisContractError):
        schema.validate(_result("probably_done"))
    unsupported = _result()
    unsupported["archive_candidates"][0]["evidence_locators"] = ["unknown"]
    with pytest.raises(codex_analysis.AnalysisContractError):
        schema.validate(unsupported)


def _analysis(claim: str = "completed") -> dict:
    return {
        "type": "observation",
        "event_id": ANALYSIS_ID,
        "source": "personal_assistant",
        "source_kind": "codex_session",
        "source_identity": LOCATOR["source_identity"],
        "knowledge_scope": {"kind": "project", "project_id": PROJECT_ID},
        "analysis_state": "final",
        "archive_candidates": [
            {
                "completion_state": claim,
                "rationale": "Tests pass and the final response reports completion.",
                "evidence_locators": [LOCATOR],
            }
        ],
    }


def test_archive_candidate_projection_is_stable_pending_and_ledger_rebuildable():
    first = archive_candidates.candidate_events(_analysis())
    repeated = archive_candidates.candidate_events(_analysis())

    assert repeated == first
    assert len(first) == 1
    candidate = archive_candidates.build_inbox(first)[0]
    assert candidate["candidate_id"] == first[0]["event_id"]
    assert candidate["session_id"] == LOCATOR["source_identity"]
    assert candidate["project_id"] == PROJECT_ID
    assert candidate["completion_rationale"].startswith("Tests pass")
    assert candidate["analysis_state"] == "final"
    assert candidate["review_state"] == "pending"
    assert candidate["archive_authority"] == "none"
    assert candidate["execution_authority"] == "none"
    assert candidate["evidence_locators"] == [LOCATOR]

    review = archive_candidates.review_event(
        candidate,
        "accepted",
        event_id="dddddddd-4444-4444-8444-444444444444",
    )
    rebuilt = archive_candidates.build_inbox([*first, review])[0]
    assert rebuilt["review_state"] == "accepted"
    assert rebuilt["archive_authority"] == "authorized"
    assert rebuilt["execution_authority"] == "none"


def test_unsupported_or_unrelated_completion_claims_do_not_become_candidates():
    assert archive_candidates.candidate_events(_analysis("probably_done")) == []
    unrelated = _analysis()
    unrelated["analysis_state"] = "failed"
    assert archive_candidates.candidate_events(unrelated) == []


def test_provisional_and_final_claims_keep_distinct_stable_candidate_identity():
    provisional = _analysis()
    provisional["analysis_state"] = "provisional"
    provisional["event_id"] = "eeeeeeee-5555-4555-8555-555555555555"

    provisional_id = archive_candidates.candidate_events(provisional)[0]["event_id"]
    final_id = archive_candidates.candidate_events(_analysis())[0]["event_id"]

    assert provisional_id != final_id


class FakeLiteLLM:
    def __init__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                prompt = body["messages"][-1]["content"]
                locator = next(
                    line.removeprefix("EVIDENCE_LOCATOR ")
                    for line in prompt.splitlines()
                    if line.startswith("EVIDENCE_LOCATOR ")
                )
                result = {
                    "title": "Session work completed",
                    "observation": "The requested work and verification completed.",
                    "evidence_locators": [locator],
                    "memory_candidates": [],
                    "improvement_signals": [],
                    "archive_candidates": [
                        {
                            "completion_state": "completed",
                            "rationale": "The implementation and focused tests completed.",
                            "evidence_locators": [locator],
                        },
                        {
                            "completion_state": "completed",
                            "rationale": "The final response confirms the requested handoff.",
                            "evidence_locators": [locator],
                        },
                    ],
                }
                owner.calls += 1
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": json.dumps(result)},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.calls = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1/chat/completions"


def _row(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


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


def test_actor_forged_archive_authority_never_enters_review_projection(tmp_path: Path):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    trajectory = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    with trajectory.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(archive_candidates.candidate_events(_analysis())[0]) + "\n")

    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))

    assert assistant.archive_candidates() == []


def _configure_model(monkeypatch, model: FakeLiteLLM, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


def test_public_analysis_dashboard_and_cli_review_are_idempotent_and_non_executing(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    canary = project / "work.txt"
    canary.write_text("must stay unchanged\n")
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    session = archived / "session.jsonl"
    original_session = _row(
        {"type": "session_meta", "payload": {"id": LOCATOR["source_identity"], "cwd": str(project)}}
    ) + _row({"type": "event_msg", "payload": {"type": "task_complete"}})
    session.write_bytes(original_session)
    subprocess_calls: list[object] = []
    real_run = subprocess.run

    def record_subprocess(*args, **kwargs):
        subprocess_calls.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr(assistant_services.subprocess, "run", record_subprocess)

    assistant = PersonalAssistant(
        root,
        resolve_observer(root, "observer"),
        clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    assistant.add_project(project)
    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        result = assistant.process_codex_once(active, archived)
        replay = assistant.analyze_codex_once(active, archived)

    assert result["analysis"]["final"] == 1
    assert result["analysis"]["archive_candidates_created"] == 2
    assert replay["archive_candidates_created"] == 0
    assert model.calls == 1
    candidates = assistant.archive_candidates()
    assert len(candidates) == 2
    assert {item["review_state"] for item in candidates} == {"pending"}
    assert {item["session_id"] for item in candidates} == {LOCATOR["source_identity"]}
    assert {item["project_id"] for item in candidates} == {assistant.projects()[0].id}

    journal = next((root / ".assistant-authority").glob("*/events.jsonl"))
    before_review = len(journal.read_text().splitlines())
    first_id, second_id = [item["candidate_id"] for item in candidates]
    rejected = assistant.review_archive_candidates([first_id], "rejected")
    assert rejected["archive_candidates"][0]["review_state"] == "rejected"
    after_reject = len(journal.read_text().splitlines())
    assistant.review_archive_candidates([first_id], "rejected")
    assert len(journal.read_text().splitlines()) == after_reject

    assert run(
        [
            "--root",
            str(root),
            "--identity",
            "observer",
            "archive-candidate",
            "list",
        ]
    ) == 0
    listed = json.loads(capsys.readouterr().out)
    assert len(listed["archive_candidates"]) == 2
    assert run(
        [
            "--root",
            str(root),
            "--identity",
            "observer",
            "archive-candidate",
            "show",
            first_id,
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["candidate_id"] == first_id
    assert run(
        [
            "--root",
            str(root),
            "--identity",
            "observer",
            "archive-candidate",
            "review",
            first_id,
            second_id,
            "--state",
            "accepted",
        ]
    ) == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert {item["review_state"] for item in reviewed["archive_candidates"]} == {
        "accepted"
    }

    client = TestClient(create_app(root))
    base = "/api/identities/.identities~observer/archive-candidates"
    response = client.get(base)
    assert response.status_code == 200
    assert len(response.json()) == 2
    detail = client.get(f"{base}/{first_id}")
    assert detail.status_code == 200
    assert detail.json()["evidence_locators"]
    evidence = client.get(f"{base}/{first_id}/evidence/0")
    assert evidence.status_code == 200
    assert evidence.json()["raw"].encode() in original_session.splitlines(keepends=True)
    individual = client.post(
        f"{base}/{first_id}/review", json={"state": "dismissed"}
    )
    assert individual.status_code == 200
    assert individual.json()["review_state"] == "dismissed"
    api_batch = client.post(
        f"{base}/review",
        json={"candidate_ids": [first_id, second_id], "state": "accepted"},
    )
    assert api_batch.status_code == 200
    after_batch = len(journal.read_text().splitlines())
    assert after_batch == before_review + 5
    api_replay = client.post(
        f"{base}/review",
        json={"candidate_ids": [first_id, second_id], "state": "accepted"},
    )
    assert api_replay.status_code == 200
    assert len(journal.read_text().splitlines()) == after_batch

    read_only = TestClient(create_app(root, read_only=True))
    blocked = read_only.post(
        f"{base}/{first_id}/review", json={"state": "accepted"}
    )
    assert blocked.status_code == 403

    assert session.read_bytes() == original_session
    assert canary.read_text() == "must stay unchanged\n"
    assert sorted(path.name for path in project.iterdir()) == ["work.txt"]
    assert not any(
        isinstance(command, (list, tuple))
        and command
        and Path(str(command[0])).name == "codex"
        for command in subprocess_calls
    )
