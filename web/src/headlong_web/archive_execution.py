"""Narrow, ledger-backed execution contract for Codex session archival."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

ATTEMPT_SCHEMA = "headlong.codex-archive-attempt/v1"
RESULT_SCHEMA = "headlong.codex-archive-result/v1"
DIRECTIVE_SCHEMA = "headlong.archive-directive/v1"
OPERATIONS = frozenset({"archive", "unarchive"})
RESULT_STATES = frozenset(
    {"succeeded", "already_done", "failed", "timeout", "unsupported", "indeterminate"}
)


class ArchiveExecutionError(ValueError):
    """An archive execution event or request violated the contract."""


@dataclass(frozen=True)
class AdapterResult:
    """Bounded outcome returned by the archive-only Codex adapter."""

    state: str
    error_code: str | None = None
    message: str | None = None
    exit_code: int | None = None

    def __post_init__(self) -> None:
        if self.state not in RESULT_STATES:
            raise ArchiveExecutionError(f"unsupported archive adapter result: {self.state}")


class ArchiveAdapter(Protocol):
    """Minimal mutation capability granted to the governance boundary."""

    def execute(self, operation: str, session_id: str) -> AdapterResult: ...


@dataclass(frozen=True)
class CommandResult:
    """Process information required by the fixed Codex command contract."""

    returncode: int | None
    stdout: str
    stderr: str


class CommandExecutor(Protocol):
    def run(self, command: tuple[str, ...], *, timeout: float) -> CommandResult: ...


class SubprocessCommandExecutor:
    """Run only commands assembled by ``CodexArchiveAdapter``."""

    def run(self, command: tuple[str, ...], *, timeout: float) -> CommandResult:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class CodexArchiveAdapter:
    """Capability-probing adapter exposing only Codex archive and unarchive."""

    def __init__(
        self,
        *,
        executor: CommandExecutor | None = None,
        binary: str | None = None,
        timeout: float = 30.0,
    ):
        self.executor = executor or SubprocessCommandExecutor()
        self.binary = shutil.which("codex") if binary is None else binary
        self.timeout = timeout
        self._probe_result: AdapterResult | None = None
        self._probed = False

    def execute(self, operation: str, session_id: str) -> AdapterResult:
        operation = _operation(operation)
        session_id = _uuid(session_id, "Codex Session id")
        if not self.binary:
            return AdapterResult(
                "unsupported",
                error_code="codex_unavailable",
                message="Codex CLI is unavailable; install a CLI with archive and unarchive support.",
            )
        capability = self._probe()
        if capability is not None:
            return capability
        try:
            completed = self.executor.run((self.binary, operation, session_id), timeout=self.timeout)
        except (TimeoutError, subprocess.TimeoutExpired):
            return AdapterResult(
                "timeout",
                error_code="command_timeout",
                message=f"Codex {operation} timed out; the session state may be unchanged. Retry explicitly.",
            )
        except OSError as exc:
            return AdapterResult(
                "failed",
                error_code="command_unavailable",
                message=_bounded_message(str(exc), f"Codex {operation} could not start."),
            )
        if completed.returncode is None:
            return AdapterResult(
                "indeterminate",
                error_code="partial_response",
                message=f"Codex {operation} returned no completion status; inspect Codex and retry explicitly.",
            )
        if completed.returncode == 0:
            return AdapterResult("succeeded", exit_code=0)
        detail = _bounded_message(
            completed.stderr or completed.stdout,
            f"Codex {operation} failed with exit code {completed.returncode}.",
        )
        normalized = detail.lower()
        already = (operation == "archive" and "already archived" in normalized) or (
            operation == "unarchive" and ("not archived" in normalized or "already active" in normalized)
        )
        if already:
            return AdapterResult("already_done", message=detail, exit_code=completed.returncode)
        return AdapterResult(
            "failed",
            error_code="command_failed",
            message=detail,
            exit_code=completed.returncode,
        )

    def _probe(self) -> AdapterResult | None:
        if self._probed:
            return self._probe_result
        self._probed = True
        assert self.binary is not None
        for operation in ("archive", "unarchive"):
            try:
                completed = self.executor.run(
                    (self.binary, operation, "--help"), timeout=min(self.timeout, 10.0)
                )
            except (TimeoutError, subprocess.TimeoutExpired, OSError):
                self._probe_result = AdapterResult(
                    "unsupported",
                    error_code="capability_probe_failed",
                    message="Could not verify the installed Codex archive and unarchive contract; no mutation was attempted.",
                )
                return self._probe_result
            output = f"{completed.stdout}\n{completed.stderr}".lower()
            if (
                completed.returncode != 0
                or f"usage: codex {operation}" not in output
                or "<session>" not in output
            ):
                self._probe_result = AdapterResult(
                    "unsupported",
                    error_code="unsupported_contract",
                    message="Installed Codex does not expose the required archive and unarchive session contract; no mutation was attempted.",
                )
                return self._probe_result
        self._probe_result = None
        return None


def directive_event(operation: str, session_id: str, *, event_id: str | None = None) -> dict[str, Any]:
    """Build explicit user authority for one stable Codex Session identity."""
    operation = _operation(operation)
    session_id = _uuid(session_id, "Codex Session id")
    directive_id = _uuid(event_id or str(uuid.uuid4()), "directive event id")
    return {
        "type": "archive-directive",
        "step_id": directive_id,
        "event_id": directive_id,
        "event_schema": "headlong.activity-ledger/v1",
        "archive_directive_schema": DIRECTIVE_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "user_directive",
        "source_identity": session_id,
        "session_id": session_id,
        "operation": operation,
        "evidence_kind": "user_statement",
        "verification": "observed",
        "authority": "active",
        "archive_authority": "authorized",
        "execution_authority": "codex_archive_only",
        "causal_event_ids": [],
        "title": f"Codex session {operation} directive",
        "content": f"The user authorized Codex {operation} for the identified session.",
    }


def attempt_event(
    *,
    operation: str,
    session_id: str,
    authorization_event_id: str,
    candidate_id: str | None,
    attempt_number: int,
    event_id: str | None = None,
) -> dict[str, Any]:
    operation = _operation(operation)
    session_id = _uuid(session_id, "Codex Session id")
    authorization_event_id = _uuid(authorization_event_id, "authorization event id")
    attempt_id = _uuid(event_id or str(uuid.uuid4()), "attempt event id")
    if not isinstance(attempt_number, int) or attempt_number < 1:
        raise ArchiveExecutionError("archive attempt number must be positive")
    return {
        "type": "codex-archive-attempt",
        "step_id": attempt_id,
        "event_id": attempt_id,
        "event_schema": "headlong.activity-ledger/v1",
        "archive_attempt_schema": ATTEMPT_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "archive_executor",
        "source_identity": session_id,
        "session_id": session_id,
        "operation": operation,
        "authorization_event_id": authorization_event_id,
        "candidate_id": _optional_uuid(candidate_id, "candidate id"),
        "attempt_number": attempt_number,
        "idempotency_key": f"{operation}:{authorization_event_id}:{attempt_number}",
        "authority": "authorized",
        "execution_authority": "codex_archive_only",
        "causal_event_ids": [authorization_event_id],
        "title": f"Codex session {operation} attempt",
        "content": f"Attempted Codex {operation} through the supported interface.",
    }


def result_event(
    attempt: dict[str, Any], result: AdapterResult, *, event_id: str | None = None
) -> dict[str, Any]:
    attempt = _attempt(attempt)
    result_id = _uuid(event_id or str(uuid.uuid4()), "result event id")
    return {
        "type": "codex-archive-result",
        "step_id": result_id,
        "event_id": result_id,
        "event_schema": "headlong.activity-ledger/v1",
        "archive_result_schema": RESULT_SCHEMA,
        "source": "personal_assistant",
        "source_kind": "archive_executor",
        "source_identity": attempt["session_id"],
        "session_id": attempt["session_id"],
        "operation": attempt["operation"],
        "authorization_event_id": attempt["authorization_event_id"],
        "candidate_id": attempt["candidate_id"],
        "attempt_id": attempt["event_id"],
        "attempt_number": attempt["attempt_number"],
        "execution_state": result.state,
        "error_code": result.error_code,
        "error_message": result.message,
        "exit_code": result.exit_code,
        "authority": "authorized",
        "execution_authority": "codex_archive_only",
        "causal_event_ids": [attempt["event_id"]],
        "title": f"Codex session {attempt['operation']} result: {result.state}",
        "content": result.message or f"Codex {attempt['operation']} {result.state}.",
    }


def candidate_execution(events: Iterable[dict[str, Any]], candidate_id: str) -> dict[str, Any]:
    candidate_id = _uuid(candidate_id, "candidate id")
    attempts: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("archive_attempt_schema") == ATTEMPT_SCHEMA:
            attempt = _attempt(event)
            if attempt["candidate_id"] == candidate_id and attempt["operation"] == "archive":
                attempts[attempt["event_id"]] = attempt
        elif event.get("archive_result_schema") == RESULT_SCHEMA:
            result = _result(event)
            if result["candidate_id"] == candidate_id and result["operation"] == "archive":
                results[result["attempt_id"]] = result
    ordered = sorted(attempts.values(), key=lambda item: item["attempt_number"])
    if not ordered:
        return {
            "execution_state": "not_requested",
            "execution_attempts": 0,
            "execution_error": None,
            "last_execution_at": None,
        }
    latest = ordered[-1]
    result = results.get(latest["event_id"])
    if result is None:
        return {
            "execution_state": "indeterminate",
            "execution_attempts": len(ordered),
            "execution_error": {
                "code": "result_missing",
                "message": "Archive attempt has no durable result; retry explicitly.",
            },
            "last_execution_at": latest.get("ts"),
        }
    error = None
    if result["execution_state"] not in {"succeeded", "already_done"}:
        error = {
            "code": result.get("error_code") or "archive_failed",
            "message": result.get("error_message") or "Codex archive failed; retry explicitly.",
        }
    return {
        "execution_state": result["execution_state"],
        "execution_attempts": len(ordered),
        "execution_error": error,
        "last_execution_at": result.get("ts"),
    }


def execution_history(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project attempts and results into restart-safe operation records."""
    event_list = list(events)
    directives: dict[str, dict[str, Any]] = {}
    candidate_reviews: dict[str, dict[str, Any]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    attempt_order: list[str] = []
    results: dict[str, dict[str, Any]] = {}
    for event in event_list:
        if event.get("archive_directive_schema") == DIRECTIVE_SCHEMA:
            directive = _directive(event)
            directives[directive["event_id"]] = directive
        elif (
            event.get("archive_candidate_review_schema") == "headlong.archive-candidate-review/v1"
            and event.get("review_state") == "accepted"
        ):
            candidate_reviews[str(event.get("event_id"))] = event
        elif event.get("archive_attempt_schema") == ATTEMPT_SCHEMA:
            attempt = _attempt(event)
            attempts[attempt["event_id"]] = attempt
            attempt_order.append(attempt["event_id"])
        elif event.get("archive_result_schema") == RESULT_SCHEMA:
            result = _result(event)
            results[result["attempt_id"]] = result
    output: list[dict[str, Any]] = []
    for attempt_id in attempt_order:
        attempt = attempts[attempt_id]
        authorization_id = attempt["authorization_event_id"]
        if authorization_id in directives:
            authorization_kind = "direct"
        elif authorization_id in candidate_reviews:
            authorization_kind = "candidate_acceptance"
        else:
            raise ArchiveExecutionError("archive attempt has no signed authorization")
        result = results.get(attempt["event_id"])
        state = result["execution_state"] if result else "indeterminate"
        error = None
        if result is None:
            error = {
                "code": "result_missing",
                "message": "Archive attempt has no durable result; retry explicitly.",
            }
        elif state not in {"succeeded", "already_done"}:
            error = {
                "code": result.get("error_code") or "archive_failed",
                "message": result.get("error_message")
                or "Codex archive operation failed; retry explicitly.",
            }
        output.append(
            {
                "session_id": attempt["session_id"],
                "operation": attempt["operation"],
                "authorization_event_id": authorization_id,
                "authorization_kind": authorization_kind,
                "candidate_id": attempt["candidate_id"],
                "attempt_id": attempt["event_id"],
                "attempt_number": attempt["attempt_number"],
                "execution_state": state,
                "execution_error": error,
                "attempted_at": attempt.get("ts"),
                "completed_at": result.get("ts") if result else None,
            }
        )
    return output


def latest_session_execution(events: Iterable[dict[str, Any]], session_id: str) -> dict[str, Any] | None:
    session_id = _uuid(session_id, "Codex Session id")
    matching = [item for item in execution_history(events) if item["session_id"] == session_id]
    return matching[-1] if matching else None


def attempt_count(events: Iterable[dict[str, Any]], authorization_event_id: str) -> int:
    authorization_event_id = _uuid(authorization_event_id, "authorization event id")
    return sum(
        1
        for event in events
        if event.get("archive_attempt_schema") == ATTEMPT_SCHEMA
        and event.get("authorization_event_id") == authorization_event_id
    )


def _attempt(value: dict[str, Any]) -> dict[str, Any]:
    attempt_id = _uuid(value.get("event_id"), "attempt event id")
    operation = _operation(value.get("operation"))
    session_id = _uuid(value.get("session_id"), "Codex Session id")
    authorization_id = _uuid(value.get("authorization_event_id"), "authorization event id")
    candidate_id = _optional_uuid(value.get("candidate_id"), "candidate id")
    number = value.get("attempt_number")
    if (
        value.get("type") != "codex-archive-attempt"
        or value.get("source_kind") != "archive_executor"
        or value.get("source_identity") != session_id
        or value.get("authority") != "authorized"
        or value.get("execution_authority") != "codex_archive_only"
        or value.get("causal_event_ids") != [authorization_id]
        or not isinstance(number, int)
        or number < 1
        or value.get("idempotency_key") != f"{operation}:{authorization_id}:{number}"
    ):
        raise ArchiveExecutionError("Codex archive attempt is invalid")
    return {
        **value,
        "event_id": attempt_id,
        "operation": operation,
        "session_id": session_id,
        "authorization_event_id": authorization_id,
        "candidate_id": candidate_id,
        "attempt_number": number,
    }


def _directive(value: dict[str, Any]) -> dict[str, Any]:
    directive_id = _uuid(value.get("event_id"), "directive event id")
    operation = _operation(value.get("operation"))
    session_id = _uuid(value.get("session_id"), "Codex Session id")
    if (
        value.get("type") != "archive-directive"
        or value.get("source_kind") != "user_directive"
        or value.get("source_identity") != session_id
        or value.get("evidence_kind") != "user_statement"
        or value.get("verification") != "observed"
        or value.get("authority") != "active"
        or value.get("archive_authority") != "authorized"
        or value.get("execution_authority") != "codex_archive_only"
        or value.get("causal_event_ids") != []
    ):
        raise ArchiveExecutionError("Archive Directive is invalid")
    return {
        **value,
        "event_id": directive_id,
        "operation": operation,
        "session_id": session_id,
    }


def _result(value: dict[str, Any]) -> dict[str, Any]:
    result_id = _uuid(value.get("event_id"), "result event id")
    attempt_id = _uuid(value.get("attempt_id"), "attempt event id")
    session_id = _uuid(value.get("session_id"), "Codex Session id")
    operation = _operation(value.get("operation"))
    authorization_id = _uuid(value.get("authorization_event_id"), "authorization event id")
    candidate_id = _optional_uuid(value.get("candidate_id"), "candidate id")
    state = value.get("execution_state")
    if (
        value.get("type") != "codex-archive-result"
        or value.get("source_kind") != "archive_executor"
        or value.get("source_identity") != session_id
        or value.get("authority") != "authorized"
        or value.get("execution_authority") != "codex_archive_only"
        or value.get("causal_event_ids") != [attempt_id]
        or state not in RESULT_STATES
    ):
        raise ArchiveExecutionError("Codex archive result is invalid")
    return {
        **value,
        "event_id": result_id,
        "attempt_id": attempt_id,
        "session_id": session_id,
        "operation": operation,
        "authorization_event_id": authorization_id,
        "candidate_id": candidate_id,
        "execution_state": state,
    }


def _operation(value: Any) -> str:
    if value not in OPERATIONS:
        raise ArchiveExecutionError(f"unsupported Codex archive operation: {value}")
    return str(value)


def _uuid(value: Any, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ArchiveExecutionError(f"invalid {field}") from exc


def _optional_uuid(value: Any, field: str) -> str | None:
    return None if value is None else _uuid(value, field)


def _bounded_message(value: str, fallback: str) -> str:
    compact = " ".join(str(value).split())
    return (compact or fallback)[:500]
