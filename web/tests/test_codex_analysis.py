"""DONGWOO-909 product tests for the Codex Session analysis lifecycle."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"


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
        self.fail = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                if owner.fail:
                    payload = b'{"error":{"message":"temporary route failure"}}'
                    self.send_response(500)
                else:
                    prompt = body["messages"][-1]["content"]
                    locators = [
                        line.removeprefix("EVIDENCE_LOCATOR ")
                        for line in prompt.splitlines()
                        if line.startswith("EVIDENCE_LOCATOR ")
                    ]
                    result = {
                        "title": "Session work changed",
                        "observation": "The registered work has a new meaningful state.",
                        "evidence_locators": [locators[-1]],
                        "memory_candidates": [
                            {
                                "content": "A project-local decision may need remembering.",
                                "evidence_locators": [locators[-1]],
                            }
                        ],
                        "improvement_signals": [
                            {
                                "kind": "tool_failure",
                                "content": "A tool failure may justify a later proposal.",
                                "evidence_locators": [locators[-1]],
                            }
                        ],
                    }
                    content = json.dumps(result)
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
    traj = identity / "trajectories" / "aaaaaaaa-root"
    traj.mkdir(parents=True)
    (identity / "info.txt").write_text(
        f"name=observer\ncreated=2026-08-27T00:00:00Z\nroot_trajectory={ROOT_TRAJ}\n"
    )
    (traj / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"}) + "\n"
    )
    return identity


def _command(root: Path, *args: str) -> int:
    return run(["--root", str(root), "--identity", "observer", *args])


def _events(identity: Path) -> list[dict]:
    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    return [json.loads(line) for line in ledger.read_text().splitlines()]


def _assistant(root: Path, clock: FakeClock) -> PersonalAssistant:
    return PersonalAssistant(root, resolve_observer(root, "observer"), clock=clock)


def _configure_model(monkeypatch, model: FakeLiteLLM, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)


def test_provisional_final_and_later_revision_are_timed_and_append_only(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    session = active / "session.jsonl"
    session.write_bytes(
        _row({"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": str(project)}})
        + _row({"type": "event_msg", "payload": {"type": "turn_started"}})
    )
    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()

    clock = FakeClock(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
    assistant = _assistant(root, clock)
    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        first = assistant.process_codex_once(active, archived)
        assert first["analysis"]["waiting"] == 1
        assert first["analysis"]["provisional"] == 0
        assert model.calls == []

        clock.advance(minutes=4, seconds=59)
        assert assistant.analyze_codex_once(active, archived)["provisional"] == 0
        clock.advance(seconds=1)
        assert assistant.analyze_codex_once(active, archived)["provisional"] == 1
        assert len(model.calls) == 1
        assert assistant.analyze_codex_once(active, archived)["duplicate"] == 1

        clock.advance(minutes=24, seconds=59)
        assert assistant.analyze_codex_once(active, archived)["final"] == 0
        clock.advance(seconds=1)
        assert assistant.analyze_codex_once(active, archived)["final"] == 1
        assert len(model.calls) == 2

        before_append = _events(identity)
        first_final = next(
            event
            for event in before_append
            if event.get("analysis_state") == "final"
        )
        first_provisional = next(
            event
            for event in before_append
            if event.get("analysis_state") == "provisional"
        )
        assert first_final["supersedes_event_ids"] == [first_provisional["event_id"]]
        assert first_final["memory_candidates"][0]["evidence_locators"]
        assert first_final["improvement_signals"][0]["evidence_locators"]

        clock.advance(minutes=1)
        with session.open("ab") as fh:
            fh.write(_row({"type": "event_msg", "payload": {"type": "turn_started"}}))
        follow = assistant.follow_codex_once(active, archived)
        assert follow["appended"] == 1
        superseded = [
            event
            for event in _events(identity)
            if event.get("analysis_state") == "superseded"
        ]
        assert len(superseded) == 1
        assert superseded[0]["supersedes_event_ids"] == [first_final["event_id"]]

        clock.advance(minutes=5)
        assert assistant.analyze_codex_once(active, archived)["provisional"] == 1
        second_provisional = [
            event
            for event in _events(identity)
            if event.get("analysis_state") == "provisional"
        ][-1]
        assert second_provisional["source_revision_digest"] != first_provisional[
            "source_revision_digest"
        ]
        assert first_final["event_id"] in second_provisional["supersedes_event_ids"]

        session.rename(archived / "session.jsonl")
        assert assistant.process_codex_once(active, archived)["analysis"]["final"] == 1

    states = {
        event.get("analysis_state")
        for event in _events(identity)
        if event.get("analysis_state")
    }
    assert {"provisional", "final", "superseded"} <= states
    visible = TestClient(create_app(root)).get(
        "/api/identities/.identities~observer/mindlog?tail=100"
    )
    assert visible.status_code == 200
    assert states <= {
        step["raw"].get("analysis_state")
        for step in visible.json()["steps"]
        if step.get("raw")
    }


def test_model_failure_keeps_cursor_and_final_marker_retriable(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    session = archived / "session.jsonl"
    session.write_bytes(
        _row({"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": str(project)}})
        + _row({"type": "event_msg", "payload": {"type": "task_complete"}})
    )
    assert _command(root, "project", "add", str(project)) == 0
    capsys.readouterr()
    clock = FakeClock(datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc))
    assistant = _assistant(root, clock)

    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        model.fail = True
        result = assistant.process_codex_once(active, archived)
        assert result["collection"]["appended"] == 2
        assert result["analysis"]["failed"] == 1
        cursor_path = identity / "assistant" / "cursors" / "codex" / f"{SESSION_ID}.json"
        cursor_before = json.loads(cursor_path.read_text())
        state_path = identity / "assistant" / "analysis" / "codex" / f"{SESSION_ID}.json"
        failed_state = json.loads(state_path.read_text())
        assert failed_state["status"] == "failed"
        assert failed_state["final_event_id"] is None
        assert not any(
            event.get("analysis_state") == "final" for event in _events(identity)
        )

        health = json.loads(
            (identity / "assistant" / "source-health" / "codex.json").read_text()
        )
        assert health["status"] == "degraded"
        assert health["analysis"]["sessions"][0]["status"] == "failed"

        model.fail = False
        retried = assistant.analyze_codex_once(active, archived)
        assert retried["final"] == 1
        assert json.loads(cursor_path.read_text()) == cursor_before
        final_state = json.loads(state_path.read_text())
        assert final_state["status"] == "final"
        assert final_state["final_event_id"]
        final = next(
            event for event in _events(identity) if event.get("analysis_state") == "final"
        )
        failed = next(
            event for event in _events(identity) if event.get("analysis_state") == "failed"
        )
        assert failed["event_id"] in final["supersedes_event_ids"]

