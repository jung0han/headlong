"""Priority scheduler for current Codex work and Historical Backfill.

The bridge owns byte-safe collection and analysis owns lifecycle semantics.  This
module owns only admission and ordering, keeping the three-lane policy out of the
Personal Assistant application boundary.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from headlong_web import codex_analysis
from headlong_web.codex_bridge import CodexSource, discover_sources, source_has_delta

if TYPE_CHECKING:
    from headlong_web.assistant import PersonalAssistant, RegisteredProject


STATE_SCHEMA = "headlong.codex-scheduler-state/v1"
HEALTH_SCHEMA = "headlong.codex-scheduler-health/v1"
DEFAULT_CAPACITY = 1
COMPATIBILITY_BATCH_CAPACITY = 100
MAX_CAPACITY = 10_000
LANES = (
    "active_collection",
    "newly_eligible_analysis",
    "historical_backfill",
)


@dataclass(frozen=True)
class _Work:
    active: tuple[CodexSource, ...]
    newly_eligible: tuple[CodexSource, ...]
    historical: tuple[CodexSource, ...]


class CodexScheduler:
    """Run one bounded, durable three-lane Codex scheduling cycle."""

    def __init__(
        self,
        assistant: PersonalAssistant,
        active_root: Path,
        archived_root: Path,
        *,
        capacity: int | None = None,
    ) -> None:
        self.assistant = assistant
        self.active_root = active_root.resolve()
        self.archived_root = archived_root.resolve()
        self.capacity = _capacity(capacity)
        self.state_path = assistant.state_dir / "scheduling" / "codex.json"
        self.health_path = (
            assistant.state_dir / "source-health" / "codex-scheduling.json"
        )

    def run_once(self) -> dict[str, Any]:
        now = codex_analysis.format_time(self.assistant._now())
        state, bootstrapping = self._read_state()
        sources, discovery_errors = self._eligible_sources()
        known_before = set(state["sources"])
        work = self._classify(sources, known_before, bootstrapping)
        selected = self._select(work)

        collection = _empty_collection()
        analysis = _empty_analysis()
        lane_results: dict[str, dict[str, Any]] = {
            lane: {"attempted": 0, "progressed": 0, "failures": 0, "backlog": 0}
            for lane in LANES
        }

        active_order = tuple(source.id for source in selected.active)
        active_ids = set(active_order)
        if active_ids:
            lane_results["active_collection"]["attempted"] = len(active_ids)
            before = self._cursor_offsets(active_ids)
            result = self.assistant._follow_codex_selected(
                self.active_root, self.archived_root, active_order
            )
            _merge(collection, result)
            # Establish or refresh the inactivity state in the same active-lane
            # admission. This is a cheap eligibility check; due model work is
            # still admitted only through the newly-eligible lane.
            checked = self.assistant._analyze_codex_selected(
                self.active_root, self.archived_root, active_order
            )
            _merge(analysis, checked)
            lane_results["active_collection"]["progressed"] = self._cursor_progress(
                active_ids, before
            )
            lane_results["active_collection"]["failures"] = len(result["errors"])

        newly_order = tuple(source.id for source in selected.newly_eligible)
        newly_ids = set(newly_order)
        if newly_ids:
            lane_results["newly_eligible_analysis"]["attempted"] = len(newly_ids)
            progressed, failures = self._collect_and_analyze(
                newly_order, collection, analysis
            )
            lane_results["newly_eligible_analysis"]["progressed"] = progressed
            lane_results["newly_eligible_analysis"]["failures"] = failures

        historical_order = tuple(source.id for source in selected.historical)
        historical_ids = set(historical_order)
        if historical_ids:
            lane_results["historical_backfill"]["attempted"] = len(historical_ids)
            progressed, failures = self._collect_and_analyze(
                historical_order, collection, analysis
            )
            lane_results["historical_backfill"]["progressed"] = progressed
            lane_results["historical_backfill"]["failures"] = failures

        if discovery_errors:
            collection["errors"].extend(discovery_errors)
            collection["status"] = "degraded"

        # Preserve the public lifecycle summary without spending capacity or
        # invoking a model for already-settled revisions.
        selected_ids = active_ids | newly_ids | historical_ids
        analysis["duplicate"] += self._settled_count(
            source for source in sources if source.id not in selected_ids
        )

        refreshed, refreshed_errors = self._eligible_sources()
        refreshed_work = self._classify(
            refreshed, {source.id for source in sources}, False
        )
        lane_results["active_collection"]["backlog"] = len(refreshed_work.active)
        lane_results["newly_eligible_analysis"]["backlog"] = len(
            refreshed_work.newly_eligible
        )
        lane_results["historical_backfill"]["backlog"] = len(
            refreshed_work.historical
        )

        previous_health = _read_object(self.health_path)
        for lane in LANES:
            previous_lane = previous_health.get(lane)
            previous_lane = previous_lane if isinstance(previous_lane, dict) else {}
            lane_results[lane]["last_progress_at"] = (
                now
                if lane_results[lane]["progressed"]
                else previous_lane.get("last_progress_at")
            )
        all_errors = collection["errors"] + analysis["errors"] + refreshed_errors
        health: dict[str, Any] = {
            "schema": HEALTH_SCHEMA,
            "status": "degraded" if all_errors else "ok",
            "updated_at": now,
            "last_progress_at": (
                now
                if any(lane_results[lane]["progressed"] for lane in LANES)
                else previous_health.get("last_progress_at")
            ),
            "backlog_size": sum(lane_results[lane]["backlog"] for lane in LANES),
            "failures": sum(lane_results[lane]["failures"] for lane in LANES),
            **lane_results,
        }
        self.assistant._write_state_json(self.health_path, health)
        self._write_state(state, refreshed, now)
        return {
            "collection": collection,
            "analysis": analysis,
            "lanes": lane_results,
            "status": health["status"],
        }

    def _collect_and_analyze(
        self,
        session_order: tuple[str, ...],
        collection: dict[str, Any],
        analysis: dict[str, Any],
    ) -> tuple[int, int]:
        session_ids = set(session_order)
        cursor_before = self._cursor_offsets(session_ids)
        analysis_before = self._analysis_markers(session_ids)
        collected = self.assistant._follow_codex_selected(
            self.active_root, self.archived_root, session_order
        )
        analyzed = self.assistant._analyze_codex_selected(
            self.active_root, self.archived_root, session_order
        )
        _merge(collection, collected)
        _merge(analysis, analyzed)
        progressed_ids = self._cursor_progress_ids(session_ids, cursor_before)
        for session_id in session_ids:
            if self._analysis_marker(session_id) != analysis_before[session_id]:
                progressed_ids.add(session_id)
        failures = len(collected["errors"]) + analyzed["failed"] + len(
            analyzed["errors"]
        )
        return len(progressed_ids), failures

    def _eligible_sources(self) -> tuple[list[CodexSource], list[str]]:
        sources, errors = discover_sources(
            {"active": self.active_root, "archived": self.archived_root}
        )
        projects = self.assistant.projects()
        return [source for source in sources if _project(source, projects)], errors

    def _settled_count(self, sources: Any) -> int:
        count = 0
        now = self.assistant._now()
        for source in sources:
            state = self.assistant._read_codex_analysis_state(source.id)
            if state is None or state.get("status") not in {"provisional", "final"}:
                continue
            if codex_analysis.due_kind(state, source.source_root, now) is None:
                count += 1
        return count

    def _classify(
        self,
        sources: list[CodexSource],
        known_before: set[str],
        bootstrapping: bool,
    ) -> _Work:
        active: list[CodexSource] = []
        newly: list[CodexSource] = []
        historical: list[CodexSource] = []
        for source in sources:
            cursor = self.assistant._read_codex_cursor(source.id)
            try:
                has_delta = source_has_delta(source, cursor)
            except OSError:
                continue
            if source.source_root == "active" and has_delta:
                active.append(source)
                continue
            state = self.assistant._read_codex_analysis_state(source.id)
            due = False
            if cursor is not None and state is not None:
                due = (
                    codex_analysis.due_kind(
                        state, source.source_root, self.assistant._now()
                    )
                    is not None
                )
            newly_archived = source.source_root == "archived" and (
                (cursor is not None and cursor.get("source_root") == "active")
                or (not bootstrapping and source.id not in known_before)
            )
            if due or newly_archived:
                newly.append(source)
            elif cursor is None:
                historical.append(source)
        newest = lambda source: (_mtime_ns(source), source.id)
        return _Work(
            tuple(sorted(active, key=newest, reverse=True)),
            tuple(sorted(newly, key=newest, reverse=True)),
            tuple(sorted(historical, key=newest, reverse=True)),
        )

    def _select(self, work: _Work) -> _Work:
        remaining = self.capacity
        active = work.active[:remaining]
        remaining -= len(active)
        newly = work.newly_eligible[:remaining]
        remaining -= len(newly)
        historical = work.historical[:remaining]
        return _Work(active, newly, historical)

    def _read_state(self) -> tuple[dict[str, Any], bool]:
        value = _read_object(self.state_path)
        if not value:
            return {"schema": STATE_SCHEMA, "sources": {}}, True
        if value.get("schema") != STATE_SCHEMA or not isinstance(
            value.get("sources"), dict
        ):
            from headlong_web.assistant import AssistantError

            raise AssistantError("unsupported Codex scheduler state")
        return value, False

    def _write_state(
        self, state: dict[str, Any], sources: list[CodexSource], now: str
    ) -> None:
        records = state["sources"]
        for source in sources:
            previous = records.get(source.id)
            previous = previous if isinstance(previous, dict) else {}
            records[source.id] = {
                "first_seen_at": previous.get("first_seen_at") or now,
                "last_seen_at": now,
                "source_root": source.source_root,
            }
        state["updated_at"] = now
        self.assistant._write_state_json(self.state_path, state)

    def _cursor_offsets(
        self, session_ids: set[str]
    ) -> dict[str, tuple[str, int] | None]:
        return {
            session_id: (
                (str(cursor.get("canonical_path") or ""), int(cursor["byte_offset"]))
                if (cursor := self.assistant._read_codex_cursor(session_id))
                else None
            )
            for session_id in session_ids
        }

    def _cursor_progress(
        self,
        session_ids: set[str],
        before: dict[str, tuple[str, int] | None],
    ) -> int:
        return len(self._cursor_progress_ids(session_ids, before))

    def _cursor_progress_ids(
        self,
        session_ids: set[str],
        before: dict[str, tuple[str, int] | None],
    ) -> set[str]:
        after = self._cursor_offsets(session_ids)
        return {
            session_id
            for session_id in session_ids
            if after[session_id] is not None and after[session_id] != before[session_id]
        }

    def _analysis_markers(self, session_ids: set[str]) -> dict[str, tuple[Any, ...]]:
        return {session_id: self._analysis_marker(session_id) for session_id in session_ids}

    def _analysis_marker(self, session_id: str) -> tuple[Any, ...]:
        state = self.assistant._read_codex_analysis_state(session_id) or {}
        return (
            state.get("provisional_event_id"),
            state.get("final_event_id"),
            state.get("failure_event_id"),
        )


def _capacity(value: int | None) -> int:
    if value is None:
        raw = os.environ.get("HEADLONG_CODEX_CYCLE_CAPACITY", "")
        try:
            value = int(raw) if raw else DEFAULT_CAPACITY
        except ValueError:
            value = DEFAULT_CAPACITY
    if isinstance(value, bool) or not 1 <= value <= MAX_CAPACITY:
        raise ValueError(f"capacity must be between 1 and {MAX_CAPACITY}")
    return value


def _project(
    source: CodexSource, projects: list[RegisteredProject]
) -> RegisteredProject | None:
    matches: list[RegisteredProject] = []
    for project in projects:
        try:
            source.cwd.relative_to(project.path)
        except ValueError:
            continue
        matches.append(project)
    return max(matches, key=lambda item: len(item.path.parts), default=None)


def _mtime_ns(source: CodexSource) -> int:
    try:
        return source.path.stat().st_mtime_ns
    except OSError:
        return 0


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _empty_collection() -> dict[str, Any]:
    return {
        "appended": 0,
        "deferred": 0,
        "discovered": 0,
        "duplicate": 0,
        "eligible": 0,
        "errors": [],
        "recovered": 0,
        "status": "ok",
    }


def _empty_analysis() -> dict[str, Any]:
    return {
        "discovered": 0,
        "eligible": 0,
        "waiting": 0,
        "provisional": 0,
        "final": 0,
        "duplicate": 0,
        "failed": 0,
        "errors": [],
        "status": "ok",
        "sessions": [],
        "work_proposals_created": 0,
    }


def _merge(target: dict[str, Any], value: dict[str, Any]) -> None:
    for key, item in value.items():
        if key == "status":
            if item != "ok":
                target[key] = "degraded"
        elif isinstance(item, int) and not isinstance(item, bool):
            target[key] = int(target.get(key, 0)) + item
        elif isinstance(item, list):
            target.setdefault(key, []).extend(item)
