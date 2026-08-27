"""DONGWOO-996: the continuously supervised Personal Assistant user journey."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web import archive_execution, assistant_runtime
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
CURRENT_ID = "bbbbbbbb-2222-4222-8222-222222222222"
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class FakeArchiveAdapter:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.results = [
            archive_execution.AdapterResult(
                "failed", error_code="command_failed", message="fake unavailable"
            ),
            archive_execution.AdapterResult("succeeded"),
            archive_execution.AdapterResult("succeeded"),
        ]

    def execute(self, operation: str, session_id: str) -> archive_execution.AdapterResult:
        self.calls.append((operation, session_id))
        return self.results.pop(0)


class FakeLiteLLM:
    """Serve strict auxiliary results and the free-form native monolith."""

    def __init__(self, malformed_session: str):
        self.calls: list[dict] = []
        self.malformed_session = malformed_session
        self.malformed_remaining = 1
        self.native_learned = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                prompt = "\n".join(str(message.get("content", "")) for message in body["messages"])
                if body.get("stream"):
                    if (
                        "Keep current native memory responsive" in prompt
                        and not owner.native_learned
                    ):
                        owner.native_learned = True
                        content = """I will retain the current project decision.
```bash
mem add --type decision "Keep current native memory responsive during Historical Backfill."
traj append --field type=thought --field content="Learned the current decision during backfill." --field source=monolith
FINAL="learned"
```"""
                    else:
                        content = """Nothing reusable changed.
```bash
traj append --field type=idle --field content=idle --field source=monolith
FINAL="idle"
```"""
                    chunks = [
                        {"choices": [{"delta": {"content": content}, "finish_reason": "stop"}]},
                        {
                            "choices": [],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                        },
                    ]
                    payload = (
                        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
                    session_id = next(
                        line.removeprefix("Codex Session: ")
                        for line in prompt.splitlines()
                        if line.startswith("Codex Session: ")
                    )
                    locators = [
                        line.removeprefix("EVIDENCE_LOCATOR ")
                        for line in prompt.splitlines()
                        if line.startswith("EVIDENCE_LOCATOR ")
                    ]
                    if session_id == owner.malformed_session and owner.malformed_remaining:
                        owner.malformed_remaining -= 1
                        result = {"title": "missing required structured fields"}
                    else:
                        current = session_id == CURRENT_ID
                        result = {
                            "title": "Current decision" if current else "Historical activity",
                            "observation": (
                                "Keep current native memory responsive during Historical Backfill."
                                if current
                                else f"Historical work observed for {session_id}."
                            ),
                            "evidence_locators": [locators[-1]],
                            "memory_candidates": [],
                            "improvement_signals": [],
                            "archive_candidates": (
                                [
                                    {
                                        "completion_state": "completed",
                                        "rationale": "The current work is complete.",
                                        "evidence_locators": [locators[-1]],
                                    }
                                ]
                                if current
                                else []
                            ),
                        }
                    response = {
                        "choices": [
                            {
                                "message": {"content": json.dumps(result)},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                    }
                    payload = json.dumps(response).encode()
                    content_type = "application/json"
                self.send_response(200)
                self.send_header("content-type", content_type)
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
    trajectory = identity / "trajectories" / "aaaaaaaa-root"
    trajectory.mkdir(parents=True)
    for directory in ("memories", "skills", "kernel", "workdir"):
        (identity / directory).mkdir()
    (identity / "info.txt").write_text(
        f"name=observer\ncreated=2026-08-27T00:00:00Z\nroot_trajectory={ROOT_TRAJ}\n"
    )
    (trajectory / "trajectory.jsonl").write_text(
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"}) + "\n"
    )
    shutil.copytree(REPO_ROOT / "thinkers" / "monolith", identity / "thinkers" / "monolith")
    shutil.copytree(REPO_ROOT / "thinkers" / "_lib", identity / "thinkers" / "_lib")
    return identity


def _session(path: Path, session_id: str, project: Path, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": str(project)}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": message}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _events(identity: Path) -> list[dict]:
    trajectory = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    return [json.loads(line) for line in trajectory.read_text().splitlines()]


def _thinker_env(identity: Path, model_url: str, home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{REPO_ROOT / 'bin'}:{REPO_ROOT / 'tools'}:{env['PATH']}",
            "HOME": str(home),
            "IDENTITY_DIR": str(identity),
            "IDENTITY_NAME": "observer",
            "TRAJ_DIR": str(identity / "trajectories"),
            "TRAJ_ID": ROOT_TRAJ,
            "ROOT_TRAJ_ID": ROOT_TRAJ,
            "THINKERS_DIR": str(identity / "thinkers"),
            "MEM_DIR": str(identity / "memories"),
            "SKILLS_DIR": str(identity / "skills"),
            "SKILLS_KERNEL_DIR": str(identity / "kernel"),
            "SHELLM_THINKER_ENV": "local",
            "MONOLITH_TIERED_MEMORY": "0",
            "MONOLITH_BACKOFF_BASE": "300",
            "MONOLITH_BACKOFF_CAP": "300",
            "LLM_PROVIDER": "openai",
            "LLM_API_URL": model_url,
            "LLM_STRUCTURED_OUTPUT_MODE": "strict",
            "SHELLM_MODEL": "fake-headlong-model",
            "OPENAI_API_KEY": "fake-test-key",
            "LLM_RETRIES": "0",
        }
    )
    return env


def _wait_for(identity: Path, *, memory: bool = False, step: str | None = None) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if memory and list((identity / "memories").glob("*.md")):
            return
        if step and any(event.get("type") == step for event in _events(identity)):
            return
        time.sleep(0.1)
    raise AssertionError("native Observer did not reach the expected state")


def test_integrated_runtime_preserves_current_learning_and_authorized_archive(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered-project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    active.mkdir(parents=True)
    archived.mkdir(parents=True)
    historical_ids = [str(uuid.UUID(int=index + 1)) for index in range(12)]
    for index, session_id in enumerate(historical_ids):
        path = archived / f"history-{index:02d}.jsonl"
        _session(path, session_id, project, f"historical activity {index}")
        os.utime(path, (index + 1, index + 1))
    malformed_id = historical_ids[-1]
    source_before = {path: path.read_bytes() for path in archived.glob("*.jsonl")}
    clock = FakeClock(datetime(2026, 8, 27, tzinfo=timezone.utc))
    adapter = FakeArchiveAdapter()

    with FakeLiteLLM(malformed_id) as model:
        env = _thinker_env(identity, model.url, tmp_path / "home")
        for key in (
            "LLM_PROVIDER",
            "LLM_API_URL",
            "LLM_STRUCTURED_OUTPUT_MODE",
            "SHELLM_MODEL",
            "OPENAI_API_KEY",
            "LLM_RETRIES",
        ):
            monkeypatch.setenv(key, env[key])
        assistant = PersonalAssistant(
            root,
            resolve_observer(root, "observer"),
            clock=clock,
            archive_adapter=adapter,
        )
        assistant.add_project(project)
        subprocess.run(
            [str(REPO_ROOT / "bin" / "thinkers"), "start", "monolith"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            _wait_for(identity, step="idle")
            first = assistant_runtime.process_codex_cycle(assistant, active, archived)
            assert first["analysis"]["failed"] == 1
            assert not any(
                event.get("source_identity") == malformed_id
                and event.get("analysis_state") in {"provisional", "final"}
                for event in _events(identity)
            )
            assert adapter.calls == []

            current_path = active / "current.jsonl"
            _session(
                current_path,
                CURRENT_ID,
                project,
                "Keep current native memory responsive while backfill continues.",
            )
            source_before[current_path] = current_path.read_bytes()
            second = assistant_runtime.process_codex_cycle(assistant, active, archived, capacity=2)
            assert second["lanes"]["active_collection"]["progressed"] == 1

            clock.advance(minutes=5)
            current = assistant_runtime.process_codex_cycle(assistant, active, archived, capacity=2)
            assert current["lanes"]["newly_eligible_analysis"]["progressed"] == 1
            assert current["lanes"]["historical_backfill"]["backlog"] > 0
            _wait_for(identity, memory=True)
            assistant.capture_native_memory_mutations()
            current_health = assistant_runtime.public_health(
                root, resolve_observer(root, "observer")
            )
            assert current_health["operations"]["native_memory_capture"]["active"] == 1
            assert current_health["operations"]["native_memory_capture"]["mutations"]["added"] == 1
            assert assistant.archive_candidates()[0]["review_state"] == "pending"
            assert adapter.calls == []

            restarted = PersonalAssistant(
                root,
                resolve_observer(root, "observer"),
                clock=clock,
                archive_adapter=adapter,
            )
            for _ in range(20):
                cycle = assistant_runtime.process_codex_cycle(
                    restarted, active, archived, capacity=3
                )
                if cycle["lanes"]["historical_backfill"]["backlog"] == 0:
                    break
            assert cycle["lanes"]["historical_backfill"]["backlog"] == 0

            for path in (identity / "memories").glob("*.md"):
                path.unlink()
            assert restarted.rebuild_native_memory()["active"] == 1
            memories = TestClient(create_app(root)).get(
                "/api/identities/.identities~observer/memories"
            )
            assert memories.status_code == 200
            assert len(memories.json()) == 1

            [candidate] = restarted.archive_candidates()
            failed = restarted.review_archive_candidates([candidate["candidate_id"]], "accepted")[
                "archive_candidates"
            ][0]
            assert failed["execution_state"] == "failed"
            recovered = PersonalAssistant(
                root,
                resolve_observer(root, "observer"),
                clock=clock,
                archive_adapter=adapter,
            )
            assert (
                recovered.retry_archive_candidate(candidate["candidate_id"])["execution_state"]
                == "succeeded"
            )
            assert recovered.unarchive_codex_session(CURRENT_ID)["execution_state"] == "succeeded"
        finally:
            subprocess.run(
                [str(REPO_ROOT / "bin" / "thinkers"), "stop", "--force"],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

    assert adapter.calls == [
        ("archive", CURRENT_ID),
        ("archive", CURRENT_ID),
        ("unarchive", CURRENT_ID),
    ]
    assert {path: path.read_bytes() for path in source_before} == source_before
    health = assistant_runtime.public_health(root, resolve_observer(root, "observer"))
    assert health["sources"]["codex"]["scheduling"]["historical_backfill"]["backlog"] == 0
    assert health["operations"]["native_memory_capture"]["active"] == 1
    assert health["operations"]["structured_results"]["mode"] == "strict"
    assert health["operations"]["structured_results"]["failures"] == 1
    assert health["operations"]["archive_candidate_review"]["accepted"] == 1
    assert health["operations"]["archive_execution"]["attempts"] == 3
    assert health["operations"]["archive_execution"]["failed"] == 1
    assert health["operations"]["archive_execution"]["succeeded"] == 2
    encoded = json.dumps(health)
    assert "fake-test-key" not in encoded
    assert "historical activity" not in encoded
    assert len(encoded) < 20_000
    health_markers = "".join(
        path.read_text() for path in (identity / "assistant" / "source-health").glob("*.json")
    )
    assert "fake-test-key" not in health_markers
    assert "Keep current native memory responsive" not in health_markers
    native_calls = [call for call in model.calls if call.get("stream")]
    auxiliary_calls = [call for call in model.calls if not call.get("stream")]
    assert native_calls and all("response_format" not in call for call in native_calls)
    assert auxiliary_calls and all(
        call["response_format"]["type"] == "json_schema" for call in auxiliary_calls
    )
