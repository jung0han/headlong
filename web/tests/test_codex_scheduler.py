"""DONGWOO-992 product tests for responsive, durable Codex scheduling."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web import assistant_runtime
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class FakeLiteLLM:
    def __init__(self):
        self.calls: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                prompt = body["messages"][-1]["content"]
                locators = [
                    line.removeprefix("EVIDENCE_LOCATOR ")
                    for line in prompt.splitlines()
                    if line.startswith("EVIDENCE_LOCATOR ")
                ]
                session_id = next(
                    line.removeprefix("Codex Session: ")
                    for line in prompt.splitlines()
                    if line.startswith("Codex Session: ")
                )
                content = json.dumps(
                    {
                        "title": f"Observed {session_id}",
                        "observation": f"Current memory input for {session_id}.",
                        "evidence_locators": [locators[-1]],
                        "memory_candidates": [],
                        "improvement_signals": [],
                    }
                )
                payload = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": content}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
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


def _session(path: Path, session_id: str, project: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        _row({"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}})
        + _row({"type": "event_msg", "payload": {"type": "agent_message", "message": message}})
    )


def _events(identity: Path) -> list[dict]:
    path = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _configure_model(monkeypatch, model: FakeLiteLLM, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "fake-model")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)


def test_current_lanes_preempt_large_newest_first_backfill_and_resume(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    historical_ids = [str(uuid.UUID(int=index + 1)) for index in range(12)]
    for index, session_id in enumerate(historical_ids):
        path = archived / f"history-{index:02d}.jsonl"
        _session(path, session_id, project, f"historical {index}")
        os.utime(path, (index + 1, index + 1))

    clock = FakeClock(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"), clock=clock)
    assistant.add_project(project)
    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        first = assistant_runtime.process_codex_cycle(assistant, active, archived)
        assert first["lanes"]["historical_backfill"]["progressed"] == 1
        assert [call["messages"][-1]["content"].splitlines()[1] for call in model.calls] == [
            f"Codex Session: {historical_ids[-1]}",
        ]

        current_id = str(uuid.UUID(int=1000))
        current_path = active / "current.jsonl"
        _session(current_path, current_id, project, "new current decision")
        second = assistant_runtime.process_codex_cycle(
            assistant, active, archived, capacity=2
        )
        assert second["lanes"]["active_collection"]["progressed"] == 1
        assert second["lanes"]["historical_backfill"]["progressed"] == 1

        clock.advance(minutes=5)
        third = assistant_runtime.process_codex_cycle(
            assistant, active, archived, capacity=2
        )
        assert third["lanes"]["newly_eligible_analysis"]["progressed"] == 1
        current_observation = next(
            event
            for event in _events(identity)
            if event.get("source_identity") == current_id
            and event.get("analysis_state") == "provisional"
        )
        assert current_observation["analysis_completed_at"] == "2026-08-27T00:05:00Z"
        assert third["lanes"]["historical_backfill"]["progressed"] == 1

        restarted = PersonalAssistant(
            root, resolve_observer(root, "observer"), clock=clock
        )
        for _ in range(20):
            cycle = assistant_runtime.process_codex_cycle(
                restarted, active, archived, capacity=2
            )
            if cycle["lanes"]["historical_backfill"]["backlog"] == 0:
                break
        assert cycle["lanes"]["historical_backfill"]["backlog"] == 0

    events = _events(identity)
    assert len(
        {
            event["event_id"]
            for event in events
            if event.get("source_event_schema") == "headlong.codex-source-event/v1"
        }
    ) == 2 * (len(historical_ids) + 1)
    health = TestClient(create_app(root)).get(
        "/api/identities/.identities~observer/assistant/health"
    )
    assert health.status_code == 200
    scheduling = health.json()["sources"]["codex"]["scheduling"]
    assert scheduling["historical_backfill"]["backlog"] == 0
    assert scheduling["active_collection"]["last_progress_at"] is not None
    assert scheduling["newly_eligible_analysis"]["last_progress_at"] is not None


def test_newly_archived_session_is_consolidated_before_historical_backfill(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    old_id = str(uuid.UUID(int=1))
    current_id = str(uuid.UUID(int=2))
    _session(archived / "old.jsonl", old_id, project, "old history")
    current = active / "current.jsonl"
    _session(current, current_id, project, "current work")
    clock = FakeClock(datetime(2026, 8, 27, tzinfo=timezone.utc))
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"), clock=clock)
    assistant.add_project(project)

    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        first = assistant_runtime.process_codex_cycle(
            assistant, active, archived, capacity=1
        )
        assert first["lanes"]["active_collection"]["progressed"] == 1
        current.rename(archived / "current.jsonl")
        second = assistant_runtime.process_codex_cycle(
            assistant, active, archived, capacity=1
        )
        assert second["lanes"]["newly_eligible_analysis"]["progressed"] == 1
        assert second["lanes"]["historical_backfill"]["progressed"] == 0
        assert model.calls[-1]["messages"][-1]["content"].splitlines()[1] == (
            f"Codex Session: {current_id}"
        )
        assert not any(
            event.get("source_identity") == old_id for event in _events(identity)
        )
