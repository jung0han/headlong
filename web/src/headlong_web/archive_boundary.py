"""Authenticated Unix-socket boundary for the Codex archive capability."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
from pathlib import Path
from typing import Any

from headlong_web import archive_candidates, archive_execution, authority


REQUEST_SCHEMA = "headlong.archive-boundary-request/v1"
RESPONSE_SCHEMA = "headlong.archive-boundary-response/v1"
DEFAULT_SOCKET = Path("/run/headlong-archive/archive.sock")
MAX_MESSAGE_BYTES = 8_192
_IDENTITY_RE = re.compile(r"[A-Za-z0-9._-]+(?:~[A-Za-z0-9._-]+)*")
_REQUEST_FIELDS = {
    "schema",
    "identity_id",
    "authorization_event_id",
    "operation",
    "session_id",
}
DEFAULT_CLIENT_TIMEOUT = 75.0


class ArchiveTransportLost(OSError):
    """The request was sent but its mutation outcome was not received."""


class ArchiveBoundaryClient:
    """Governance-facing client with no general command execution surface."""

    def __init__(
        self,
        identity_id: str,
        *,
        socket_path: Path | None = None,
        timeout: float = DEFAULT_CLIENT_TIMEOUT,
    ):
        self.identity_id = identity_id
        self.socket_path = socket_path or Path(
            os.environ.get("HEADLONG_ARCHIVE_SOCKET", str(DEFAULT_SOCKET))
        )
        self.timeout = timeout

    def execute(
        self, operation: str, session_id: str, authorization_event_id: str
    ) -> archive_execution.AdapterResult:
        request = {
            "schema": REQUEST_SCHEMA,
            "identity_id": self.identity_id,
            "authorization_event_id": authorization_event_id,
            "operation": operation,
            "session_id": session_id,
        }
        try:
            raw = _exchange(
                self.socket_path,
                json.dumps(request, separators=(",", ":")).encode(),
                self.timeout,
            )
            response = json.loads(raw)
            if not isinstance(response, dict) or set(response) != {
                "schema",
                "state",
                "error_code",
                "message",
                "exit_code",
            }:
                raise ValueError("invalid response fields")
            if response["schema"] != RESPONSE_SCHEMA:
                raise ValueError("invalid response schema")
            return archive_execution.AdapterResult(
                response["state"],
                error_code=_optional_text(response["error_code"]),
                message=_optional_text(response["message"]),
                exit_code=(
                    response["exit_code"]
                    if isinstance(response["exit_code"], int)
                    else None
                ),
            )
        except ArchiveTransportLost:
            return archive_execution.AdapterResult(
                "indeterminate",
                error_code="archive_boundary_transport_lost",
                message=(
                    "The archive request was sent but its durable outcome was not "
                    "received; retry to reconcile the same signed attempt."
                ),
            )
        except (OSError, TimeoutError, socket.timeout):
            return archive_execution.AdapterResult(
                "failed",
                error_code="archive_boundary_unavailable",
                message="The hardened Codex archive service is unavailable; no mutation was attempted.",
            )
        except (ValueError, json.JSONDecodeError, archive_execution.ArchiveExecutionError):
            return archive_execution.AdapterResult(
                "indeterminate",
                error_code="archive_boundary_invalid_response",
                message="The hardened Codex archive service returned an invalid bounded response.",
            )


def handle_request(
    root: Path,
    request: Any,
    *,
    executor: archive_execution.CodexArchiveExecutor,
) -> dict[str, Any]:
    """Verify signed authority independently, then invoke the fixed adapter."""
    try:
        operation, session_id, journal, attempt, durable = _authorized_request(
            root, request
        )
    except (
        ValueError,
        authority.AuthorityJournalError,
        archive_execution.ArchiveExecutionError,
        archive_candidates.ArchiveCandidateError,
    ):
        result = archive_execution.AdapterResult(
            "failed",
            error_code="unauthorized_request",
            message="Archive request did not match a signed active authorization.",
        )
        return _response(result)
    if durable is not None:
        result = archive_execution.adapter_result(durable)
    else:
        try:
            result = executor.execute(operation, session_id)
        except (OSError, RuntimeError):
            result = archive_execution.AdapterResult(
                "failed",
                error_code="archive_executor_failed",
                message="The fixed Codex archive executor failed; session state may be unchanged.",
            )
        journal.append(archive_execution.result_event(attempt, result))
    return _response(result)


def _authorized_request(
    root: Path, request: Any
) -> tuple[
    str,
    str,
    authority.AuthorityJournal,
    dict[str, Any],
    dict[str, Any] | None,
]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise ValueError("invalid request fields")
    if request.get("schema") != REQUEST_SCHEMA:
        raise ValueError("invalid request schema")
    identity_id = request.get("identity_id")
    if not isinstance(identity_id, str) or not _IDENTITY_RE.fullmatch(identity_id):
        raise ValueError("invalid identity")
    operation = archive_execution._operation(request.get("operation"))
    session_id = archive_execution._uuid(request.get("session_id"), "Codex Session id")
    authorization_id = archive_execution._uuid(
        request.get("authorization_event_id"), "authorization event id"
    )
    journal = authority.AuthorityJournal(root, identity_id)
    events = journal.read()
    authorization = next(
        (event for event in events if event.get("event_id") == authorization_id), None
    )
    if authorization is None:
        raise ValueError("authorization not found")
    if authorization.get("archive_directive_schema") == archive_execution.DIRECTIVE_SCHEMA:
        directive = archive_execution._directive(authorization)
        if directive["operation"] != operation or directive["session_id"] != session_id:
            raise ValueError("directive mismatch")
        latest_directive = next(
            (
                event
                for event in reversed(events)
                if event.get("archive_directive_schema")
                == archive_execution.DIRECTIVE_SCHEMA
                and event.get("session_id") == session_id
            ),
            None,
        )
        if latest_directive is None or latest_directive.get("event_id") != authorization_id:
            raise ValueError("directive is no longer active")
    elif (
        operation != "archive"
        or authorization.get("archive_candidate_review_schema")
        != archive_candidates.REVIEW_SCHEMA
        or authorization.get("review_state") != "accepted"
        or authorization.get("archive_authority") != "authorized"
        or authorization.get("session_id") != session_id
    ):
        raise ValueError("candidate authorization mismatch")
    else:
        candidate = archive_candidates.find_candidate(
            events, str(authorization.get("candidate_id"))
        )
        if candidate is None or candidate.get("review_event_id") != authorization_id:
            raise ValueError("candidate authorization is not current")

    attempts = [
        archive_execution._attempt(event)
        for event in events
        if event.get("archive_attempt_schema") == archive_execution.ATTEMPT_SCHEMA
        and event.get("authorization_event_id") == authorization_id
        and event.get("operation") == operation
        and event.get("session_id") == session_id
    ]
    if not attempts:
        raise ValueError("signed archive attempt not found")
    attempt = attempts[-1]
    durable = next(
        (
            archive_execution._result(event)
            for event in reversed(events)
            if event.get("archive_result_schema") == archive_execution.RESULT_SCHEMA
            and event.get("attempt_id") == attempt["event_id"]
        ),
        None,
    )
    return operation, session_id, journal, attempt, durable


def _response(result: archive_execution.AdapterResult) -> dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "state": result.state,
        "error_code": _optional_text(result.error_code),
        "message": _optional_text(result.message),
        "exit_code": result.exit_code,
    }


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("bounded text must be a string")
    return value[:500]


def _exchange(path: Path, payload: bytes, timeout: float) -> bytes:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("archive request exceeds bounded limits")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(path))
        try:
            client.sendall(payload + b"\n")
            data = _recv_line(client)
        except (OSError, TimeoutError, socket.timeout) as exc:
            raise ArchiveTransportLost from exc
    return data


def _recv_line(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = connection.recv(min(4096, MAX_MESSAGE_BYTES + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > MAX_MESSAGE_BYTES:
            raise ValueError("archive message exceeds bounded limits")
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def serve(
    root: Path,
    socket_path: Path,
    *,
    executor=None,
    max_requests: int | None = None,
) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    adapter = executor or archive_execution.CodexArchiveAdapter()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        server.listen(16)
        handled = 0
        while max_requests is None or handled < max_requests:
            connection, _address = server.accept()
            with connection:
                try:
                    request = json.loads(_recv_line(connection))
                    response = handle_request(root, request, executor=adapter)
                except (ValueError, json.JSONDecodeError):
                    response = _response(
                        archive_execution.AdapterResult(
                            "failed",
                            error_code="unauthorized_request",
                            message="Archive request was not valid.",
                        )
                    )
                try:
                    connection.sendall(
                        json.dumps(response, separators=(",", ":")).encode()
                        + b"\n"
                    )
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
            handled += 1


def main() -> None:
    parser = argparse.ArgumentParser(description="Hardened Codex archive boundary")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    args = parser.parse_args()
    serve(args.root.resolve(), args.socket)
