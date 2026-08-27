"""DONGWOO-987 product slice: current Codex activity -> native HeadLong Memory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.server import create_app


ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"
SESSION_ID = "bbbbbbbb-2222-4222-8222-222222222222"
REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


class FakeLiteLLM:
    """Serve both the auxiliary analysis and free-form monolith calls."""

    def __init__(self):
        self.calls: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                owner.calls.append(body)
                prompt = "\n".join(
                    str(message.get("content", "")) for message in body["messages"]
                )
                if "EVIDENCE_LOCATOR " in prompt:
                    locators = [
                        line.removeprefix("EVIDENCE_LOCATOR ")
                        for line in prompt.splitlines()
                        if line.startswith("EVIDENCE_LOCATOR ")
                    ]
                    result = {
                        "title": "Native learning remains the project decision",
                        "observation": (
                            "The project decided to preserve autonomous native "
                            "HeadLong learning."
                        ),
                        "evidence_locators": [locators[-1]],
                        "memory_candidates": [],
                        "improvement_signals": [],
                    }
                    content = json.dumps(result)
                elif "preserve autonomous native HeadLong learning" in prompt:
                    content = """I will retain the reusable project decision.
```bash
mem add --type decision "This project preserves autonomous native HeadLong learning."
traj append --field type=thought --field content="Learned the project's native-learning decision." --field source=monolith
FINAL="learned"
```"""
                else:
                    content = """Nothing needs attention yet.
```bash
traj append --field type=idle --field content=idle --field source=monolith
FINAL="idle"
```"""
                response = {
                    "choices": [
                        {"message": {"content": content}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 10},
                }
                if body.get("stream"):
                    chunks = [
                        {
                            "choices": [
                                {
                                    "delta": {"content": content},
                                    "finish_reason": "stop",
                                }
                            ]
                        },
                        {"choices": [], "usage": response["usage"]},
                    ]
                    payload = (
                        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
                        + "data: [DONE]\n\n"
                    ).encode()
                    content_type = "text/event-stream"
                else:
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
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"})
        + "\n"
    )
    shutil.copytree(
        REPO_ROOT / "thinkers" / "monolith", identity / "thinkers" / "monolith"
    )
    shutil.copytree(REPO_ROOT / "thinkers" / "_lib", identity / "thinkers" / "_lib")
    return identity


def _session(path: Path, project: Path) -> None:
    path.parent.mkdir(parents=True)
    rows = [
        {"type": "session_meta", "payload": {"id": SESSION_ID, "cwd": str(project)}},
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "Preserve autonomous native HeadLong learning.",
            },
        },
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


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
            "SHELLM_MODEL": "fake-headlong-model",
            "OPENAI_API_KEY": "fake-test-key",
            "LLM_RETRIES": "0",
        }
    )
    return env


def _wait_for_memory(identity: Path, timeout: float = 15) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        memories = list((identity / "memories").glob("*.md"))
        if memories:
            return memories[0]
        time.sleep(0.1)
    raise AssertionError("native HeadLong memory did not surface")


def _wait_for_step(identity: Path, step_type: str, timeout: float = 15) -> None:
    trajectory = identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            json.loads(line).get("type") == step_type
            for line in trajectory.read_text().splitlines()
        ):
            return
        time.sleep(0.1)
    raise AssertionError(f"monolith did not append {step_type}")


def test_current_codex_observation_wakes_native_learning_within_five_minutes(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    project = tmp_path / "registered-project"
    project.mkdir()
    active = tmp_path / "codex" / "sessions"
    archived = tmp_path / "codex" / "archived_sessions"
    archived.mkdir(parents=True)
    _session(active / "current.jsonl", project)
    clock = FakeClock(datetime(2026, 8, 27, tzinfo=timezone.utc))

    with FakeLiteLLM() as model:
        env = _thinker_env(identity, model.url, tmp_path / "home")
        for key, value in env.items():
            if key in {
                "LLM_PROVIDER",
                "LLM_API_URL",
                "SHELLM_MODEL",
                "OPENAI_API_KEY",
                "LLM_RETRIES",
            }:
                monkeypatch.setenv(key, value)
        assistant = PersonalAssistant(
            root, resolve_observer(root, "observer"), clock=clock
        )
        registered = assistant.add_project(project)
        subprocess.run(
            [str(REPO_ROOT / "bin" / "thinkers"), "start", "monolith"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            # `thinkers start` uses the public manual-trigger boundary. Let its
            # bootstrap idle finish so the Codex Observation is the next wake.
            _wait_for_step(identity, "idle")
            initial = assistant.process_codex_once(active, archived)
            assert initial["analysis"]["provisional"] == 0
            assert list((identity / "memories").glob("*.md")) == []

            clock.advance(minutes=5)
            analyzed = assistant.analyze_codex_once(active, archived)
            assert analyzed["provisional"] == 1
            memory_path = _wait_for_memory(identity)
            assert assistant.memory_candidates(registered.id) == []
            native_call = next(
                call
                for call in model.calls
                if call.get("stream")
                and "preserve autonomous native HeadLong learning" in "\n".join(
                    str(message.get("content", ""))
                    for message in call["messages"]
                )
            )
            assert "response_format" not in native_call
        finally:
            subprocess.run(
                [str(REPO_ROOT / "bin" / "thinkers"), "stop", "--force"],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )

    events = [
        json.loads(line)
        for line in (
            identity / "trajectories" / "aaaaaaaa-root" / "trajectory.jsonl"
        ).read_text().splitlines()
    ]
    observation = next(
        event
        for event in events
        if event.get("type") == "observation"
        and event.get("source_identity") == SESSION_ID
    )
    assert observation["knowledge_scope"] == {
        "kind": "project",
        "project_id": registered.id,
    }
    assert observation["evidence_locators"]
    assert len(observation["content"]) < 1200
    assert any(
        event.get("type") == "thought"
        and event.get("source") == "monolith"
        and "native-learning decision" in event.get("content", "")
        for event in events
    )

    client = TestClient(create_app(root))
    listed = client.get("/api/identities/.identities~observer/memories")
    assert listed.status_code == 200
    assert len(listed.json()) == 1
    assert listed.json()[0]["type"] == "decision"
    shown = client.get(
        f"/api/identities/.identities~observer/memories/{memory_path.name}"
    )
    assert shown.status_code == 200
    assert "preserves autonomous native HeadLong learning" in shown.json()["content"]
