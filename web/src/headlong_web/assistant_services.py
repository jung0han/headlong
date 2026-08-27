"""Focused deterministic services behind the PersonalAssistant facade."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from headlong_web import authority, control, discovery, proposals, shadow_gate, trajectory


class AssistantServiceError(RuntimeError):
    """A deterministic assistant service boundary failed."""


class ActivityLedger:
    """Own public trajectory appends and protected authority provenance."""

    def __init__(self, root: Path, identity: discovery.IdentityInfo):
        self.root = root.resolve()
        self.identity = identity
        self.authority = authority.AuthorityJournal(self.root, identity.id)
        self.authority.initialize()
        self._write_observer_marker()

    def events(self) -> list[dict[str, Any]]:
        public = [
            event
            for event in self._trajectory_events()
            if event.get("type") not in authority.PROTECTED_EVENT_TYPES
        ]
        try:
            protected = self.authority.read()
        except authority.AuthorityJournalError as exc:
            raise AssistantServiceError(str(exc)) from exc
        combined = [*public, *protected]
        # The two append boundaries retain one logical ledger order through
        # their append timestamps. Stable input order resolves the vanishingly
        # rare equal timestamp without making projections move on later reads.
        return sorted(combined, key=_ledger_time)

    def recovery_events(self) -> list[dict[str, Any]]:
        """Read the complete ledger without the viewer's malformed-line tolerance."""
        traj_dir = discovery.find_root_traj_dir(self.identity)
        if traj_dir is None:
            raise AssistantServiceError("Observer Identity has no root trajectory")
        public: list[dict[str, Any]] = []
        try:
            with (traj_dir / "trajectory.jsonl").open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("ledger row is not an object")
                    if event.get("type") not in authority.PROTECTED_EVENT_TYPES:
                        public.append(event)
            protected = self.authority.read()
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            authority.AuthorityJournalError,
        ) as exc:
            raise AssistantServiceError("Activity Ledger is corrupt") from exc
        return sorted([*public, *protected], key=_ledger_time)

    def append(self, event: dict[str, Any]) -> None:
        if "ts" not in event:
            event = {
                **event,
                "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
        if event.get("type") in authority.PROTECTED_EVENT_TYPES:
            try:
                self.authority.append(event)
            except authority.AuthorityJournalError as exc:
                raise AssistantServiceError(str(exc)) from exc
            return
        proc = subprocess.run(
            [
                str(control.BIN_DIR / "traj"),
                "append",
                "--traj_dir",
                str(self.identity.path / "trajectories"),
                str(self.identity.root_trajectory),
            ],
            cwd=self.root,
            env=control.identity_env(self.identity, self.root),
            input=json.dumps(event, separators=(",", ":")),
            text=True,
            capture_output=True,
            timeout=30,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or "trajectory append failed").strip().splitlines()[-1]
            raise AssistantServiceError(detail)

    def _trajectory_events(self) -> list[dict[str, Any]]:
        traj_dir = discovery.find_root_traj_dir(self.identity)
        if traj_dir is None:
            raise AssistantServiceError("Observer Identity has no root trajectory")
        return list(trajectory.iter_jsonl(traj_dir / "trajectory.jsonl"))

    def _write_observer_marker(self) -> None:
        directory = self.root / ".assistant-observers"
        directory.mkdir(mode=0o700, exist_ok=True)
        marker = directory / f"{self.identity.path.name}.env"
        if marker.exists():
            return
        try:
            marker.write_text("HEADLONG_PROPOSAL_ONLY=1\n", encoding="utf-8")
            marker.chmod(0o600)
        except OSError as exc:
            raise AssistantServiceError("cannot configure Observer isolation") from exc


class GovernanceService:
    """Own Proposal Inbox and Shadow Gate projections and user review events."""

    def __init__(
        self,
        ledger: ActivityLedger,
        *,
        clock: Callable[[], datetime],
    ):
        self.ledger = ledger
        self.clock = clock

    def proposals(self) -> list[dict[str, Any]]:
        try:
            return proposals.build_inbox(self.ledger.events())
        except proposals.ProposalError as exc:
            raise AssistantServiceError(str(exc)) from exc

    def proposal(self, proposal_id: str) -> dict[str, Any] | None:
        try:
            return proposals.find_proposal(self.ledger.events(), proposal_id)
        except proposals.ProposalError as exc:
            raise AssistantServiceError(str(exc)) from exc

    def review_proposal(self, proposal_id: str, state: str) -> dict[str, Any]:
        current = self.proposal(proposal_id)
        if current is None:
            raise AssistantServiceError(
                f"Work Improvement Proposal not found: {proposal_id}"
            )
        try:
            event = proposals.review_event(current, state)
        except proposals.ProposalError as exc:
            raise AssistantServiceError(str(exc)) from exc
        self.ledger.append(event)
        reviewed = self.proposal(proposal_id)
        if reviewed is None:
            raise AssistantServiceError("reviewed proposal disappeared from the ledger")
        return reviewed

    def shadow_report(self) -> dict[str, Any]:
        try:
            return shadow_gate.report(self.ledger.events(), self.clock())
        except shadow_gate.ShadowGateError as exc:
            raise AssistantServiceError(str(exc)) from exc

    def observations(self) -> list[dict[str, Any]]:
        try:
            return shadow_gate.observations(self.ledger.events())
        except shadow_gate.ShadowGateError as exc:
            raise AssistantServiceError(str(exc)) from exc

    def review_observation(
        self, observation_event_id: str, *, useful: bool, accurate: bool
    ) -> dict[str, Any]:
        events = self.ledger.events()
        try:
            event = shadow_gate.observation_evaluation_event(
                events,
                observation_event_id,
                useful=useful,
                accurate=accurate,
                reviewed_at=self.clock(),
            )
        except shadow_gate.ShadowGateError as exc:
            raise AssistantServiceError(str(exc)) from exc
        self.ledger.append(event)
        return next(
            item
            for item in shadow_gate.observations([*events, event])
            if item["event_id"] == event["observation_event_id"]
        )

    def memories(self) -> list[dict[str, Any]]:
        try:
            return shadow_gate.active_memories(self.ledger.events())
        except shadow_gate.ShadowGateError as exc:
            raise AssistantServiceError(str(exc)) from exc

    def review_memory(self, memory_event_id: str, *, correct: bool) -> dict[str, Any]:
        events = self.ledger.events()
        try:
            event = shadow_gate.memory_evaluation_event(
                events,
                memory_event_id,
                correct=correct,
                reviewed_at=self.clock(),
            )
        except shadow_gate.ShadowGateError as exc:
            raise AssistantServiceError(str(exc)) from exc
        self.ledger.append(event)
        return next(
            item
            for item in shadow_gate.active_memories([*events, event])
            if item["event_id"] == event["memory_event_id"]
        )


def _ledger_time(event: dict[str, Any]) -> float:
    value = event.get("ts")
    if not isinstance(value, str):
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return float("-inf")
