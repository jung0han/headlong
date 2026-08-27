"""DONGWOO-907 product slice: Registered Project -> Codex -> mind log."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web.assistant import EvidenceLocator
from headlong_web.assistant_cli import run
from headlong_web.server import create_app

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
IGNORED_ID = "cccccccc-3333-4333-8333-333333333333"
FUTURE_ID = "dddddddd-4444-4444-8444-444444444444"


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


def _session(path: Path, session_id: str, cwd: Path, *, complete: bool) -> bytes | None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": str(cwd), "git": {"branch": "test"}},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "RAW_TOOL_PAYLOAD_MUST_NOT_ENTER_THE_LEDGER",
            },
        },
    ]
    if complete:
        events.append(
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "finished the registered work",
                },
            }
        )
    rows = [json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in events]
    path.write_bytes(b"".join(rows))
    return rows[-1] if complete else None


class _FakeLiteLLM:
    def __init__(self):
        self.calls: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                content = json.dumps(
                    {
                        "title": "Completed registered work",
                        "observation": "The session completed its registered project task.",
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


def _command(root: Path, *args: str) -> int:
    return run(["--root", str(root), "--identity", "observer", *args])


def test_completed_codex_session_becomes_one_resolvable_compact_observation(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered-project"
    worktree = project / "worktrees" / "task"
    worktree.mkdir(parents=True)
    outside = tmp_path / "not-registered"
    outside.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    expected_evidence = _session(
        archived / "filename-does-not-contain-the-session-id.jsonl",
        SESSION_ID,
        worktree,
        complete=True,
    )
    _session(active / "still-active.jsonl", IGNORED_ID, project, complete=False)

    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_STRUCTURED_OUTPUT_MODE", "strict")

    assert _command(root, "project", "add", str(project), "--name", "registered") == 0
    added = json.loads(capsys.readouterr().out)
    assert added["name"] == "registered"
    assert Path(added["path"]) == project.resolve()
    assert _command(root, "project", "list") == 0
    assert json.loads(capsys.readouterr().out)["projects"] == [added]

    with _FakeLiteLLM() as model:
        monkeypatch.setenv("LLM_API_URL", model.url)
        assert _command(
            root,
            "observe-codex",
            "--sessions-root",
            str(active),
            "--archived-sessions-root",
            str(archived),
        ) == 0
        first = json.loads(capsys.readouterr().out)
        assert first == {"discovered": 1, "duplicate": 0, "eligible": 1, "observed": 1}
        assert len(model.calls) == 1
        assert model.calls[0]["response_format"]["type"] == "json_schema"
        sent = model.calls[0]["messages"][-1]["content"]
        assert SESSION_ID in sent
        assert "RAW_TOOL_PAYLOAD_MUST_NOT_ENTER_THE_LEDGER" in sent

        # Same source delivery is idempotent before another paid model call.
        assert _command(
            root,
            "observe-codex",
            "--sessions-root",
            str(active),
            "--archived-sessions-root",
            str(archived),
        ) == 0
        second = json.loads(capsys.readouterr().out)
        assert second == {"discovered": 1, "duplicate": 1, "eligible": 1, "observed": 0}
        assert len(model.calls) == 1

    client = TestClient(create_app(root))
    response = client.get("/api/identities/.identities~observer/mindlog?tail=10")
    assert response.status_code == 200, response.text
    observations = [
        step["raw"] for step in response.json()["steps"] if step["type"] == "observation"
    ]
    assert len(observations) == 1
    observation = observations[0]
    assert observation["event_schema"] == "headlong.activity-ledger/v1"
    assert observation["knowledge_scope"] == {
        "kind": "project",
        "project_id": added["id"],
    }
    assert observation["source_identity"] == SESSION_ID
    assert observation["authority"] == "candidate"
    assert observation["content"] == "The session completed its registered project task."
    locator = EvidenceLocator.decode(observation["evidence_locators"][0])
    encoded = locator.encode()

    assert _command(
        root,
        "resolve-evidence",
        encoded,
        "--sessions-root",
        str(active),
        "--archived-sessions-root",
        str(archived),
    ) == 0
    resolved = json.loads(capsys.readouterr().out)
    assert resolved["raw"].encode() == expected_evidence

    ledger = next((identity / "trajectories").glob("aaaaaaaa-*/trajectory.jsonl"))
    ledger_text = ledger.read_text()
    assert "RAW_TOOL_PAYLOAD_MUST_NOT_ENTER_THE_LEDGER" not in ledger_text
    assert "function_call_output" not in ledger_text

    # Removal changes future eligibility but preserves the existing ledger.
    assert _command(root, "project", "remove", added["id"]) == 0
    capsys.readouterr()
    _session(archived / "future.jsonl", FUTURE_ID, project, complete=True)
    with _FakeLiteLLM() as model:
        monkeypatch.setenv("LLM_API_URL", model.url)
        assert _command(
            root,
            "observe-codex",
            "--sessions-root",
            str(active),
            "--archived-sessions-root",
            str(archived),
        ) == 0
        removed = json.loads(capsys.readouterr().out)
        assert removed["eligible"] == 0
        assert removed["observed"] == 0
        assert model.calls == []
    assert ledger_text in ledger.read_text()
