"""Regression coverage for the model gateway's documented env population."""

from __future__ import annotations

import subprocess
from pathlib import Path

from headlong_web import discovery, model_gateway


def _identity(root: Path) -> discovery.IdentityInfo:
    path = root / ".identities" / "observer"
    path.mkdir(parents=True)
    (path / "info.txt").write_text(
        "name=observer\nroot_trajectory=aaaaaaaa-1111-4111-8111-111111111111\n"
    )
    return discovery.IdentityInfo(
        id=".identities~observer",
        name="observer",
        path=path,
        path_rel=".identities/observer",
        created=None,
        root_trajectory="aaaaaaaa-1111-4111-8111-111111111111",
        group=".identities",
    )


def test_real_environment_wins_over_root_and_identity_files(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    (root / ".env").write_text("SHELLM_MODEL=root-model\nOPENAI_API_KEY=root-key\n")
    (identity.path / ".env").write_text(
        "SHELLM_MODEL=identity-model\nOPENAI_API_KEY=identity-key\n"
    )
    monkeypatch.setenv("SHELLM_MODEL", "process-model")
    monkeypatch.setenv("OPENAI_API_KEY", "process-key")
    seen: dict[str, str] = {}

    def fake_run(*_args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(model_gateway.subprocess, "run", fake_run)
    result = model_gateway.ModelGateway(root, identity).complete_text(
        "prompt", system="system", token_timeout=30, operation="env test"
    )

    assert result == "ok"
    assert seen["SHELLM_MODEL"] == "process-model"
    assert seen["OPENAI_API_KEY"] == "process-key"


def test_identity_file_overrides_root_file_only_for_missing_inherited_values(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "headlong"
    root.mkdir()
    identity = _identity(root)
    (root / ".env").write_text(
        "SHELLM_MODEL=root-model\nOPENAI_API_KEY=root-key\nLLM_PROVIDER=openai\n"
    )
    (identity.path / ".env").write_text(
        "SHELLM_MODEL=identity-model\nOPENAI_API_KEY=identity-key\n"
    )
    monkeypatch.delenv("SHELLM_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    seen: dict[str, str] = {}

    def fake_run(*_args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess([], 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(model_gateway.subprocess, "run", fake_run)
    model_gateway.ModelGateway(root, identity).complete_text(
        "prompt", system="system", token_timeout=30, operation="env test"
    )

    assert seen["SHELLM_MODEL"] == "identity-model"
    assert seen["OPENAI_API_KEY"] == "identity-key"
    assert seen["LLM_PROVIDER"] == "openai"
