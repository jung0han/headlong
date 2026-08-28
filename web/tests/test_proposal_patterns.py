"""DONGWOO-912 evidence-threshold and Observer proposal contracts."""

from __future__ import annotations

import json
from pathlib import Path

from headlong_web.codex_bridge import discover_sources
from headlong_web.proposals import (
    build_inbox,
    direct_proposal_events,
    evidence_update_event,
    inferred_pattern_proposal_events,
    review_event,
)

PROJECT_SCOPE = {"kind": "project", "project_id": "project-headlong"}
WORK_DIRECT_EVIDENCE = (
    "user_correction",
    "test_failure",
    "tool_failure",
    "reviewer_finding",
)


def _uuid(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


def _locator(session: str, number: int = 1) -> dict:
    return {
        "schema": "headlong.evidence-locator/v1",
        "kind": "codex_event",
        "source_identity": session,
        "source_root": "archived",
        "relative_path": f"{session}.jsonl",
        "line": number,
        "byte_offset": number * 10,
        "byte_length": 10,
        "sha256": f"{number:064x}",
        "host": "test-host",
    }


def _analysis(
    number: int,
    *,
    task_root: int | None = None,
    proposal_type: str = "work",
    kind: str = "inferred_pattern",
    content: str = "The same retry is repeatedly implemented by hand.",
    repeated: bool = False,
) -> dict:
    session = _uuid(number)
    locators = [_locator(session, 1)]
    if repeated:
        locators.append(_locator(session, 2))
    return {
        "type": "observation",
        "source": "personal_assistant",
        "analysis_state": "final",
        "event_id": _uuid(number + 100),
        "source_identity": session,
        "task_root_id": _uuid(task_root if task_root is not None else number),
        "knowledge_scope": PROJECT_SCOPE,
        "supersedes_event_ids": [],
        "improvement_signals": [
            {
                "kind": kind,
                "proposal_type": proposal_type,
                "content": content,
                "evidence_locators": locators,
            }
        ],
    }


def test_pattern_requires_three_distinct_task_roots_and_keeps_all_evidence():
    two_roots = [
        _analysis(1, task_root=50, repeated=True),
        _analysis(2, task_root=50),  # A subagent of the same Codex task.
        _analysis(3, task_root=51),
    ]
    assert inferred_pattern_proposal_events(two_roots) == []

    exactly_three = [*two_roots, _analysis(4, task_root=52)]
    events = inferred_pattern_proposal_events(exactly_three)
    assert len(events) == 1
    proposal = events[0]
    assert proposal["type"] == "work-improvement-proposal"
    assert proposal["proposal_label"] == "Work Improvement Proposal"
    assert proposal["task_root_ids"] == [_uuid(50), _uuid(51), _uuid(52)]
    assert proposal["source_identities"] == [_uuid(1), _uuid(2), _uuid(3), _uuid(4)]
    assert len(proposal["evidence_locators"]) == 5
    assert proposal["execution_authority"] == "none"
    assert len(build_inbox(events)) == 1


def test_pattern_is_one_proposal_and_append_only_update_enriches_evidence():
    first = inferred_pattern_proposal_events([_analysis(1), _analysis(2), _analysis(3)])[0]
    desired = inferred_pattern_proposal_events(
        [_analysis(1), _analysis(2), _analysis(3), _analysis(4)]
    )[0]
    assert desired["event_id"] == first["event_id"]

    pending = build_inbox([first])[0]
    accepted = review_event(pending, "accepted", event_id=_uuid(900))
    update = evidence_update_event(desired, pending)
    assert update is not None
    inbox = build_inbox([first, accepted, update])
    assert len(inbox) == 1
    assert inbox[0]["review_state"] == "accepted"
    assert inbox[0]["source_identities"] == [_uuid(1), _uuid(2), _uuid(3), _uuid(4)]
    assert inbox[0]["execution_authority"] == "none"
    assert evidence_update_event(desired, inbox[0]) is None


def test_work_and_observer_proposals_have_distinct_public_types_and_labels():
    work = inferred_pattern_proposal_events(
        [_analysis(1), _analysis(2), _analysis(3)]
    )[0]
    observer = inferred_pattern_proposal_events(
        [
            _analysis(1, proposal_type="observer"),
            _analysis(2, proposal_type="observer"),
            _analysis(3, proposal_type="observer"),
        ]
    )[0]
    assert work["event_id"] != observer["event_id"]
    assert (work["type"], work["proposal_label"]) == (
        "work-improvement-proposal",
        "Work Improvement Proposal",
    )
    assert (observer["type"], observer["proposal_label"]) == (
        "observer-improvement-proposal",
        "Observer Improvement Proposal",
    )


def test_each_direct_work_evidence_kind_qualifies():
    qualifying = []
    for offset, kind in enumerate(WORK_DIRECT_EVIDENCE, start=1):
        qualifying.extend(
            direct_proposal_events(
                _analysis(
                    offset,
                    kind=kind,
                    content=f"Concrete work evidence: {kind}.",
                )
            )
        )
    assert {item["evidence_kind"] for item in qualifying} == set(
        WORK_DIRECT_EVIDENCE
    )
    unsupported = [
        _analysis(10, kind="self_evaluation"),
        _analysis(11, kind="design_preference"),
        _analysis(12, kind="open_loop"),
    ]
    assert all(direct_proposal_events(analysis) == [] for analysis in unsupported)
    assert inferred_pattern_proposal_events(unsupported) == []


def test_concrete_observer_evidence_qualifies_but_opinion_does_not():
    qualifying = []
    for offset, kind in enumerate(
        ("observer_failure", "observer_regression", "user_correction"), start=1
    ):
        qualifying.extend(
            direct_proposal_events(
                _analysis(
                    offset,
                    proposal_type="observer",
                    kind=kind,
                    content=f"Concrete Observer evidence: {kind}.",
                )
            )
        )
    assert len(qualifying) == 3
    assert {event["proposal_type"] for event in qualifying} == {"observer"}
    assert {event["execution_authority"] for event in qualifying} == {"none"}

    unsupported = [
        _analysis(10, proposal_type="observer", kind="self_evaluation"),
        _analysis(11, proposal_type="observer", kind="design_preference"),
        _analysis(12, proposal_type="observer", kind="open_loop"),
    ]
    assert all(direct_proposal_events(analysis) == [] for analysis in unsupported)
    assert inferred_pattern_proposal_events(unsupported) == []


def test_codex_parent_chain_resolves_one_validated_task_root(tmp_path: Path):
    active = tmp_path / "sessions"
    archived = tmp_path / "archived"
    active.mkdir()
    archived.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    root, child, nested = _uuid(1), _uuid(2), _uuid(3)

    def write(session: str, parent: str | None) -> None:
        payload = {"id": session, "cwd": str(cwd)}
        if parent is not None:
            payload.update({"parent_thread_id": parent, "forked_from_id": parent})
        (active / f"{session}.jsonl").write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n"
        )

    write(root, None)
    write(child, root)
    write(nested, child)
    sources, errors = discover_sources({"active": active, "archived": archived})
    assert errors == []
    assert {source.task_root_id for source in sources} == {root}
