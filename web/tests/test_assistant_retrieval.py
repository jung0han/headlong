"""DONGWOO-917 product tests for scoped assistant responses."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from headlong_web import references
from headlong_web.assistant import PersonalAssistant, resolve_observer
from headlong_web.assistant_cli import run

ROOT_TRAJ = "aaaaaaaa-1111-4111-8111-111111111111"


class FakeResponseModel:
    def __init__(self) -> None:
        owner = self
        self.calls: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["content-length"]))
                )
                owner.calls.append(request)
                prompt = json.loads(request["messages"][-1]["content"])
                context = prompt["scoped_context"]
                memory = " ".join(
                    item["content"] for item in context["active_memories"]
                )
                titles = " ".join(item["title"] for item in context["references"])
                if (
                    "Use Ruff format." in memory
                    and "Never commit generated artifacts." in memory
                    and "Use Black." not in memory
                    and "Ruff migration guide" in titles
                ):
                    answer = (
                        "Use Ruff format, keep generated artifacts out of Git, "
                        "and consult the Ruff migration guide."
                    )
                else:
                    answer = "SCOPED_CONTEXT_MISMATCH"
                payload = json.dumps(
                    {
                        "choices": [
                            {"message": {"content": answer}, "finish_reason": "stop"}
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
        json.dumps({"type": "trajectory", "step_id": ROOT_TRAJ, "ts": "t0"})
        + "\n"
    )
    return identity


def _setup_scoped_knowledge(
    tmp_path: Path,
) -> tuple[Path, Path, PersonalAssistant, str, str]:
    root = tmp_path / "headlong"
    root.mkdir()
    identity_dir = _identity(root)
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    registered_a = assistant.add_project(project_a, "project-a")
    registered_b = assistant.add_project(project_b, "project-b")
    assistant.remember_memory(
        "Use Ruff format.",
        memory_kind="decision",
        memory_key="formatter",
        project_selector=registered_a.id,
    )
    assistant.remember_memory(
        "Use Black.",
        memory_kind="decision",
        memory_key="formatter",
        project_selector=registered_b.id,
    )
    assistant.remember_memory(
        "Never commit generated artifacts.",
        memory_kind="constraint",
        memory_key="generated-artifacts",
        global_scope=True,
    )
    references.store_reference(
        identity_dir,
        references.FetchedDocument(
            source_url="https://example.com/ruff-migration",
            media_type="text/plain",
            text=(
                "Ruff migration guide. Replace formatter configuration carefully "
                "and verify generated artifacts remain ignored."
            ),
        ),
        fetched_at="2026-08-27T02:00:00Z",
        title="Ruff migration guide",
        summary="A concise guide to adopting Ruff format.",
        knowledge_scope={"kind": "global"},
    )
    return root, identity_dir, assistant, registered_a.id, registered_b.id


def test_reference_scope_is_filtered_before_ranking(tmp_path: Path):
    root, identity_dir, assistant, project_a, project_b = _setup_scoped_knowledge(
        tmp_path
    )
    references.store_reference(
        identity_dir,
        references.FetchedDocument(
            source_url="https://example.com/project-a-secret",
            media_type="text/plain",
            text="Project Alpha uses a secret marmalade deployment rule.",
        ),
        fetched_at="2026-08-27T03:00:00Z",
        title="Marmalade deployment rule",
        summary="A project-local marmalade rule.",
        knowledge_scope={"kind": "project", "project_id": project_a},
    )

    visible = assistant.response_context("marmalade deployment", project_a)
    hidden = assistant.response_context("marmalade deployment", project_b)
    assert [item["title"] for item in visible["references"]] == [
        "Marmalade deployment rule"
    ]
    assert hidden["references"] == []


def test_each_ranked_reference_retains_its_own_scope(tmp_path: Path):
    _root, identity_dir, assistant, project_a, _project_b = _setup_scoped_knowledge(
        tmp_path
    )
    references.store_reference(
        identity_dir,
        references.FetchedDocument(
            source_url="https://example.com/project-a-artifacts",
            media_type="text/plain",
            text="Project Alpha generated artifacts require a local review.",
        ),
        fetched_at="2026-08-27T03:00:00Z",
        title="Project Alpha artifact review",
        summary="A project-local generated artifacts rule.",
        knowledge_scope={"kind": "project", "project_id": project_a},
    )

    context = assistant.response_context("generated artifacts", project_a)
    scopes = {
        item["title"]: item["knowledge_scope"] for item in context["references"]
    }
    assert scopes == {
        "Project Alpha artifact review": {
            "kind": "project",
            "project_id": project_a,
        },
        "Ruff migration guide": {"kind": "global"},
    }


def test_legacy_reference_without_scope_remains_explicitly_global(tmp_path: Path):
    _root, identity_dir, assistant, project_a, _project_b = _setup_scoped_knowledge(
        tmp_path
    )
    metadata_path = next(
        (identity_dir / "assistant" / "references").glob("web-*/*/metadata.json")
    )
    metadata = json.loads(metadata_path.read_text())
    metadata.pop("knowledge_scope")
    metadata_path.write_text(json.dumps(metadata))

    context = assistant.response_context("Ruff migration", project_a)
    assert context["references"][0]["knowledge_scope"] == {"kind": "global"}


def _configure_model(monkeypatch, model: FakeResponseModel, tmp_path: Path) -> None:
    monkeypatch.setenv("HEADLONG_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("SHELLM_MODEL", "deepseek-flash-v4-private")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-test-key")
    monkeypatch.setenv("LLM_RETRIES", "0")
    monkeypatch.setenv("LLM_API_URL", model.url)


def test_public_response_uses_project_global_and_reference_without_contamination(
    tmp_path: Path, monkeypatch, capsys
):
    root, _identity_dir, assistant, project_a, _project_b = _setup_scoped_knowledge(
        tmp_path
    )
    query = "Which formatter and generated artifacts rule does the Ruff guide support?"
    with FakeResponseModel() as model:
        _configure_model(monkeypatch, model, tmp_path)
        assert (
            run(
                [
                    "--root",
                    str(root),
                    "--identity",
                    "observer",
                    "respond",
                    query,
                    "--project",
                    project_a,
                ]
            )
            == 0
        )
    result = json.loads(capsys.readouterr().out)
    assert result["response"] == (
        "Use Ruff format, keep generated artifacts out of Git, and consult the "
        "Ruff migration guide."
    )
    assert result["project_id"] == project_a
    assert len(model.calls) == 1
    sent = model.calls[0]["messages"][-1]["content"]
    assert "Use Ruff format." in sent
    assert "Never commit generated artifacts." in sent
    assert "Ruff migration guide" in sent
    assert "Use Black." not in sent

    assert {item["kind"] for item in result["evidence"]} == {
        "active_memory",
        "reference",
    }
    resolved = [
        assistant.resolve_response_evidence(item["locator"])
        for item in result["evidence"]
    ]
    assert any(item["kind"] == "activity_ledger_event" for item in resolved)
    assert any(
        item["kind"] == "web_reference"
        and item["reference"]["text"].startswith("Ruff migration guide")
        for item in resolved
    )
    reference_evidence = next(
        item for item in result["evidence"] if item["kind"] == "reference"
    )
    assert (
        run(
            [
                "--root",
                str(root),
                "--identity",
                "observer",
                "resolve-response-evidence",
                json.dumps(reference_evidence["locator"]),
            ]
        )
        == 0
    )
    drilled_down = json.loads(capsys.readouterr().out)
    assert drilled_down["reference"]["text"].startswith("Ruff migration guide")


def test_context_repairs_stale_projection_without_changing_authorities(
    tmp_path: Path,
):
    root, identity_dir, assistant, project_a, _project_b = _setup_scoped_knowledge(
        tmp_path
    )
    trajectory = next((identity_dir / "trajectories").glob("*/trajectory.jsonl"))
    ledger_before = trajectory.read_bytes()
    reference_before = {
        path.relative_to(identity_dir): path.read_bytes()
        for path in (identity_dir / "assistant" / "references").rglob("*")
        if path.is_file()
    }
    projection = identity_dir / "assistant" / "projections" / "active-memory"
    # A valid but incomplete view is stale rather than syntactically corrupt.
    sorted(projection.glob("*.md"))[0].unlink()

    context = assistant.response_context(
        "Ruff formatter generated artifacts migration guide", project_a
    )
    assert {item["content"] for item in context["active_memories"]} == {
        "Use Ruff format.",
        "Never commit generated artifacts.",
    }
    assert [item["title"] for item in context["references"]] == [
        "Ruff migration guide"
    ]
    assert len(list(projection.glob("*.md"))) == 3
    assert trajectory.read_bytes() == ledger_before
    assert {
        path.relative_to(identity_dir): path.read_bytes()
        for path in (identity_dir / "assistant" / "references").rglob("*")
        if path.is_file()
    } == reference_before

    for path in projection.glob("*.md"):
        path.unlink()
    projection.rmdir()
    missing_rebuilt = assistant.response_context(
        "Ruff formatter generated artifacts migration guide", project_a
    )
    assert missing_rebuilt["active_memories"] == context["active_memories"]
    assert len(list(projection.glob("*.md"))) == 3
    assert trajectory.read_bytes() == ledger_before
    assert {
        path.relative_to(identity_dir): path.read_bytes()
        for path in (identity_dir / "assistant" / "references").rglob("*")
        if path.is_file()
    } == reference_before
