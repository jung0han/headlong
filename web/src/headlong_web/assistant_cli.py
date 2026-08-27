"""Public command boundary for HeadLong's Personal Assistant."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from headlong_web.assistant import (
    AssistantError,
    EvidenceLocator,
    PersonalAssistant,
    resolve_observer,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="headlong-assistant")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="HeadLong serve root")
    parser.add_argument("--identity", help="Observer Identity name or dashboard id")
    commands = parser.add_subparsers(dest="command", required=True)

    project = commands.add_parser("project", help="manage Registered Projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    add = project_commands.add_parser("add")
    add.add_argument("path", type=Path)
    add.add_argument("--name")
    project_commands.add_parser("list")
    remove = project_commands.add_parser("remove")
    remove.add_argument("selector")

    web_source = commands.add_parser("web-source", help="manage Registered Web Sources")
    web_commands = web_source.add_subparsers(dest="web_source_command", required=True)
    web_add = web_commands.add_parser("add")
    web_add.add_argument("url")
    web_add.add_argument("--name")
    web_commands.add_parser("list")
    web_remove = web_commands.add_parser("remove")
    web_remove.add_argument("selector")

    observe = commands.add_parser("observe-codex", help="observe eligible Codex Sessions once")
    _source_root_args(observe)

    commands.add_parser("observe-web", help="consider Registered Web Sources once")

    reference = commands.add_parser("reference", help="read saved References")
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    reference_commands.add_parser("list")
    reference_show = reference_commands.add_parser("show")
    reference_show.add_argument("source_id")
    reference_show.add_argument("revision_id")

    follow = commands.add_parser("follow-codex", help="collect active Codex records once")
    _source_root_args(follow)

    process = commands.add_parser(
        "process-codex", help="collect Codex records and run due session analysis once"
    )
    _source_root_args(process)

    evidence = commands.add_parser("resolve-evidence", help="resolve a v1 Evidence Locator")
    evidence.add_argument("locator")
    _source_root_args(evidence)
    return parser


def _source_root_args(parser: argparse.ArgumentParser) -> None:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser.add_argument("--sessions-root", type=Path, default=codex_home / "sessions")
    parser.add_argument(
        "--archived-sessions-root",
        type=Path,
        default=codex_home / "archived_sessions",
    )


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
        identity = resolve_observer(root, args.identity)
        assistant = PersonalAssistant(root, identity)
        if args.command == "project":
            if args.project_command == "add":
                result = assistant.add_project(args.path, args.name).to_dict()
            elif args.project_command == "remove":
                result = assistant.remove_project(args.selector).to_dict()
            else:
                result = {"projects": [project.to_dict() for project in assistant.projects()]}
        elif args.command == "web-source":
            if args.web_source_command == "add":
                result = assistant.add_web_source(args.url, args.name).to_dict()
            elif args.web_source_command == "remove":
                result = assistant.remove_web_source(args.selector).to_dict()
            else:
                result = {
                    "web_sources": [source.to_dict() for source in assistant.web_sources()]
                }
        elif args.command == "observe-codex":
            result = assistant.observe_codex_once(
                args.sessions_root, args.archived_sessions_root
            )
        elif args.command == "observe-web":
            result = assistant.observe_web_once()
        elif args.command == "reference":
            if args.reference_command == "list":
                result = {"references": assistant.references()}
            else:
                result = assistant.reference(args.source_id, args.revision_id)
                if result is None:
                    raise AssistantError("Reference revision not found")
        elif args.command == "follow-codex":
            result = assistant.follow_codex_once(
                args.sessions_root, args.archived_sessions_root
            )
        elif args.command == "process-codex":
            result = assistant.process_codex_once(
                args.sessions_root, args.archived_sessions_root
            )
        else:
            locator = EvidenceLocator.decode(args.locator)
            raw = assistant.resolve_evidence(
                locator, args.sessions_root, args.archived_sessions_root
            )
            result = {
                "locator": locator.to_dict(),
                "sha256": locator.sha256,
                "raw": raw.decode("utf-8"),
            }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except AssistantError as exc:
        print(f"headlong-assistant: error: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
