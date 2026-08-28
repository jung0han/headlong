"""Localized pending-item projections preserve the append-only source of truth."""

from __future__ import annotations

import json
from copy import deepcopy

from headlong_web import localized_projection
from headlong_web.assistant import PersonalAssistant, resolve_observer


def test_korean_projection_is_digest_bound_and_preserves_authority(tmp_path):
    identity = tmp_path / ".identities" / "observer"
    proposal = {
        "proposal_id": "f3d12370-99f3-5d36-8c83-790203259bfb",
        "proposal_type": "work",
        "title": "Work Improvement Proposal from reviewer finding",
        "content": "Add focused tests for the shared MySQL fixture.",
        "review_state": "pending",
        "authority": "candidate",
        "execution_authority": "none",
        "evidence_locators": [{"source_identity": "session-1"}],
    }
    original = deepcopy(proposal)

    localized_projection.write_translations(
        identity,
        "ko",
        [
            localized_projection.translation_record(
                "proposal",
                proposal,
                title="리뷰 지적에 따른 작업 개선 제안",
                content="공유 MySQL 픽스처에 대한 집중 테스트를 추가합니다.",
            )
        ],
    )

    [translated] = localized_projection.localize_items(
        identity, "ko", "proposal", [proposal]
    )
    assert translated["title"] == "리뷰 지적에 따른 작업 개선 제안"
    assert translated["content"] == "공유 MySQL 픽스처에 대한 집중 테스트를 추가합니다."
    assert translated["proposal_id"] == proposal["proposal_id"]
    assert translated["review_state"] == "pending"
    assert translated["authority"] == "candidate"
    assert translated["execution_authority"] == "none"
    assert translated["evidence_locators"] == proposal["evidence_locators"]
    assert proposal == original

    changed = {**proposal, "content": "A newer source statement."}
    [stale] = localized_projection.localize_items(
        identity, "ko", "proposal", [changed]
    )
    assert stale["content"] == "A newer source statement."


def test_korean_projection_localizes_archive_rationale_and_default_labels(tmp_path):
    identity = tmp_path / ".identities" / "observer"
    archive = {
        "candidate_id": "f3d12370-99f3-5d36-8c83-790203259bfb",
        "completion_rationale": "The requested implementation is complete.",
        "review_state": "pending",
        "archive_authority": "none",
    }

    localized_projection.write_translations(
        identity,
        "ko",
        [
            localized_projection.translation_record(
                "archive",
                archive,
                content="요청한 구현이 완료되었습니다.",
            )
        ],
    )

    [translated] = localized_projection.localize_items(
        identity, "ko", "archive", [archive]
    )
    assert translated["title"] == "Codex 세션 보관 후보"
    assert translated["content"] == "요청한 구현이 완료되었습니다."
    assert translated["completion_rationale"] == "요청한 구현이 완료되었습니다."


def test_assistant_translates_each_unresolved_kind_and_reuses_projection(
    tmp_path, monkeypatch
):
    root = tmp_path / "headlong"
    identity = root / ".identities" / "observer"
    trajectory = identity / "trajectories" / "root" / "trajectory.jsonl"
    trajectory.parent.mkdir(parents=True)
    (identity / "info.txt").write_text(
        "name=observer\ncreated=2026-08-28T00:00:00Z\nroot_trajectory=root-id\n"
    )
    trajectory.write_text(
        json.dumps({"type": "trajectory", "step_id": "root-id", "ts": "t0"})
        + "\n"
    )
    assistant = PersonalAssistant(root, resolve_observer(root, "observer"))
    proposal = {
        "proposal_id": "proposal-1",
        "proposal_type": "work",
        "title": "Work Improvement Proposal from tool failure",
        "content": "Prevent this tool failure from recurring.",
        "review_state": "pending",
        "evidence_kind": "tool_failure",
    }
    archive = {
        "candidate_id": "archive-1",
        "completion_rationale": "The requested work is complete.",
        "review_state": "pending",
    }
    memory_event = {
        "type": "memory-candidate",
        "event_id": "memory-1",
        "content": "The user prefers focused regression tests.",
        "knowledge_scope": {"kind": "global"},
        "evidence_kind": "model_inference",
        "verification": "observed",
        "authority": "candidate",
        "evidence_locators": [],
        "causal_event_ids": [],
        "supersedes_event_ids": [],
        "source_kind": "codex_session",
        "source_identity": "session-1",
    }
    monkeypatch.setattr(assistant._governance, "proposals", lambda: [proposal])
    monkeypatch.setattr(
        assistant._governance, "archive_candidates", lambda: [archive]
    )
    monkeypatch.setattr(assistant, "_ledger_events", lambda: [memory_event])

    class FakeGateway:
        calls = 0

        def complete_structured(self, prompt, *, schema, **_kwargs):
            self.calls += 1
            targets = json.loads(prompt)["items"]
            return schema.validate(
                {
                    "items": [
                        {
                            "id": target["id"],
                            "title": "한국어 제목" if target["title"] else "",
                            "content": f"한국어 번역: {target['content']}",
                        }
                        for target in targets
                    ]
                }
            )

    gateway = FakeGateway()
    assistant._model = gateway
    monkeypatch.setenv("HEADLONG_ASSISTANT_LANGUAGE", "ko")

    result = assistant.localize_pending("ko")
    assert result["pending"] == {"proposal": 1, "archive": 1, "memory": 1}
    assert result["translated"] == {"proposal": 1, "archive": 1, "memory": 1}
    assert gateway.calls == 3
    assert assistant.proposals()[0]["content"].startswith("한국어 번역:")
    assert assistant.archive_candidates()[0]["completion_rationale"].startswith(
        "한국어 번역:"
    )
    assert assistant.memory_candidates()[0]["content"].startswith("한국어 번역:")

    replay = assistant.localize_pending("ko")
    assert replay["translated"] == {"proposal": 0, "archive": 0, "memory": 0}
    assert replay["already_localized"] == {
        "proposal": 1,
        "archive": 1,
        "memory": 1,
    }
    assert gateway.calls == 3
