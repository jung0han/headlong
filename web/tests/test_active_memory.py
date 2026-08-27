"""DONGWOO-910 product tests for authority-aware scoped Active Memory."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
from headlong_web.server import create_app

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_A = "bbbbbbbb-2222-4222-8222-222222222222"


class FakeLiteLLM:
    def __init__(self):
        owner = self
        self.calls: list[dict] = []

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
                content = json.dumps(
                    {
                        "title": "A candidate was inferred",
                        "observation": "The model found a possible local preference.",
                        "evidence_locators": [locators[-1]],
                        "memory_candidates": [
                            {
                                "content": "Prefer the model-suggested formatter.",
                                "evidence_locators": [locators[-1]],
                            }
                        ],
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


def _session(path: Path, session_id: str, cwd: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": str(cwd)}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "message": (
                    "An agent suggested a formatter; the user did not accept it."
                ),
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _command(root: Path, *args: str) -> int:
    return run(["--root", str(root), "--identity", "observer", *args])


def _configure_model(monkeypatch, model: FakeLiteLLM, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)


def test_model_candidate_requires_user_authority_before_activation(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    _identity(root)
    project = tmp_path / "project-a"
    other_project = tmp_path / "project-b"
    project.mkdir()
    other_project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    _session(archived / "session.jsonl", SESSION_A, project)

    assert _command(root, "project", "add", str(project), "--name", "project-a") == 0
    registered = json.loads(capsys.readouterr().out)
    assert (
        _command(root, "project", "add", str(other_project), "--name", "project-b") == 0
    )
    other_registered = json.loads(capsys.readouterr().out)
    with FakeLiteLLM() as model:
        _configure_model(monkeypatch, model, tmp_path)
        assistant = PersonalAssistant(
            root,
            resolve_observer(root, "observer"),
            clock=lambda: datetime(2026, 8, 27, tzinfo=UTC),
        )
        assert assistant.process_codex_once(active, archived)["analysis"]["final"] == 1
        assert (
            assistant.process_codex_once(active, archived)["analysis"]["duplicate"] == 1
        )

    assert _command(root, "memory", "candidates", "--project", registered["id"]) == 0
    candidates = json.loads(capsys.readouterr().out)["memory_candidates"]
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["authority"] == "candidate"
    assert candidate["evidence_kind"] == "model_inference"
    assert candidate["knowledge_scope"] == {
        "kind": "project",
        "project_id": registered["id"],
    }

    assert _command(root, "memory", "list", "--project", registered["id"]) == 0
    assert json.loads(capsys.readouterr().out)["active_memories"] == []

    assert (
        _command(
            root,
            "memory",
            "accept",
            candidate["event_id"],
            "--kind",
            "preference",
            "--key",
            "formatter",
            "--project",
            other_registered["id"],
        )
        == 2
    )
    assert "cannot activate in another project" in capsys.readouterr().err

    assert (
        _command(
            root,
            "memory",
            "accept",
            candidate["event_id"],
            "--kind",
            "preference",
            "--key",
            "formatter",
        )
        == 0
    )
    activated = json.loads(capsys.readouterr().out)
    assert activated["authority"] == "active"
    assert activated["authority_basis"] == "user_accepted_candidate"
    assert activated["causal_event_ids"] == [candidate["event_id"]]
    assert activated["evidence_locators"] == candidate["evidence_locators"]

    assert _command(root, "memory", "list", "--project", registered["id"]) == 0
    listed = json.loads(capsys.readouterr().out)["active_memories"]
    assert [item["content"] for item in listed] == [candidate["content"]]


def test_project_default_global_authority_supersession_and_rebuild(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    assert _command(root, "project", "add", str(project_a)) == 0
    registered_a = json.loads(capsys.readouterr().out)
    assert _command(root, "project", "add", str(project_b)) == 0
    registered_b = json.loads(capsys.readouterr().out)

    monkeypatch.chdir(project_a)
    assert (
        _command(
            root,
            "memory",
            "remember",
            "Use Black.",
            "--kind",
            "decision",
            "--key",
            "formatter",
        )
        == 0
    )
    first_a = json.loads(capsys.readouterr().out)
    assert first_a["knowledge_scope"]["project_id"] == registered_a["id"]

    assert (
        _command(
            root,
            "memory",
            "remember",
            "Use Ruff format.",
            "--kind",
            "decision",
            "--key",
            "formatter",
        )
        == 0
    )
    second_a = json.loads(capsys.readouterr().out)
    assert second_a["supersedes_event_ids"] == [first_a["event_id"]]

    monkeypatch.chdir(project_b)
    assert (
        _command(
            root,
            "memory",
            "remember",
            "Use yapf.",
            "--kind",
            "decision",
            "--key",
            "formatter",
        )
        == 0
    )
    first_b = json.loads(capsys.readouterr().out)
    assert first_b["knowledge_scope"]["project_id"] == registered_b["id"]

    assert (
        _command(
            root,
            "memory",
            "remember",
            "Always keep generated files out of Git.",
            "--kind",
            "constraint",
            "--key",
            "generated-files",
            "--global",
        )
        == 0
    )
    global_memory = json.loads(capsys.readouterr().out)
    assert global_memory["knowledge_scope"] == {"kind": "global"}
    assert global_memory["authority_basis"] == "explicit_user_statement"

    assert _command(root, "memory", "list", "--project", registered_a["id"]) == 0
    before = json.loads(capsys.readouterr().out)["active_memories"]
    assert {item["content"] for item in before} == {
        "Use Ruff format.",
        "Always keep generated files out of Git.",
    }
    assert "Use yapf." not in {item["content"] for item in before}

    projection = identity / "assistant" / "projections" / "active-memory"
    assert list(projection.glob("*.md"))
    for path in projection.glob("*.md"):
        path.unlink()
    projection.rmdir()
    assert _command(root, "memory", "rebuild") == 0
    rebuilt = json.loads(capsys.readouterr().out)
    assert rebuilt["active"] == 3
    assert _command(root, "memory", "list", "--project", registered_a["id"]) == 0
    assert json.loads(capsys.readouterr().out)["active_memories"] == before

    client = TestClient(create_app(root))
    response = client.get(
        "/api/identities/.identities~observer/active-memories",
        params={"project_id": registered_b["id"], "include_global": "true"},
    )
    assert response.status_code == 200
    assert {item["content"] for item in response.json()} == {
        "Use yapf.",
        "Always keep generated files out of Git.",
    }
    api_activation = client.post(
        "/api/identities/.identities~observer/active-memories",
        json={
            "content": "Keep API output stable.",
            "memory_kind": "constraint",
            "memory_key": "api-output",
            "project_id": registered_b["id"],
        },
    )
    assert api_activation.status_code == 200
    assert api_activation.json()["authority_basis"] == "explicit_user_statement"
    assert (
        client.post(
            "/api/identities/.identities~observer/active-memories",
            json={
                "content": "Untrusted web text cannot declare its own authority.",
                "memory_kind": "constraint",
                "memory_key": "untrusted",
                "project_id": registered_b["id"],
                "source_kind": "web_source",
            },
        ).status_code
        == 422
    )
    assert first_a["event_id"] in {
        event_id for item in before for event_id in item["supersedes_event_ids"]
    }
