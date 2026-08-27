"""Scoped lexical retrieval for Personal Assistant responses.

The Activity Ledger and immutable Reference Store remain authoritative.  This
module only repairs the replaceable Active Memory projection, filters scope and
authority, and assembles a bounded response context.  Models never participate
in eligibility decisions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from headlong_web import active_memory, references
from headlong_web.knowledge import KnowledgeScope, KnowledgeScopeError

CONTEXT_SCHEMA = "headlong.assistant-response-context/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
MAX_MEMORIES = 6
MAX_REFERENCES = 4
MAX_REFERENCE_SNIPPET = 1_200
MAX_CONTEXT_CHARS = 12_000
_TOKEN_RE = re.compile(r"[\w]+(?:[-'][\w]+)*", re.UNICODE)


class RetrievalError(ValueError):
    """The scoped retrieval boundary could not produce trustworthy context."""


def ensure_active_projection(
    identity_dir: Path, events: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Return the canonical current projection, repairing missing/stale files.

    Freshness is checked against a fold of the Activity Ledger, rather than a
    second mutable version marker.  Repair changes projection files only.
    """
    rows = list(events)
    try:
        expected = active_memory.active_records(rows)
        needs_rebuild = not active_memory.projection_dir(identity_dir).is_dir()
        try:
            projected = active_memory.read_projection(identity_dir)
        except active_memory.ActiveMemoryError:
            projected = []
            needs_rebuild = True
        if needs_rebuild or projected != expected:
            projected = active_memory.rebuild_projection(identity_dir, rows)
        return projected
    except active_memory.ActiveMemoryError as exc:
        raise RetrievalError(str(exc)) from exc


def assemble_context(
    identity_dir: Path,
    *,
    identity_id: str,
    project_id: str,
    query: str,
    events: Iterable[dict[str, Any]],
    max_memories: int = MAX_MEMORIES,
    max_references: int = MAX_REFERENCES,
    max_context_chars: int = MAX_CONTEXT_CHARS,
) -> dict[str, Any]:
    """Assemble project+global Active Memory and globally saved References.

    Scope and active authority are resolved before the lexical scorer sees a
    memory. References are separate untrusted evidence and are never promoted
    to memory authority by their inclusion here.
    """
    if not isinstance(query, str) or not query.strip():
        raise RetrievalError("assistant response query is empty")
    if not isinstance(project_id, str) or not project_id:
        raise RetrievalError("assistant response requires a Registered Project")
    if max_memories < 0 or max_references < 0 or max_context_chars < 1:
        raise RetrievalError("assistant retrieval limits are invalid")

    ledger = list(events)
    projection = ensure_active_projection(identity_dir, ledger)
    try:
        # This eligibility boundary intentionally precedes _rank. Projection
        # validation also guarantees authority == active.
        eligible_memories = active_memory.select_scope(
            projection, project_id=project_id, include_global=True
        )
    except active_memory.ActiveMemoryError as exc:
        raise RetrievalError(str(exc)) from exc

    terms = _tokens(query)
    memories = _rank_memories(eligible_memories, terms, identity_id)[:max_memories]
    reference_rows = _reference_candidates(identity_dir, terms, project_id)[:max_references]
    memories, reference_rows = _fit_budget(
        memories, reference_rows, max_context_chars=max_context_chars
    )
    evidence = [
        {
            "kind": item["kind"],
            "locator": item["locator"],
            "source_evidence": item.get("source_evidence", []),
        }
        for item in [*memories, *reference_rows]
    ]
    return {
        "schema": CONTEXT_SCHEMA,
        "project_id": project_id,
        "query": query.strip(),
        "active_memories": memories,
        "references": reference_rows,
        "evidence": evidence,
    }


def resolve_context_evidence(
    identity_dir: Path,
    locator: dict[str, Any],
    events: Iterable[dict[str, Any]],
    *,
    identity_id: str,
) -> dict[str, Any]:
    """Resolve a returned context locator to its ledger event or revision."""
    if not isinstance(locator, dict) or locator.get("schema") != LOCATOR_SCHEMA:
        raise RetrievalError("unsupported response Evidence Locator")
    kind = locator.get("kind")
    if kind == "activity_ledger_event":
        if set(locator) != {
            "schema",
            "kind",
            "source_identity",
            "event_id",
            "sha256",
        }:
            raise RetrievalError("invalid Activity Ledger Evidence Locator")
        rows = list(events)
        if locator.get("source_identity") != identity_id:
            raise RetrievalError(
                "Activity Ledger Evidence Locator is for another identity"
            )
        matches = [
            event
            for event in rows
            if event.get("step_id") == locator.get("event_id")
            and event.get("event_id") == locator.get("event_id")
        ]
        public = [
            record
            for record in active_memory.active_records(rows)
            if record["event_id"] == locator.get("event_id")
        ]
        if (
            len(matches) != 1
            or len(public) != 1
            or _digest(public[0]) != locator.get("sha256")
        ):
            raise RetrievalError("Activity Ledger Evidence Locator does not resolve")
        return {"kind": kind, "event": matches[0]}
    if kind == "web_reference":
        expected = {
            "schema",
            "kind",
            "source_identity",
            "source_id",
            "revision_id",
            "sha256",
        }
        if set(locator) != expected or locator.get("sha256") != locator.get(
            "revision_id"
        ):
            raise RetrievalError("invalid Reference Evidence Locator")
        try:
            revision = references.read_reference(
                identity_dir,
                str(locator.get("source_id")),
                str(locator.get("revision_id")),
                include_text=True,
            )
        except references.ReferenceError as exc:
            raise RetrievalError(str(exc)) from exc
        if (
            revision is None
            or revision.get("source_url") != locator.get("source_identity")
            or revision.get("content_digest") != locator.get("sha256")
        ):
            raise RetrievalError("Reference Evidence Locator does not resolve")
        return {"kind": kind, "reference": revision}
    raise RetrievalError("unsupported response Evidence Locator kind")


def _rank_memories(
    records: Iterable[dict[str, Any]], terms: frozenset[str], identity_id: str
) -> list[dict[str, Any]]:
    ranked: list[tuple[tuple[int, int, int, str], dict[str, Any]]] = []
    rows = list(records)
    index = _LexicalIndex()
    for record in rows:
        text = " ".join(
            (
                record["memory_key"],
                record["memory_kind"],
                record["content"],
            )
        )
        index.add(record["event_id"], text)
    for record in rows:
        score = index.score(record["event_id"], terms)
        if score[0] == 0:
            continue
        locator = _ledger_locator(identity_id, record["event_id"], record)
        public = {
            "kind": "active_memory",
            "memory_key": record["memory_key"],
            "memory_kind": record["memory_kind"],
            "content": record["content"],
            "knowledge_scope": record["knowledge_scope"],
            "authority": "active",
            "locator": locator,
            "source_evidence": record["evidence_locators"],
        }
        ranked.append(((*score, record["event_id"]), public))
    ranked.sort(key=lambda item: (-item[0][0], -item[0][1], -item[0][2], item[0][3]))
    return [item for _score_value, item in ranked]


def _reference_candidates(
    identity_dir: Path, terms: frozenset[str], project_id: str
) -> list[dict[str, Any]]:
    try:
        metadata_rows = references.list_references(identity_dir)
    except references.ReferenceError as exc:
        raise RetrievalError(str(exc)) from exc
    candidates: list[tuple[dict[str, Any], str]] = []
    index = _LexicalIndex()
    for metadata in metadata_rows:
        try:
            scope = KnowledgeScope.parse(
                metadata.get("knowledge_scope"), legacy_global=True
            )
        except KnowledgeScopeError as exc:
            raise RetrievalError(str(exc)) from exc
        # Eligibility must precede body reads, indexing, scoring, and model use.
        if not scope.eligible_for(project_id):
            continue
        try:
            revision = references.read_reference(
                identity_dir,
                metadata["source_id"],
                metadata["revision_id"],
                include_text=True,
            )
        except references.ReferenceError as exc:
            raise RetrievalError(str(exc)) from exc
        if revision is None:
            continue
        search_text = " ".join(
            (
                revision["title"],
                revision["summary"],
                revision["source_url"],
                revision["text"],
            )
        )
        candidate_id = f"{revision['source_id']}:{revision['revision_id']}"
        index.add(candidate_id, search_text)
        candidates.append((revision, candidate_id))
    ranked: list[tuple[tuple[int, int, int, str, str], dict[str, Any]]] = []
    for revision, candidate_id in candidates:
        score = index.score(candidate_id, terms)
        if score[0] == 0:
            continue
        public = {
            "kind": "reference",
            "title": revision["title"],
            "summary": revision["summary"],
            "source_url": revision["source_url"],
            "revision_id": revision["revision_id"],
            "knowledge_scope": scope.to_dict(),
            "trust": "untrusted_reference",
            "snippet": _snippet(revision["text"], terms),
            "locator": revision["evidence_locator"],
        }
        ranked.append(
            (
                (
                    *score,
                    revision["source_id"],
                    revision["revision_id"],
                ),
                public,
            )
        )
    ranked.sort(
        key=lambda item: (
            -item[0][0],
            -item[0][1],
            -item[0][2],
            item[0][3],
            item[0][4],
        )
    )
    return [item for _score_value, item in ranked]


def _fit_budget(
    memories: list[dict[str, Any]],
    reference_rows: list[dict[str, Any]],
    *,
    max_context_chars: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    used = 0
    kept_memories: list[dict[str, Any]] = []
    kept_references: list[dict[str, Any]] = []
    for target, rows in (
        (kept_memories, memories),
        (kept_references, reference_rows),
    ):
        for row in rows:
            size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            if used + size > max_context_chars:
                continue
            target.append(row)
            used += size
    return kept_memories, kept_references


def _tokens(value: str) -> frozenset[str]:
    return frozenset(token.casefold() for token in _TOKEN_RE.findall(value))


class _LexicalIndex:
    """Small in-memory inverted index for the intentionally file-backed v1."""

    def __init__(self) -> None:
        self._postings: dict[str, dict[str, int]] = {}

    def add(self, document_id: str, value: str) -> None:
        for token in _TOKEN_RE.findall(value.casefold()):
            posting = self._postings.setdefault(token, {})
            posting[document_id] = posting.get(document_id, 0) + 1

    def score(
        self, document_id: str, terms: frozenset[str]
    ) -> tuple[int, int, int]:
        counts = [self._postings.get(term, {}).get(document_id, 0) for term in terms]
        matched = sum(1 for count in counts if count)
        occurrences = sum(counts)
        exact_terms = int(bool(terms) and matched == len(terms))
        return matched, occurrences, exact_terms


def _snippet(text: str, terms: frozenset[str]) -> str:
    folded = text.casefold()
    positions = [folded.find(term) for term in terms if folded.find(term) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - MAX_REFERENCE_SNIPPET // 3)
    end = min(len(text), start + MAX_REFERENCE_SNIPPET)
    snippet = text[start:end].strip()
    if start:
        snippet = "…" + snippet
    if end < len(text):
        snippet += "…"
    return snippet


def _ledger_locator(
    identity_id: str, event_id: str, projection_record: dict[str, Any]
) -> dict[str, str]:
    # The projection intentionally omits ledger-only fields and timestamps.
    # Its digest still detects a changed/stale locator, while resolution below
    # recomputes the same public Active Memory record from the ledger event.
    return {
        "schema": LOCATOR_SCHEMA,
        "kind": "activity_ledger_event",
        "source_identity": identity_id,
        "event_id": event_id,
        "sha256": _digest(projection_record),
    }


def _digest(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()
