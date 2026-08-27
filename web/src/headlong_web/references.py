"""Bounded public fetches and the immutable file-backed Reference Store."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error, parse, request


REFERENCE_SCHEMA = "headlong.reference-revision/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
FETCH_TIMEOUT_SECONDS = 10
FETCH_ELAPSED_SECONDS = 15
MAX_RESPONSE_BYTES = 1_000_000
MAX_TEXT_CHARS = 500_000
_CHUNK_BYTES = 64 * 1024
_SOURCE_ID_RE = re.compile(r"^web-[0-9a-f]{20}$")
_REVISION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_MEDIA_TYPES = {"text/html", "text/plain", "application/xhtml+xml"}


class ReferenceError(RuntimeError):
    """A bounded-fetch or Reference Store failure safe to show to the user."""


@dataclass(frozen=True)
class FetchedDocument:
    source_url: str
    media_type: str
    text: str

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReferenceLocator:
    """Address one immutable sanitized Reference revision."""

    source_identity: str
    source_id: str
    revision_id: str
    sha256: str
    schema: str = LOCATOR_SCHEMA
    kind: str = "web_reference"

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": self.schema,
            "kind": self.kind,
            "source_identity": self.source_identity,
            "source_id": self.source_id,
            "revision_id": self.revision_id,
            "sha256": self.sha256,
        }


def canonical_public_url(value: str) -> str:
    """Return a stable HTTP(S) source identity without accepting credentials."""
    if any(ord(char) < 32 for char in value):
        raise ReferenceError("invalid web source URL")
    try:
        parts = parse.urlsplit(value.strip())
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ReferenceError("invalid web source URL") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ReferenceError("web source URL must use http or https")
    if parts.username is not None or parts.password is not None:
        raise ReferenceError("authenticated web source URLs are not allowed")
    try:
        host = parts.hostname.lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ReferenceError("invalid web source host") from exc
    if port is not None and port < 1:
        raise ReferenceError("invalid web source port")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ReferenceError("web source target is not public")
    if ":" in host:
        host = f"[{host}]"
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path or "/"
    return parse.urlunsplit((parts.scheme.lower(), netloc, path, parts.query, ""))


def source_id(url: str) -> str:
    canonical = canonical_public_url(url)
    return "web-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]


def validate_public_target(url: str) -> None:
    """Resolve the initial target and reject any non-public answer."""
    parts = parse.urlsplit(canonical_public_url(url))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ReferenceError("web source host could not be resolved") from exc
    addresses = {answer[4][0] for answer in answers}
    if not addresses:
        raise ReferenceError("web source host returned no addresses")
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ReferenceError("web source target is not public")
    except ValueError as exc:
        raise ReferenceError("web source host returned an invalid address") from exc


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _default_opener() -> request.OpenerDirector:
    # Redirect validation/revision behavior belongs to DONGWOO-914. Refusing
    # redirects here keeps this first tracer bullet inside its initial target.
    return request.build_opener(_NoRedirect())


def fetch_public_document(
    value: str,
    *,
    opener: request.OpenerDirector | None = None,
    monotonic=time.monotonic,
) -> FetchedDocument:
    """Fetch one public text document under deterministic protocol limits."""
    url = canonical_public_url(value)
    validate_public_target(url)
    req = request.Request(
        url,
        headers={
            "Accept": "text/html, text/plain;q=0.9, application/xhtml+xml;q=0.8",
            "User-Agent": "HeadLong-Reference-Bridge/0.1",
        },
        method="GET",
    )
    started = monotonic()
    try:
        response = (opener or _default_opener()).open(
            req, timeout=FETCH_TIMEOUT_SECONDS
        )
        with response:
            status = int(getattr(response, "status", response.getcode()))
            if status in {401, 403, 407}:
                raise ReferenceError("authenticated web content is not allowed")
            if status < 200 or status >= 300:
                raise ReferenceError(f"web source returned HTTP {status}")
            media_type, charset = _content_type(response.headers)
            if media_type not in _SUPPORTED_MEDIA_TYPES:
                raise ReferenceError(f"unsupported web content type: {media_type}")
            length = response.headers.get("content-length")
            if length is not None:
                try:
                    parsed_length = int(length)
                    if parsed_length < 0:
                        raise ValueError
                    if parsed_length > MAX_RESPONSE_BYTES:
                        raise ReferenceError("web source exceeds the response size limit")
                except ValueError as exc:
                    raise ReferenceError("web source returned an invalid content length") from exc
            body = bytearray()
            while True:
                if monotonic() - started > FETCH_ELAPSED_SECONDS:
                    raise ReferenceError("web source exceeded the elapsed-time limit")
                chunk = response.read(
                    min(_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body))
                )
                if monotonic() - started > FETCH_ELAPSED_SECONDS:
                    raise ReferenceError("web source exceeded the elapsed-time limit")
                if not chunk:
                    break
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ReferenceError("web source exceeds the response size limit")
    except ReferenceError:
        raise
    except error.HTTPError as exc:
        if exc.code in {401, 403, 407}:
            raise ReferenceError("authenticated web content is not allowed") from exc
        if 300 <= exc.code < 400:
            raise ReferenceError("web source redirects are not followed") from exc
        raise ReferenceError(f"web source returned HTTP {exc.code}") from exc
    except (error.URLError, OSError, TimeoutError) as exc:
        raise ReferenceError("web source fetch failed") from exc

    try:
        decoded = bytes(body).decode(charset or "utf-8", errors="replace")
    except LookupError as exc:
        raise ReferenceError("web source declared an unsupported charset") from exc
    text = sanitize_document(decoded, media_type)
    if not text:
        raise ReferenceError("web source contained no usable text")
    if len(text) > MAX_TEXT_CHARS:
        raise ReferenceError("web source exceeds the decoded-text limit")
    return FetchedDocument(source_url=url, media_type=media_type, text=text)


def _content_type(headers: Any) -> tuple[str, str | None]:
    if hasattr(headers, "get_content_type"):
        return headers.get_content_type().lower(), headers.get_content_charset()
    raw = str(headers.get("content-type") or "").strip()
    if not raw:
        return "application/octet-stream", None
    pieces = [piece.strip() for piece in raw.split(";")]
    charset = None
    for piece in pieces[1:]:
        if piece.lower().startswith("charset="):
            charset = piece.split("=", 1)[1].strip('"\' ')
    return pieces[0].lower(), charset


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "canvas", "template"}
    _BLOCK = {
        "article", "aside", "blockquote", "br", "div", "footer", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "li", "main", "nav", "p", "pre",
        "section", "table", "td", "th", "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in self._SKIP:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def sanitize_document(text: str, media_type: str) -> str:
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = "".join(char for char in text if char in "\n\t" or char.isprintable())
    lines = [" ".join(line.split()) for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def reference_root(identity_dir: Path) -> Path:
    return identity_dir / "assistant" / "references"


def read_reference(
    identity_dir: Path, source: str, revision: str, *, include_text: bool
) -> dict[str, Any] | None:
    if not _SOURCE_ID_RE.fullmatch(source) or not _REVISION_ID_RE.fullmatch(revision):
        return None
    base = reference_root(identity_dir).resolve()
    revision_dir = (base / source / revision).resolve()
    if revision_dir != base and not revision_dir.is_relative_to(base):
        return None
    try:
        metadata = json.loads(
            (revision_dir / "metadata.json").read_text(encoding="utf-8")
        )
        _validate_metadata(metadata, source, revision)
        if not (revision_dir / "content.txt").is_file():
            return None
        if include_text:
            text = (revision_dir / "content.txt").read_text(encoding="utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != metadata.get("content_digest"):
                raise ReferenceError("Reference revision content digest does not match")
            metadata["text"] = text
        return metadata
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReferenceError("cannot read Reference revision") from exc


def list_references(identity_dir: Path) -> list[dict[str, Any]]:
    base = reference_root(identity_dir)
    if not base.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for metadata_path in sorted(base.glob("web-*/*/metadata.json")):
        source = metadata_path.parent.parent.name
        revision = metadata_path.parent.name
        item = read_reference(identity_dir, source, revision, include_text=False)
        if item is not None:
            result.append(item)
    result.sort(key=lambda item: (item["fetched_at"], item["source_id"]), reverse=True)
    return result


def store_reference(
    identity_dir: Path,
    document: FetchedDocument,
    *,
    fetched_at: str,
    title: str,
    summary: str,
) -> tuple[dict[str, Any], bool]:
    sid = source_id(document.source_url)
    digest = document.digest
    existing = read_reference(identity_dir, sid, digest, include_text=False)
    if existing is not None:
        return existing, False
    locator = ReferenceLocator(document.source_url, sid, digest, digest)
    metadata = {
        "schema": REFERENCE_SCHEMA,
        "source_id": sid,
        "source_url": document.source_url,
        "revision_id": digest,
        "content_digest": digest,
        "fetched_at": fetched_at,
        "media_type": document.media_type,
        "title": title,
        "summary": summary,
        "evidence_locator": locator.to_dict(),
    }
    source_dir = reference_root(identity_dir) / sid
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / digest
    temp = source_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temp.mkdir()
    try:
        _durable_write(temp / "content.txt", document.text)
        encoded_metadata = (
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        _durable_write(temp / "metadata.json", encoded_metadata)
        os.rename(temp, target)
        _fsync_dir(source_dir)
    except FileExistsError:
        shutil.rmtree(temp, ignore_errors=True)
        existing = read_reference(identity_dir, sid, digest, include_text=False)
        if existing is None:
            raise ReferenceError("Reference revision race did not produce a revision")
        return existing, False
    except OSError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise ReferenceError("cannot store Reference revision") from exc
    return metadata, True


def _validate_metadata(metadata: Any, source: str, revision: str) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("Reference metadata is not an object")
    locator = metadata.get("evidence_locator")
    if (
        metadata.get("schema") != REFERENCE_SCHEMA
        or metadata.get("source_id") != source
        or metadata.get("revision_id") != revision
        or metadata.get("content_digest") != revision
        or not isinstance(metadata.get("source_url"), str)
        or source_id(metadata["source_url"]) != source
        or not isinstance(metadata.get("fetched_at"), str)
        or metadata.get("media_type") not in _SUPPORTED_MEDIA_TYPES
        or not isinstance(metadata.get("title"), str)
        or not isinstance(metadata.get("summary"), str)
        or not isinstance(locator, dict)
        or locator
        != ReferenceLocator(metadata["source_url"], source, revision, revision).to_dict()
    ):
        raise ValueError("Reference metadata does not match its revision")


def _durable_write(path: Path, text: str) -> None:
    with path.open("x", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        # The revision files themselves are already fsynced; some filesystems
        # do not permit syncing directory descriptors.
        pass
