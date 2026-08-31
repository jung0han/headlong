"""Public command boundary for HeadLong's Personal Assistant."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from headlong_web import archive_execution, assistant_runtime, web_exploration
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
    web_add.add_argument(
        "--kind",
        choices=("url", "rss", "documentation", "hacker_news"),
        default="url",
    )
    web_commands.add_parser("list")
    web_commands.add_parser("health")
    web_remove = web_commands.add_parser("remove")
    web_remove.add_argument("selector")

    observe = commands.add_parser("observe-codex", help="observe eligible Codex Sessions once")
    _source_root_args(observe)

    commands.add_parser("observe-web", help="consider Registered Web Sources once")

    explore = commands.add_parser(
        "explore-web", help="run one memory-triggered bounded public exploration"
    )
    explore.add_argument("memory_selector")
    explore.add_argument(
        "--trigger-kind", choices=("interest", "open_loop"), default="interest"
    )
    explore.add_argument("--seed-url", action="append", default=[])
    explore.add_argument("--max-pages", type=int, default=8)
    explore.add_argument("--max-depth", type=int, default=2)
    explore.add_argument("--max-elapsed-seconds", type=float, default=60.0)
    explore.add_argument("--max-stored-bytes", type=int, default=2_000_000)

    reference = commands.add_parser("reference", help="read saved References")
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    reference_commands.add_parser("list")
    reference_show = reference_commands.add_parser("show")
    reference_show.add_argument("source_id")
    reference_show.add_argument("revision_id")

    memory = commands.add_parser("memory", help="review and project Active Memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True)
    candidates = memory_commands.add_parser("candidates")
    _memory_scope_args(candidates)
    memory_list = memory_commands.add_parser("list")
    _memory_scope_args(memory_list)
    remember = memory_commands.add_parser("remember")
    remember.add_argument("content")
    _memory_authority_args(remember)
    accept = memory_commands.add_parser("accept")
    accept.add_argument("candidate_event_id")
    _memory_authority_args(accept)
    memory_commands.add_parser("rebuild")

    native_memory = commands.add_parser(
        "native-memory", help="recover native HeadLong Memory"
    )
    native_memory_commands = native_memory.add_subparsers(
        dest="native_memory_command", required=True
    )
    native_memory_commands.add_parser("rebuild")
    restore_native_memory = native_memory_commands.add_parser("restore")
    restore_native_memory.add_argument("memory_id")

    memory_failure = commands.add_parser(
        "memory-failure", help="record and inspect observed Memory Failures"
    )
    memory_failure_commands = memory_failure.add_subparsers(
        dest="memory_failure_command", required=True
    )
    memory_failure_commands.add_parser("list")
    memory_failure_commands.add_parser("health")
    memory_failure_commands.add_parser("quality")
    report_failure = memory_failure_commands.add_parser("report")
    report_failure.add_argument("memory_event_id")
    report_failure.add_argument(
        "--classification",
        choices=(
            "wrong_scope",
            "evidence_contradicting",
            "behavior_affecting",
            "duplicate",
            "wording_defect",
        ),
        required=True,
    )
    report_failure.add_argument("--description", required=True)
    downstream = report_failure.add_mutually_exclusive_group()
    downstream.add_argument(
        "--downstream-event-id", help="downstream Proposal event id"
    )
    downstream.add_argument(
        "--downstream-step-id", help="downstream native action step id"
    )

    archive_candidate = commands.add_parser(
        "archive-candidate", help="inspect and review Codex Archive Candidates"
    )
    archive_commands = archive_candidate.add_subparsers(
        dest="archive_candidate_command", required=True
    )
    archive_commands.add_parser("list")
    archive_show = archive_commands.add_parser("show")
    archive_show.add_argument("candidate_id")
    archive_review = archive_commands.add_parser("review")
    archive_review.add_argument("candidate_ids", nargs="+")
    archive_review.add_argument(
        "--state",
        choices=("pending", "accepted", "rejected", "dismissed"),
        required=True,
    )

    archive_session = commands.add_parser(
        "archive-session", help="execute authorized Codex archive recovery controls"
    )
    archive_session_commands = archive_session.add_subparsers(
        dest="archive_session_command", required=True
    )
    for operation in ("archive", "unarchive"):
        command = archive_session_commands.add_parser(operation)
        command.add_argument("session_id")
    retry = archive_session_commands.add_parser("retry-candidate")
    retry.add_argument("candidate_id")

    context = commands.add_parser(
        "context", help="assemble scoped Active Memory and Reference context"
    )
    context.add_argument("query")
    context.add_argument("--project")

    respond = commands.add_parser(
        "respond", help="answer with scoped Active Memory and Reference evidence"
    )
    respond.add_argument("query")
    respond.add_argument("--project")

    response_evidence = commands.add_parser(
        "resolve-response-evidence",
        help="resolve a response Evidence Locator to its ledger event or Reference",
    )
    response_evidence.add_argument("locator", help="Evidence Locator JSON object")

    follow = commands.add_parser("follow-codex", help="collect active Codex records once")
    _source_root_args(follow)

    process = commands.add_parser(
        "process-codex", help="collect Codex records and run due session analysis once"
    )
    _source_root_args(process)

    run_codex = commands.add_parser(
        "run-codex-bridge", help="continuously collect and analyze Codex Sessions"
    )
    _source_root_args(run_codex)
    run_codex.add_argument(
        "--interval-seconds",
        type=float,
        default=os.environ.get(
            "HEADLONG_CODEX_BRIDGE_INTERVAL_SECONDS",
            str(assistant_runtime.DEFAULT_CODEX_INTERVAL_SECONDS),
        ),
    )

    run_web = commands.add_parser(
        "run-web-bridge", help="continuously refresh Registered Web Sources"
    )
    run_web.add_argument(
        "--interval-seconds",
        type=float,
        default=os.environ.get(
            "HEADLONG_WEB_BRIDGE_INTERVAL_SECONDS",
            str(assistant_runtime.DEFAULT_WEB_INTERVAL_SECONDS),
        ),
    )

    commands.add_parser("status", help="show bounded Personal Assistant health")

    localize = commands.add_parser(
        "localize-pending", help="translate unresolved review items"
    )
    localize.add_argument("--language", choices=("en", "ko"), default="ko")

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


def _memory_scope_args(parser: argparse.ArgumentParser) -> None:
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--project")
    scope.add_argument("--global", dest="global_scope", action="store_true")


def _memory_authority_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--kind", choices=("decision", "preference", "constraint"), required=True
    )
    parser.add_argument("--key", required=True)
    _memory_scope_args(parser)


def run(
    argv: list[str] | None = None,
    *,
    archive_adapter: archive_execution.ArchiveAdapter | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = args.root.resolve()
        identity = resolve_observer(root, args.identity)
        assistant = PersonalAssistant(root, identity, archive_adapter=archive_adapter)
        if args.command == "project":
            if args.project_command == "add":
                result = assistant.add_project(args.path, args.name).to_dict()
            elif args.project_command == "remove":
                result = assistant.remove_project(args.selector).to_dict()
            else:
                result = {"projects": [project.to_dict() for project in assistant.projects()]}
        elif args.command == "web-source":
            if args.web_source_command == "add":
                result = assistant.add_web_source(
                    args.url, args.name, args.kind
                ).to_dict()
            elif args.web_source_command == "remove":
                result = assistant.remove_web_source(args.selector).to_dict()
            elif args.web_source_command == "health":
                result = {"web_sources": assistant.web_source_health()}
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
        elif args.command == "explore-web":
            result = assistant.explore_web_once(
                args.memory_selector,
                trigger_kind=args.trigger_kind,
                seed_urls=tuple(args.seed_url),
                limits=web_exploration.ExplorationLimits(
                    max_pages=args.max_pages,
                    max_depth=args.max_depth,
                    max_elapsed_seconds=args.max_elapsed_seconds,
                    max_stored_bytes=args.max_stored_bytes,
                ),
            )
        elif args.command == "reference":
            if args.reference_command == "list":
                result = {"references": assistant.references()}
            else:
                result = assistant.reference(args.source_id, args.revision_id)
                if result is None:
                    raise AssistantError("Reference revision not found")
        elif args.command == "memory":
            if args.memory_command == "rebuild":
                result = assistant.rebuild_active_memory()
            elif args.memory_command == "candidates":
                project = args.project
                if not args.global_scope and project is None:
                    project = str(Path.cwd())
                result = {
                    "memory_candidates": assistant.memory_candidates(
                        project, global_only=args.global_scope
                    )
                }
            elif args.memory_command == "list":
                project = args.project
                if not args.global_scope and project is None:
                    project = str(Path.cwd())
                result = {
                    "active_memories": assistant.active_memories(
                        project, global_only=args.global_scope
                    )
                }
            elif args.memory_command == "remember":
                result = assistant.remember_memory(
                    args.content,
                    memory_kind=args.kind,
                    memory_key=args.key,
                    project_selector=args.project,
                    global_scope=args.global_scope,
                    current_path=Path.cwd(),
                )
            else:
                result = assistant.accept_memory_candidate(
                    args.candidate_event_id,
                    memory_kind=args.kind,
                    memory_key=args.key,
                    project_selector=args.project,
                    global_scope=args.global_scope,
                )
        elif args.command == "native-memory":
            if args.native_memory_command == "rebuild":
                result = assistant.rebuild_native_memory()
            else:
                result = assistant.restore_native_memory(args.memory_id)
        elif args.command == "memory-failure":
            if args.memory_failure_command == "list":
                result = {"memory_failures": assistant.memory_failures()}
            elif args.memory_failure_command == "health":
                result = assistant.memory_failure_health()
            elif args.memory_failure_command == "quality":
                result = {
                    "memory_quality_observations": (
                        assistant.memory_quality_observations()
                    )
                }
            else:
                result = assistant.report_memory_issue(
                    args.memory_event_id,
                    args.classification,
                    args.description,
                    downstream_event_id=args.downstream_event_id,
                    downstream_step_id=args.downstream_step_id,
                )
        elif args.command == "archive-candidate":
            if args.archive_candidate_command == "list":
                result = {"archive_candidates": assistant.archive_candidates()}
            elif args.archive_candidate_command == "show":
                result = assistant.archive_candidate(args.candidate_id)
                if result is None:
                    raise AssistantError("Archive Candidate not found")
            else:
                result = assistant.review_archive_candidates(
                    args.candidate_ids, args.state
                )
        elif args.command == "archive-session":
            if args.archive_session_command == "archive":
                result = assistant.archive_codex_session(args.session_id)
            elif args.archive_session_command == "unarchive":
                result = assistant.unarchive_codex_session(args.session_id)
            else:
                result = assistant.retry_archive_candidate(args.candidate_id)
        elif args.command == "context":
            result = assistant.response_context(
                args.query, args.project, current_path=Path.cwd()
            )
        elif args.command == "respond":
            result = assistant.respond(
                args.query, args.project, current_path=Path.cwd()
            )
        elif args.command == "resolve-response-evidence":
            locator = json.loads(args.locator)
            if not isinstance(locator, dict):
                raise AssistantError("response Evidence Locator must be an object")
            result = assistant.resolve_response_evidence(locator)
        elif args.command == "follow-codex":
            result = assistant.follow_codex_once(
                args.sessions_root, args.archived_sessions_root
            )
        elif args.command == "process-codex":
            result = assistant.process_codex_once(
                args.sessions_root, args.archived_sessions_root
            )
        elif args.command == "run-codex-bridge":
            assistant_runtime.run_bridge(
                assistant,
                "codex",
                interval_seconds=args.interval_seconds,
                active_root=args.sessions_root,
                archived_root=args.archived_sessions_root,
            )
            result = {"status": "stopped", "bridge": "codex"}
        elif args.command == "run-web-bridge":
            assistant_runtime.run_bridge(
                assistant,
                "web",
                interval_seconds=args.interval_seconds,
            )
            result = {"status": "stopped", "bridge": "web"}
        elif args.command == "status":
            result = assistant_runtime.public_health(root, identity)
        elif args.command == "localize-pending":
            result = assistant.localize_pending(args.language)
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
    except (AssistantError, ValueError) as exc:
        print(f"headlong-assistant: error: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
