"""DONGWOO-919 complete proposal-only technical and Shadow Gate seam."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web import active_memory, references
from headlong_web.assistant import EvidenceLocator, PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run
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
                request = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(request)
                prompt = request["messages"][-1]["content"]
                if "Analysis kind:" in prompt:
                    locators = [
                        line.removeprefix("EVIDENCE_LOCATOR ")
                        for line in prompt.splitlines()
                        if line.startswith("EVIDENCE_LOCATOR ")
                    ]
                    result = {
                        "title": "Registered work reached a final state",
                        "observation": "The Codex task completed with grounded evidence.",
                        "evidence_locators": [locators[-1]],
                        "memory_candidates": [],
                        "improvement_signals": [],
                    }
                else:
                    result = {
                        "selected": True,
                        "title": "Bounded assistant reference",
                        "summary": "A controlled public source relevant to the assistant.",
                    }
                content = json.dumps(result)
                payload = json.dumps(
                    {
                        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
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


def _row(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode() + b"\n"


def _command(root: Path, *args: str) -> int:
    return run(["--root", str(root), "--identity", "observer", *args])


def _tree_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _ledger(identity: Path) -> list[dict]:
    path = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_complete_product_gate_is_restart_safe_rebuildable_and_proposal_only(
    tmp_path: Path, monkeypatch, capsys
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered-project"
    (project / ".git" / "hooks").mkdir(parents=True)
    (project / "work.txt").write_text("must remain unchanged\n")
    (project / ".git" / "config").write_text("[core]\n")
    (project / ".git" / "hooks" / "pre-commit").write_text("disabled sentinel\n")
    external = tmp_path / "external-authority"
    external.mkdir()
    (external / "personal-wiki.md").write_text("authoritative user text\n")
    (external / "linear-writes.jsonl").write_text("")
    external_before = _tree_snapshot(project) | {
        f"external/{key}": value for key, value in _tree_snapshot(external).items()
    }

    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    for index in range(20):
        session_id = str(uuid.UUID(int=index + 1))
        (archived / f"session-{index}.jsonl").write_bytes(
            _row({"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}})
            + _row(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "message": f"done {index}"},
                }
            )
        )

    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    assert _command(root, "project", "add", str(project), "--name", "registered") == 0
    project_record = json.loads(capsys.readouterr().out)
    assert _command(root, "web-source", "add", "https://example.com/reference") == 0
    capsys.readouterr()

    document = references.FetchedDocument(
        source_url="https://example.com/reference",
        media_type="text/plain",
        text="Controlled public reference text.",
    )
    monkeypatch.setattr(references, "fetch_public_document", lambda _url: document)
    clock = FakeClock(datetime(2026, 8, 27, 12, 0, tzinfo=timezone.utc))

    with FakeLiteLLM() as model:
        monkeypatch.setenv("LLM_API_URL", model.url)
        assistant = PersonalAssistant(root, resolve_observer(root, "observer"), clock=clock)
        first = assistant.process_codex_once(active, archived)
        assert first["analysis"]["final"] == 20
        assert first["collection"]["appended"] == 40
        assert assistant.observe_web_once()["saved"] == 1
        memory = assistant.remember_memory(
            "Keep this project decision scoped locally.",
            memory_kind="decision",
            memory_key="local-scope",
            project_selector=project_record["id"],
        )
        model_call_count = len(model.calls)

        # A new runtime instance exercises durable cursor and ledger recovery.
        restarted = PersonalAssistant(root, resolve_observer(root, "observer"), clock=clock)
        replay = restarted.process_codex_once(active, archived)
        assert replay["collection"]["appended"] == 0
        assert replay["analysis"]["final"] == 0
        assert len(model.calls) == model_call_count

    client = TestClient(create_app(root))
    observations_url = "/api/identities/.identities~observer/assistant/shadow-gate/observations"
    observations = client.get(observations_url).json()
    assert len(observations) == 20
    locator = EvidenceLocator.decode(observations[0]["evidence_locators"][0])
    assert restarted.resolve_evidence(locator, active, archived).endswith(b"\n")

    # All judgments are append-only dashboard actions.  Exactly 80% pass.
    for index, observation in enumerate(observations):
        response = client.post(
            f"{observations_url}/{observation['event_id']}/review",
            json={"useful": index < 16, "accurate": True},
        )
        assert response.status_code == 200, response.text
    memory_url = (
        "/api/identities/.identities~observer/assistant/shadow-gate/active-memories/"
        f"{memory['event_id']}/review"
    )
    assert client.post(memory_url, json={"correct": False}).status_code == 200
    unsafe = client.get(
        "/api/identities/.identities~observer/assistant/shadow-gate"
    ).json()
    assert unsafe["incorrect_active_memory_count"] == 1
    assert unsafe["ready"] is False
    assert client.post(memory_url, json={"correct": True}).status_code == 200

    report = client.get(
        "/api/identities/.identities~observer/assistant/shadow-gate"
    ).json()
    assert report["final_consolidation_count"] == 20
    assert report["reviewed_observation_count"] == 20
    assert report["useful_and_accurate_rate"] == 0.8
    assert report["incorrect_active_memory_count"] == 0
    assert report["threshold"] == {
        "duration_days": 7,
        "final_consolidations": 20,
        "rule": "whichever_occurs_first",
        "duration_reached": False,
        "final_count_reached": True,
        "reached": True,
    }
    assert report["ready"] is True
    assert report["authority"] == {
        "mode": "proposal_only",
        "external_writes_enabled": False,
        "hook_adapters_enabled": False,
        "project_mounts_enabled": False,
    }

    # Delete the only file projection, rebuild through the dashboard, and compare.
    projected_before = restarted.active_memories(project_record["id"])
    projection = active_memory.projection_dir(identity)
    for path in projection.glob("*"):
        path.unlink()
    projection.rmdir()
    rebuilt = client.post(
        "/api/identities/.identities~observer/active-memories/rebuild"
    )
    assert rebuilt.status_code == 200, rebuilt.text
    assert restarted.active_memories(project_record["id"]) == projected_before
    assert client.get("/api/identities/.identities~observer/proposals").json() == []

    external_after = _tree_snapshot(project) | {
        f"external/{key}": value for key, value in _tree_snapshot(external).items()
    }
    assert external_after == external_before
    ledger = _ledger(identity)
    assert len([e for e in ledger if e.get("type") == "observation-evaluation"]) == 20
    assert len([e for e in ledger if e.get("type") == "active-memory-evaluation"]) == 2


def test_seven_elapsed_days_reaches_maturity_without_enabling_authority(
    tmp_path: Path
):
    """The time side of the OR threshold is independent of the count side."""
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    event_id = str(uuid.uuid4())
    locator = {
        "schema": "headlong.evidence-locator/v1",
        "kind": "codex_event",
        "source_identity": str(uuid.uuid4()),
        "source_root": "archived",
        "relative_path": "session.jsonl",
        "line": 1,
        "byte_offset": 0,
        "byte_length": 2,
        "sha256": "a" * 64,
        "host": "test",
    }
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    path = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    with path.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "type": "observation",
                    "step_id": event_id,
                    "event_id": event_id,
                    "analysis_state": "final",
                    "analysis_completed_at": start.isoformat(),
                    "authority": "candidate",
                    "knowledge_scope": {"kind": "project", "project_id": "p"},
                    "evidence_locators": [locator],
                    "title": "one final",
                    "content": "one final observation",
                }
            )
            + "\n"
        )
    assistant = PersonalAssistant(
        root,
        resolve_observer(root, "observer"),
        clock=lambda: start + timedelta(days=7),
    )
    reviewed = assistant.review_observation(event_id, useful=True, accurate=True)
    assert reviewed["evaluation"]["useful"] is True
    report = assistant.shadow_gate_report()
    assert report["threshold"]["duration_reached"] is True
    assert report["threshold"]["final_count_reached"] is False
    assert report["ready"] is True
    assert report["authority"]["external_writes_enabled"] is False
