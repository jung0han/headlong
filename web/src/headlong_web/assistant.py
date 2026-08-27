"""Deterministic boundaries for HeadLong's Personal Assistant.

The model interprets source material.  This module owns registration, source
eligibility, evidence addressing, event shape, and idempotent ledger writes.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import socket
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from headlong_web import control, discovery, trajectory

REGISTRATION_SCHEMA = "headlong.assistant.registrations/v1"
EVENT_SCHEMA = "headlong.activity-ledger/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
ANALYSIS_SCHEMA = "headlong.codex-observation/v1"
_EVENT_NAMESPACE = uuid.UUID("88d66cf8-0918-4593-974e-71e544b6fd5b")
_MAX_TITLE = 160
_MAX_OBSERVATION = 1200


class AssistantError(RuntimeError):
    """A user-actionable Personal Assistant boundary failure."""


@dataclass(frozen=True)
class RegisteredProject:
    id: str
    name: str
    path: Path

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "path": str(self.path)}


@dataclass(frozen=True)
class EvidenceLocator:
    """Address one immutable complete line in a local Codex Session."""

    source_identity: str
    source_root: str
    relative_path: str
    line: int
    byte_offset: int
    byte_length: int
    sha256: str
    host: str
    schema: str = LOCATOR_SCHEMA
    kind: str = "codex_event"

    def to_dict(self) -> dict[str, str | int]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "source_identity": self.source_identity,
            "source_root": self.source_root,
            "relative_path": self.relative_path,
            "line": self.line,
            "byte_offset": self.byte_offset,
            "byte_length": self.byte_length,
            "sha256": self.sha256,
            "host": self.host,
        }

    def encode(self) -> str:
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":")
        ).encode()
        token = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"headlong-evidence:v1:{token}"

    @classmethod
    def decode(cls, value: str | dict[str, Any]) -> EvidenceLocator:
        if isinstance(value, str):
            prefix = "headlong-evidence:v1:"
            if not value.startswith(prefix):
                raise AssistantError("unsupported Evidence Locator encoding")
            token = value[len(prefix) :]
            try:
                raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
                value = json.loads(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                raise AssistantError("malformed Evidence Locator") from exc
        if not isinstance(value, dict):
            raise AssistantError("Evidence Locator must be an object")
        expected = {
            "schema",
            "kind",
            "source_identity",
            "source_root",
            "relative_path",
            "line",
            "byte_offset",
            "byte_length",
            "sha256",
            "host",
        }
        if set(value) != expected:
            raise AssistantError("Evidence Locator fields do not match v1")
        if value.get("schema") != LOCATOR_SCHEMA or value.get("kind") != "codex_event":
            raise AssistantError("unsupported Evidence Locator kind or schema")
        try:
            locator = cls(
                source_identity=_canonical_uuid(value["source_identity"]),
                source_root=str(value["source_root"]),
                relative_path=str(value["relative_path"]),
                line=int(value["line"]),
                byte_offset=int(value["byte_offset"]),
                byte_length=int(value["byte_length"]),
                sha256=str(value["sha256"]),
                host=str(value["host"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AssistantError("invalid Evidence Locator field") from exc
        if locator.source_root not in {"active", "archived"}:
            raise AssistantError("invalid Evidence Locator source root")
        if (
            locator.line < 1
            or locator.byte_offset < 0
            or locator.byte_length < 1
            or len(locator.sha256) != 64
            or Path(locator.relative_path).is_absolute()
            or ".." in Path(locator.relative_path).parts
        ):
            raise AssistantError("invalid Evidence Locator coordinates")
        return locator


@dataclass(frozen=True)
class CodexSession:
    id: str
    cwd: Path
    path: Path
    source_root: str
    relative_path: str
    evidence: EvidenceLocator


def resolve_observer(root: Path, selector: str | None) -> discovery.IdentityInfo:
    root = root.resolve()
    identities = discovery.scan_identities(root)
    if selector:
        matches = [
            item
            for item in identities
            if selector in {item.id, item.name, item.path.name}
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AssistantError(f"identity selector is ambiguous: {selector}")
        raise AssistantError(f"identity not found: {selector}")
    default = root / ".identities" / "default"
    if default.is_symlink():
        name = default.resolve().name
        matches = [item for item in identities if item.path.name == name]
        if len(matches) == 1:
            return matches[0]
    if len(identities) == 1:
        return identities[0]
    raise AssistantError("select an Observer Identity with --identity")


class PersonalAssistant:
    """Identity-local application boundary shared by CLI and dashboard."""

    def __init__(self, root: Path, identity: discovery.IdentityInfo):
        self.root = root.resolve()
        self.identity = identity
        self.state_dir = identity.path / "assistant"
        self.registrations_file = self.state_dir / "registrations.json"

    def projects(self) -> list[RegisteredProject]:
        data = self._read_registrations()
        return [
            RegisteredProject(item["id"], item["name"], Path(item["path"]))
            for item in data["projects"]
        ]

    def add_project(self, path: Path, name: str | None = None) -> RegisteredProject:
        try:
            canonical = path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise AssistantError(f"Registered Project does not exist: {path}") from exc
        if not canonical.is_dir():
            raise AssistantError(f"Registered Project is not a directory: {canonical}")
        project_id = "project-" + hashlib.sha256(str(canonical).encode()).hexdigest()[:20]
        project = RegisteredProject(project_id, name or canonical.name, canonical)
        with self._state_lock():
            data = self._read_registrations()
            existing = next((p for p in data["projects"] if p["id"] == project_id), None)
            if existing is None:
                data["projects"].append(project.to_dict())
                data["projects"].sort(key=lambda item: item["id"])
                self._write_registrations(data)
            else:
                project = RegisteredProject(
                    existing["id"], existing["name"], Path(existing["path"])
                )
        return project

    def remove_project(self, selector: str) -> RegisteredProject:
        with self._state_lock():
            data = self._read_registrations()
            canonical = Path(selector).expanduser().resolve(strict=False)
            matches = [
                item
                for item in data["projects"]
                if selector in {item["id"], item["name"]}
                or Path(item["path"]) == canonical
            ]
            if len(matches) != 1:
                message = "not found" if not matches else "ambiguous"
                raise AssistantError(f"Registered Project {message}: {selector}")
            removed = matches[0]
            data["projects"] = [p for p in data["projects"] if p["id"] != removed["id"]]
            self._write_registrations(data)
        return RegisteredProject(removed["id"], removed["name"], Path(removed["path"]))

    def observe_codex_once(self, active_root: Path, archived_root: Path) -> dict[str, int]:
        """Analyze every currently eligible, not-yet-observed complete session."""
        roots = {"active": active_root.resolve(), "archived": archived_root.resolve()}
        result = {"discovered": 0, "eligible": 0, "observed": 0, "duplicate": 0}
        # Serialize registration eligibility, dedupe, model cost, and append.
        with self._state_lock():
            projects = self.projects()
            for session in discover_codex_sessions(roots):
                result["discovered"] += 1
                project = _project_for_cwd(projects, session.cwd)
                if project is None:
                    continue
                result["eligible"] += 1
                event_id = _observation_id(project.id, session.evidence)
                if self._ledger_has(event_id):
                    result["duplicate"] += 1
                    continue
                analysis = self._analyze(session)
                event = observation_event(event_id, project, session, analysis)
                self._append_event(event)
                result["observed"] += 1
        return result

    def resolve_evidence(
        self, locator: EvidenceLocator, active_root: Path, archived_root: Path
    ) -> bytes:
        return resolve_evidence(
            locator,
            {"active": active_root.resolve(), "archived": archived_root.resolve()},
        )

    def _analyze(self, session: CodexSession) -> dict[str, str]:
        try:
            transcript = session.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AssistantError(f"cannot read Codex Session: {session.path}") from exc
        system = (
            "You analyze one completed Codex development session. Return only a JSON "
            'object with exactly two string fields: "title" and "observation". '
            "Describe the meaningful outcome, correction, failure, decision, or open loop. "
            "Be compact; do not reproduce the transcript or complete tool payloads."
        )
        prompt = (
            f"Registered Project: {session.cwd}\n"
            f"Codex Session: {session.id}\n\n"
            "AUTHORITATIVE SOURCE JSONL FOLLOWS\n"
            f"{transcript}"
        )
        env = control.identity_env(self.identity, self.root)
        cmd = control._wrap(
            "llm",
            "--no-stream",
            "-t",
            "1200",
            "--system-prompt",
            system,
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.root,
                env=env,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=600,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssistantError("Codex Session analysis failed to run") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "model call failed").strip().splitlines()[-1]
            raise AssistantError(f"Codex Session analysis failed: {detail}")
        try:
            value = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise AssistantError("model returned invalid observation JSON") from exc
        if not isinstance(value, dict) or set(value) != {"title", "observation"}:
            raise AssistantError("model observation does not match the required schema")
        title = value["title"]
        observation = value["observation"]
        if (
            not isinstance(title, str)
            or not title.strip()
            or len(title) > _MAX_TITLE
            or not isinstance(observation, str)
            or not observation.strip()
            or len(observation) > _MAX_OBSERVATION
        ):
            raise AssistantError("model observation is empty or exceeds compact limits")
        return {"title": title.strip(), "observation": observation.strip()}

    def _ledger_has(self, event_id: str) -> bool:
        traj_dir = discovery.find_root_traj_dir(self.identity)
        if traj_dir is None:
            raise AssistantError("Observer Identity has no root trajectory")
        return any(step.get("step_id") == event_id for step in trajectory.iter_jsonl(traj_dir / "trajectory.jsonl"))

    def _append_event(self, event: dict[str, Any]) -> None:
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
            raise AssistantError(detail)

    def _read_registrations(self) -> dict[str, Any]:
        try:
            data = json.loads(self.registrations_file.read_text())
        except FileNotFoundError:
            return {"schema": REGISTRATION_SCHEMA, "projects": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise AssistantError("cannot read registrations.json") from exc
        if (
            not isinstance(data, dict)
            or data.get("schema") != REGISTRATION_SCHEMA
            or not isinstance(data.get("projects"), list)
        ):
            raise AssistantError("unsupported registrations.json schema")
        return data

    def _write_registrations(self, data: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        temp = self.registrations_file.with_suffix(f".tmp.{os.getpid()}")
        temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        os.replace(temp, self.registrations_file)

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock = self.state_dir / ".lock"
        with lock.open("a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            yield


def observation_event(
    event_id: str,
    project: RegisteredProject,
    session: CodexSession,
    analysis: dict[str, str],
) -> dict[str, Any]:
    """Build the shared consequential-event envelope for the Activity Ledger."""
    return activity_event(
        event_type="observation",
        event_id=event_id,
        source_kind="codex_session",
        source_identity=session.id,
        knowledge_scope={"kind": "project", "project_id": project.id},
        evidence_kind="model_inference",
        verification="observed",
        authority="candidate",
        evidence_locators=[session.evidence.to_dict()],
        title=analysis["title"],
        content=analysis["observation"],
        details={
            "analysis_kind": "completed_session",
            "analysis_schema": ANALYSIS_SCHEMA,
        },
    )


def activity_event(
    *,
    event_type: str,
    event_id: str,
    source_kind: str,
    source_identity: str,
    knowledge_scope: dict[str, str],
    evidence_kind: str,
    verification: str,
    authority: str,
    evidence_locators: list[dict[str, Any]],
    title: str,
    content: str,
    causal_event_ids: list[str] | None = None,
    supersedes_event_ids: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the v1 envelope future source and projection modules share."""
    scope_kind = knowledge_scope.get("kind")
    if scope_kind not in {"project", "global"}:
        raise AssistantError("Activity Ledger event has invalid Knowledge Scope")
    if scope_kind == "project" and not knowledge_scope.get("project_id"):
        raise AssistantError("project-scoped event requires project_id")
    if evidence_kind not in {
        "user_statement",
        "observed_event",
        "primary_source",
        "secondary_source",
        "model_inference",
    }:
        raise AssistantError("Activity Ledger event has invalid evidence kind")
    if verification not in {
        "unverified",
        "observed",
        "corroborated",
        "refuted",
        "stale",
    }:
        raise AssistantError("Activity Ledger event has invalid verification")
    if authority not in {
        "candidate",
        "active",
        "rejected",
        "dismissed",
        "superseded",
    }:
        raise AssistantError("Activity Ledger event has invalid authority")
    event = {
        "type": event_type,
        "step_id": event_id,
        "event_id": event_id,
        "event_schema": EVENT_SCHEMA,
        "source": "personal_assistant",
        "source_kind": source_kind,
        "source_identity": source_identity,
        "knowledge_scope": knowledge_scope,
        "evidence_kind": evidence_kind,
        "verification": verification,
        "authority": authority,
        "causal_event_ids": causal_event_ids or [],
        "supersedes_event_ids": supersedes_event_ids or [],
        "evidence_locators": evidence_locators,
        "title": title,
        "content": content,
    }
    for key, value in (details or {}).items():
        if key in event:
            raise AssistantError(f"Activity Ledger detail conflicts with envelope: {key}")
        event[key] = value
    return event


def discover_codex_sessions(roots: dict[str, Path]) -> Iterator[CodexSession]:
    """Yield eligible sessions in stable order, deduplicated by session UUID."""
    found: list[CodexSession] = []
    for root_kind in ("archived", "active"):
        root = roots[root_kind]
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            session = _read_session(path, root_kind, root)
            if session is not None:
                found.append(session)
    # Archived is definitive and sorted first. Conflicting duplicates are an
    # error rather than an arbitrary choice; identical session ids collapse.
    by_id: dict[str, CodexSession] = {}
    for session in found:
        previous = by_id.get(session.id)
        if previous is None:
            by_id[session.id] = session
            continue
        if previous.path.read_bytes() != session.path.read_bytes():
            raise AssistantError(f"conflicting Codex Session identity: {session.id}")
    yield from by_id.values()


def resolve_evidence(locator: EvidenceLocator, roots: dict[str, Path]) -> bytes:
    candidates: list[Path] = []
    preferred_root = roots[locator.source_root]
    preferred = (preferred_root / locator.relative_path).resolve()
    if _contained(preferred, preferred_root):
        candidates.append(preferred)
    # A session commonly moves from active to archived. Search by validated
    # metadata when its original relative path no longer resolves.
    for root in roots.values():
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.jsonl")):
            if path not in candidates and _session_id(path) == locator.source_identity:
                candidates.append(path)
    for path in candidates:
        if not path.is_file() or _session_id(path) != locator.source_identity:
            continue
        try:
            with path.open("rb") as fh:
                prefix = fh.read(locator.byte_offset)
                if prefix.count(b"\n") + 1 != locator.line:
                    continue
                fh.seek(locator.byte_offset)
                raw = fh.read(locator.byte_length)
        except OSError:
            continue
        if hashlib.sha256(raw).hexdigest() != locator.sha256:
            continue
        if not raw.endswith(b"\n"):
            continue
        return raw
    raise AssistantError("Evidence Locator no longer resolves to matching source bytes")


def _read_session(path: Path, root_kind: str, root: Path) -> CodexSession | None:
    if not _contained(path.resolve(), root):
        return None
    first = _first_complete_line(path)
    if first is None:
        return None
    try:
        meta = json.loads(first[3])
        if meta.get("type") != "session_meta":
            return None
        payload = meta["payload"]
        session_id = _canonical_uuid(payload["id"])
        cwd = Path(payload["cwd"]).expanduser().resolve(strict=False)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    evidence_row: tuple[int, int, int, bytes] | None = None
    last_row: tuple[int, int, int, bytes] | None = None
    with path.open("rb") as fh:
        offset = 0
        for line_number, raw in enumerate(fh, 1):
            row = (line_number, offset, len(raw), raw)
            offset += len(raw)
            if not raw.endswith(b"\n"):
                continue
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            last_row = row
            if event.get("type") == "event_msg" and event.get("payload", {}).get("type") == "task_complete":
                evidence_row = row
    if root_kind == "active" and (evidence_row is None or evidence_row != last_row):
        return None
    if evidence_row is None:
        evidence_row = last_row
    if evidence_row is None:
        return None
    line_number, offset, length, raw = evidence_row
    locator = EvidenceLocator(
        source_identity=session_id,
        source_root=root_kind,
        relative_path=path.relative_to(root).as_posix(),
        line=line_number,
        byte_offset=offset,
        byte_length=length,
        sha256=hashlib.sha256(raw).hexdigest(),
        host=socket.gethostname(),
    )
    return CodexSession(
        id=session_id,
        cwd=cwd,
        path=path,
        source_root=root_kind,
        relative_path=locator.relative_path,
        evidence=locator,
    )


def _first_complete_line(path: Path) -> tuple[int, int, int, bytes] | None:
    try:
        with path.open("rb") as fh:
            raw = fh.readline()
    except OSError:
        return None
    if not raw.endswith(b"\n"):
        return None
    return (1, 0, len(raw), raw)


def _session_id(path: Path) -> str | None:
    first = _first_complete_line(path)
    if first is None:
        return None
    try:
        event = json.loads(first[3])
        if event.get("type") != "session_meta":
            return None
        return _canonical_uuid(event["payload"]["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _canonical_uuid(value: Any) -> str:
    parsed = uuid.UUID(str(value))
    canonical = str(parsed)
    if str(value).lower() != canonical:
        raise ValueError("UUID is not canonical")
    return canonical


def _project_for_cwd(
    projects: list[RegisteredProject], cwd: Path
) -> RegisteredProject | None:
    matches = [project for project in projects if _contained(cwd, project.path)]
    return max(matches, key=lambda item: len(item.path.parts), default=None)


def _contained(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _observation_id(project_id: str, locator: EvidenceLocator) -> str:
    key = f"{ANALYSIS_SCHEMA}:{project_id}:{locator.source_identity}:{locator.sha256}"
    return str(uuid.uuid5(_EVENT_NAMESPACE, key))
