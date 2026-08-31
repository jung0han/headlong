"""Frontend build command regression coverage."""

from pathlib import Path

from headlong_web import cli


def test_bun_build_forces_bun_runtime(
    tmp_path: Path, monkeypatch,
) -> None:
    viewer = tmp_path / "viewer"
    static = tmp_path / "static"
    viewer.mkdir()
    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        calls.append(command)
        assert cwd == viewer
        assert check is True
        if command[-2:] == ["run", "build"]:
            build = viewer / "build" / "client"
            build.mkdir(parents=True)
            (build / "index.html").write_text("ready")

    monkeypatch.setattr(cli, "VIEWER_DIR", viewer)
    monkeypatch.setattr(cli, "STATIC_DIR", static)
    monkeypatch.setattr(cli, "_js_runtime", lambda: ["bun"])
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    cli._build_frontend()

    assert calls == [
        ["bun", "install"],
        ["bun", "--bun", "run", "build"],
    ]
    assert (static / "index.html").read_text() == "ready"
