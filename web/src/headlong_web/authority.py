"""Actor-inaccessible, signed append boundary for user authority events."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any


AUTHORITY_SCHEMA = "headlong.authority-journal/v1"
PROTECTED_EVENT_TYPES = frozenset(
    {
        "memory-activated",
        "proposal-review",
        "observation-evaluation",
        "active-memory-evaluation",
        "work-improvement-proposal",
        "observer-improvement-proposal",
        "proposal-evidence-update",
        "archive-candidate",
        "archive-candidate-review",
    }
)


class AuthorityJournalError(RuntimeError):
    """The protected journal failed verification or durable append."""


class AuthorityJournal:
    """Signed event journal stored outside the Observer actor work area."""

    def __init__(self, root: Path, identity_id: str):
        name = hashlib.sha256(identity_id.encode()).hexdigest()[:24]
        self.directory = root.resolve() / ".assistant-authority" / name
        self.key_file = self.directory / "signing.key"
        self.events_file = self.directory / "events.jsonl"
        self.marker_file = self.directory / "initialized"
        self.lock_file = self.directory / ".lock"

    def initialize(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        with self.lock_file.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.marker_file.exists():
                self._key()
                self.read()
                return
            self._key()
            # Unsigned trajectory rows are never migrated: accepting their
            # claimed caller fields would recreate the authority-forgery bug.
            self._write_marker()

    def append(self, event: dict[str, Any]) -> None:
        if event.get("type") not in PROTECTED_EVENT_TYPES:
            raise AuthorityJournalError("event does not belong in authority journal")
        with self.lock_file.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self._append_locked(event)

    def read(self) -> list[dict[str, Any]]:
        try:
            lines = self.events_file.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise AuthorityJournalError("cannot read authority journal") from exc
        key = self._key()
        previous = "0" * 64
        events: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AuthorityJournalError("authority journal contains invalid JSON") from exc
            if not isinstance(record, dict) or set(record) != {
                "schema",
                "previous_digest",
                "event",
                "signature",
            }:
                raise AuthorityJournalError("authority journal record has invalid fields")
            event = record["event"]
            if (
                record["schema"] != AUTHORITY_SCHEMA
                or record["previous_digest"] != previous
                or not isinstance(event, dict)
                or event.get("type") not in PROTECTED_EVENT_TYPES
            ):
                raise AuthorityJournalError("authority journal chain is invalid")
            payload = _payload(previous, event)
            expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(record["signature"]), expected):
                raise AuthorityJournalError("authority journal signature is invalid")
            previous = hashlib.sha256(line.encode()).hexdigest()
            events.append(event)
        return events

    def _append_locked(self, event: dict[str, Any]) -> None:
        current = self.read()
        previous = "0" * 64
        if current:
            last = self.events_file.read_text(encoding="utf-8").splitlines()[-1]
            previous = hashlib.sha256(last.encode()).hexdigest()
        signature = hmac.new(self._key(), _payload(previous, event), hashlib.sha256).hexdigest()
        line = json.dumps(
            {
                "schema": AUTHORITY_SCHEMA,
                "previous_digest": previous,
                "event": event,
                "signature": signature,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            fd = os.open(self.events_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(line + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise AuthorityJournalError("cannot append authority journal") from exc

    def _key(self) -> bytes:
        try:
            key = self.key_file.read_bytes()
        except FileNotFoundError:
            key = secrets.token_bytes(32)
            try:
                fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                key = self.key_file.read_bytes()
            except OSError as exc:
                raise AuthorityJournalError("cannot create authority signing key") from exc
        except OSError as exc:
            raise AuthorityJournalError("cannot read authority signing key") from exc
        if len(key) != 32:
            raise AuthorityJournalError("authority signing key is invalid")
        return key

    def _write_marker(self) -> None:
        try:
            fd = os.open(self.marker_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(AUTHORITY_SCHEMA + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return
        except OSError as exc:
            raise AuthorityJournalError("cannot initialize authority journal") from exc


def _payload(previous: str, event: dict[str, Any]) -> bytes:
    return json.dumps(
        {"schema": AUTHORITY_SCHEMA, "previous_digest": previous, "event": event},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
