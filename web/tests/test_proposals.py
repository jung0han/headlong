"""DONGWOO-911 product tests for the proposal-only review boundary."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web.assistant import EvidenceLocator, PersonalAssistant, resolve_observer
from headlong_web.proposals import work_proposal_events
from headlong_web.server import create_app

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
SUPPORTED = {
    "user_correction",
    "test_failure",
    "tool_failure",
    "reviewer_finding",
}


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
                signals = [
                    {
                        "kind": kind,
                        "content": f"Improve the work after this {kind}.",
                        "evidence_locators": [locator],
                    }
                    for kind in sorted(SUPPORTED)
                ]
                signals.extend(
                    [
                        {
                            "kind": "inferred_pattern",
                            "content": "An agent suspects a recurring pattern.",
                            "evidence_locators": [locator],
                        },
                        {
                            "kind": "open_loop",
                            "content": "An agent suggested following up later.",
                            "evidence_locators": [locator],
                        },
                    ]
                )
                result = {
                    "title": "Direct evidence found",
                    "observation": "The session contains reviewable direct evidence.",
                    "evidence_locators": [locator],
                    "memory_candidates": [],
                    "improvement_signals": signals,
                }
                payload = json.dumps(
                    {
                        "choices": [
                            {
                                "message": {"content": json.dumps(result)},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                    }
                ).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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


def _events(identity: Path) -> list[dict]:
    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def _configure_model(monkeypatch, model: FakeLiteLLM, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)


def test_direct_evidence_becomes_reviewable_without_external_mutation(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity_path = _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    canary = project / "work.txt"
    canary.write_text("must stay unchanged\n")
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    raw_meta = _row(
        {"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": str(project)}}
    )
    raw_failure = _row(
        {"type": "event_msg", "payload": {"type": "task_complete"}}
    )
    (archived / "session.jsonl").write_bytes(raw_meta + raw_failure)

    assistant = PersonalAssistant(
        root,
        resolve_observer(root, "observer"),
        clock=lambda: datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    assistant.add_project(project)
    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        result = assistant.process_codex_once(active, archived)

    assert result["analysis"]["final"] == 1
    assert result["analysis"]["work_proposals_created"] == 4
    inbox = assistant.proposals()
    assert len(inbox) == 4
    assert {item["evidence_kind"] for item in inbox} == SUPPORTED
    assert {item["review_state"] for item in inbox} == {"pending"}
    assert {item["knowledge_scope"]["project_id"] for item in inbox} == {
        assistant.projects()[0].id
    }
    assert {item["execution_authority"] for item in inbox} == {"none"}
    assert not any(
        event.get("evidence_kind") in {"inferred_pattern", "open_loop", "agent_suggestion"}
        for event in _events(identity_path)
        if event.get("proposal_schema")
    )

    locator = EvidenceLocator.decode(inbox[0]["evidence_locators"][0])
    resolved = assistant.resolve_evidence(locator, active, archived)
    assert resolved in {raw_meta, raw_failure}

    client = TestClient(create_app(root))
    identity_id = ".identities~observer"
    listed = client.get(f"/api/identities/{identity_id}/proposals")
    assert listed.status_code == 200
    assert len(listed.json()) == 4
    proposal_id = inbox[0]["proposal_id"]
    detail = client.get(f"/api/identities/{identity_id}/proposals/{proposal_id}")
    assert detail.status_code == 200
    assert detail.json()["evidence_locators"]

    for state in ("accepted", "rejected", "dismissed", "pending"):
        reviewed = client.post(
            f"/api/identities/{identity_id}/proposals/{proposal_id}/review",
            json={"state": state},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["review_state"] == state
        assert reviewed.json()["execution_authority"] == "none"

    rebuilt = PersonalAssistant(root, resolve_observer(root, "observer")).proposal(
        proposal_id
    )
    assert rebuilt is not None
    assert rebuilt["review_state"] == "pending"
    reviews = [
        event for event in _events(identity_path) if event.get("type") == "proposal-review"
    ]
    assert len(reviews) == 4
    assert all(event["execution_authority"] == "none" for event in reviews)
    assert canary.read_text() == "must stay unchanged\n"
    assert sorted(path.name for path in project.iterdir()) == ["work.txt"]


def test_review_public_boundary_validates_state_and_read_only_mode(tmp_path: Path):
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    client = TestClient(create_app(root))
    invalid = client.post(
        "/api/identities/.identities~observer/proposals/not-a-uuid/review",
        json={"state": "executed"},
    )
    assert invalid.status_code == 422

    read_only = TestClient(create_app(root, read_only=True))
    blocked = read_only.post(
        "/api/identities/.identities~observer/proposals/not-a-uuid/review",
        json={"state": "accepted"},
    )
    assert blocked.status_code == 403


def test_agent_suggestion_is_not_direct_evidence():
    locator = {
        "schema": "headlong.evidence-locator/v1",
        "kind": "codex_event",
        "source_identity": SESSION_ID,
        "source_root": "active",
        "relative_path": "session.jsonl",
        "line": 1,
        "byte_offset": 0,
        "byte_length": 2,
        "sha256": "a" * 64,
        "host": "test-host",
    }
    analysis = {
        "type": "observation",
        "source": "personal_assistant",
        "analysis_state": "final",
        "event_id": "cccccccc-3333-4333-8333-333333333333",
        "source_identity": SESSION_ID,
        "knowledge_scope": {"kind": "project", "project_id": "project-test"},
        "improvement_signals": [
            {
                "kind": "agent_suggestion",
                "content": "The agent thinks its own idea deserves implementation.",
                "evidence_locators": [locator],
            }
        ],
    }
    assert work_proposal_events(analysis) == []
