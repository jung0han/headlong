"""Bounded public fetches and the immutable file-backed Reference Store."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import time
import uuid
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from headlong_web.knowledge import KnowledgeScope, KnowledgeScopeError


REFERENCE_SCHEMA = "headlong.reference-revision/v1"
REJECTION_SCHEMA = "headlong.reference-rejection/v1"
SOURCE_HEALTH_SCHEMA = "headlong.web-source-health/v1"
LOCATOR_SCHEMA = "headlong.evidence-locator/v1"
FETCH_TIMEOUT_SECONDS = 10
FETCH_ELAPSED_SECONDS = 15
MAX_REDIRECTS = 3
MAX_RESPONSE_BYTES = 1_000_000
MAX_TEXT_CHARS = 500_000
_CHUNK_BYTES = 64 * 1024
_SOURCE_ID_RE = re.compile(r"^web-[0-9a-f]{20}$")
_REVISION_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_MEDIA_TYPES = {
    "application/json",
    "text/html",
    "text/plain",
    "application/xhtml+xml",
    "application/rss+xml",
    "application/atom+xml",
    "application/xml",
    "text/xml",
}
_MARKUP_MEDIA_TYPES = _SUPPORTED_MEDIA_TYPES - {"application/json", "text/plain"}


class ReferenceError(RuntimeError):
    """A bounded-fetch or Reference Store failure safe to show to the user."""

    def __init__(self, message: str, *, code: str = "reference_error") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FetchedDocument:
    source_url: str
    media_type: str
    text: str
    resolved_url: str | None = None
    links: tuple[str, ...] = ()

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
        raise ReferenceError("invalid web source URL", code="invalid_url")
    try:
        parts = parse.urlsplit(value.strip())
        port = parts.port
    except (UnicodeError, ValueError) as exc:
        raise ReferenceError("invalid web source URL", code="invalid_url") from exc
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ReferenceError("web source URL must use http or https", code="invalid_scheme")
    if parts.username is not None or parts.password is not None:
        raise ReferenceError(
            "authenticated web source URLs are not allowed", code="credentials_in_url"
        )
    try:
        host = parts.hostname.lower().encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ReferenceError("invalid web source host", code="invalid_host") from exc
    if port is not None and port < 1:
        raise ReferenceError("invalid web source port", code="invalid_port")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ReferenceError("web source target is not public", code="private_target")
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


def resolve_public_target(url: str) -> tuple[str, ...]:
    """Resolve a target once and return only validated public addresses."""
    parts = parse.urlsplit(canonical_public_url(url))
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        answers = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ReferenceError(
            "web source host could not be resolved", code="dns_failed"
        ) from exc
    addresses = {answer[4][0] for answer in answers}
    if not addresses:
        raise ReferenceError(
            "web source host returned no addresses", code="dns_no_addresses"
        )
    try:
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ReferenceError(
                "web source target is not public", code="private_target"
            )
    except ValueError as exc:
        raise ReferenceError(
            "web source host returned an invalid address", code="dns_invalid_address"
        ) from exc
    return tuple(sorted(addresses))


def validate_public_target(url: str) -> None:
    """Compatibility guard for callers that only need validation."""
    resolve_public_target(url)


class _ConnectionResponse:
    """Close both a response and its IP-pinned connection."""

    def __init__(
        self, response: http.client.HTTPResponse, connection: http.client.HTTPConnection
    ) -> None:
        self._response = response
        self._connection = connection
        self.status = response.status
        self.headers = response.headers

    def getcode(self) -> int:
        return self.status

    def set_read_timeout(self, seconds: float) -> None:
        if self._connection.sock is not None:
            self._connection.sock.settimeout(seconds)

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def __enter__(self) -> _ConnectionResponse:
        return self

    def __exit__(self, *_args: Any) -> None:
        self._response.close()
        self._connection.close()


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._validated_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._validated_address = address

    def connect(self) -> None:
        sock = socket.create_connection(
            (self._validated_address, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _PinnedOpener:
    """Open a request against the exact addresses validated by the caller."""

    def open(self, req: request.Request, timeout: float) -> _ConnectionResponse:
        addresses = getattr(req, "_headlong_validated_addresses", ())
        if not addresses:
            raise ReferenceError("web source has no validated address", code="dns_failed")
        parts = parse.urlsplit(req.full_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        target = parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
        last_error: Exception | None = None
        for address in addresses:
            connection_cls = (
                _PinnedHTTPSConnection if parts.scheme == "https" else _PinnedHTTPConnection
            )
            connection = connection_cls(parts.hostname or "", port, address, timeout)
            try:
                connection.request("GET", target, headers=dict(req.header_items()))
                return _ConnectionResponse(connection.getresponse(), connection)
            except (OSError, http.client.HTTPException) as exc:
                connection.close()
                last_error = exc
        raise ReferenceError("web source fetch failed", code="fetch_failed") from last_error


def _default_opener() -> _PinnedOpener:
    return _PinnedOpener()


def fetch_public_document(
    value: str,
    *,
    opener: Any | None = None,
    monotonic=time.monotonic,
) -> FetchedDocument:
    """Fetch one public text document under deterministic protocol limits."""
    source_url = canonical_public_url(value)
    url = source_url
    started = monotonic()
    response_opener = opener or _default_opener()
    redirects = 0
    while True:
        _remaining_seconds(started, monotonic)
        addresses = resolve_public_target(url)
        req = request.Request(
            url,
            headers={
                "Accept": (
                    "text/html, text/plain;q=0.9, application/json;q=0.9, "
                    "application/rss+xml;q=0.9, "
                    "application/atom+xml;q=0.9, application/xml;q=0.8, "
                    "application/xhtml+xml;q=0.8"
                ),
                "User-Agent": "HeadLong-Reference-Bridge/0.2",
            },
            method="GET",
        )
        # The production opener connects only to these exact validated
        # addresses. It never performs a second hostname lookup, closing the
        # check-then-connect DNS rebinding gap while preserving Host and TLS SNI.
        req._headlong_validated_addresses = addresses  # type: ignore[attr-defined]
        try:
            response = response_opener.open(
                req, timeout=min(FETCH_TIMEOUT_SECONDS, _remaining_seconds(started, monotonic))
            )
            with response:
                status = int(getattr(response, "status", response.getcode()))
                if status in {401, 403, 407}:
                    raise ReferenceError(
                        "authenticated web content is not allowed",
                        code="authentication_required",
                    )
                if 300 <= status < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise ReferenceError(
                            "web source returned a redirect without a location",
                            code="redirect_missing_location",
                        )
                    if redirects >= MAX_REDIRECTS:
                        raise ReferenceError(
                            "web source exceeded the redirect limit",
                            code="redirect_limit",
                        )
                    redirected = canonical_public_url(parse.urljoin(url, location))
                    if parse.urlsplit(url).scheme == "https" and parse.urlsplit(
                        redirected
                    ).scheme != "https":
                        raise ReferenceError(
                            "web source redirect would downgrade HTTPS",
                            code="redirect_downgrade",
                        )
                    url = redirected
                    redirects += 1
                    continue
                if status < 200 or status >= 300:
                    raise ReferenceError(
                        f"web source returned HTTP {status}", code="http_error"
                    )
                media_type, charset = _content_type(response.headers)
                if media_type not in _SUPPORTED_MEDIA_TYPES:
                    raise ReferenceError(
                        f"unsupported web content type: {media_type}",
                        code="unsupported_content_type",
                    )
                length = response.headers.get("content-length")
                if length is not None:
                    try:
                        parsed_length = int(length)
                        if parsed_length < 0:
                            raise ValueError
                        if parsed_length > MAX_RESPONSE_BYTES:
                            raise ReferenceError(
                                "web source exceeds the response size limit",
                                code="response_too_large",
                            )
                    except ValueError as exc:
                        raise ReferenceError(
                            "web source returned an invalid content length",
                            code="invalid_content_length",
                        ) from exc
                body = _read_bounded_body(response, started, monotonic)
                break
        except ReferenceError:
            raise
        except error.HTTPError as exc:
            if exc.code in {401, 403, 407}:
                raise ReferenceError(
                    "authenticated web content is not allowed",
                    code="authentication_required",
                ) from exc
            if 300 <= exc.code < 400:
                raise ReferenceError(
                    "web source redirect was refused", code="redirect_refused"
                ) from exc
            raise ReferenceError(
                f"web source returned HTTP {exc.code}", code="http_error"
            ) from exc
        except (error.URLError, OSError, TimeoutError, http.client.HTTPException) as exc:
            raise ReferenceError("web source fetch failed", code="fetch_failed") from exc

    try:
        decoded = bytes(body).decode(charset or "utf-8", errors="replace")
    except LookupError as exc:
        raise ReferenceError(
            "web source declared an unsupported charset", code="unsupported_charset"
        ) from exc
    text = sanitize_document(decoded, media_type)
    if not text:
        raise ReferenceError("web source contained no usable text", code="empty_content")
    if len(text) > MAX_TEXT_CHARS:
        raise ReferenceError(
            "web source exceeds the decoded-text limit", code="decoded_text_too_large"
        )
    return FetchedDocument(
        source_url=source_url,
        media_type=media_type,
        text=text,
        resolved_url=url if url != source_url else None,
        links=extract_public_links(decoded, media_type, url),
    )


def _remaining_seconds(started: float, monotonic: Any) -> float:
    remaining = FETCH_ELAPSED_SECONDS - (monotonic() - started)
    if remaining <= 0:
        raise ReferenceError(
            "web source exceeded the elapsed-time limit", code="elapsed_time_limit"
        )
    return remaining


def _read_bounded_body(response: Any, started: float, monotonic: Any) -> bytearray:
    body = bytearray()
    while True:
        remaining = _remaining_seconds(started, monotonic)
        # The pinned production transport shortens the socket timeout before
        # every read. A peer sending one byte at a time therefore cannot renew
        # a per-read timeout forever and exceed the total fetch deadline.
        set_timeout = getattr(response, "set_read_timeout", None)
        if set_timeout is not None:
            set_timeout(min(FETCH_TIMEOUT_SECONDS, remaining))
        chunk = response.read(min(_CHUNK_BYTES, MAX_RESPONSE_BYTES + 1 - len(body)))
        _remaining_seconds(started, monotonic)
        if not chunk:
            return body
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ReferenceError(
                "web source exceeds the response size limit", code="response_too_large"
            )


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


class _LinkExtractor(HTMLParser):
    _SKIP = _TextExtractor._SKIP

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.skip_depth = 0
        self.links: list[str] = []
        self._seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in {"a", "area", "link"}:
            return
        href = next((value for name, value in attrs if name == "href"), None)
        if not href:
            return
        try:
            url = canonical_public_url(parse.urljoin(self.base_url, href))
        except ReferenceError:
            return
        if url not in self._seen:
            self._seen.add(url)
            self.links.append(url)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self.skip_depth:
            self.skip_depth -= 1


def extract_public_links(text: str, media_type: str, base_url: str) -> tuple[str, ...]:
    """Extract stable public HTTP(S) links without resolving or fetching them."""
    if media_type not in {"text/html", "application/xhtml+xml"}:
        return ()
    parser = _LinkExtractor(base_url)
    parser.feed(text)
    parser.close()
    return tuple(parser.links)


def sanitize_document(text: str, media_type: str) -> str:
    if media_type in _MARKUP_MEDIA_TYPES:
        parser = _TextExtractor()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    text = "".join(char for char in text if char in "\n\t" or char.isprintable())
    if media_type == "application/json":
        return text.strip()
    lines = [" ".join(line.split()) for line in text.splitlines()]
    compact: list[str] = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def reference_root(identity_dir: Path) -> Path:
    return identity_dir / "assistant" / "references"


def rejection_root(identity_dir: Path) -> Path:
    return identity_dir / "assistant" / "reference-rejections"


def source_health_root(identity_dir: Path) -> Path:
    return identity_dir / "assistant" / "source-health"


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
        metadata = _validate_metadata(metadata, source, revision)
        if not (revision_dir / "content.txt").is_file():
            return None
        if include_text:
            text = (revision_dir / "content.txt").read_text(encoding="utf-8")
            if hashlib.sha256(text.encode()).hexdigest() != metadata.get("content_digest"):
                raise ReferenceError(
                    "Reference revision content digest does not match",
                    code="storage_failed",
                )
            metadata["text"] = text
        return metadata
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ReferenceError(
            "cannot read Reference revision", code="storage_failed"
        ) from exc


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
    knowledge_scope: KnowledgeScope | dict[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    try:
        scope = KnowledgeScope.parse(knowledge_scope, legacy_global=True)
    except KnowledgeScopeError as exc:
        raise ReferenceError(str(exc), code="invalid_scope") from exc
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
        "knowledge_scope": scope.to_dict(),
        "evidence_locator": locator.to_dict(),
    }
    if document.resolved_url is not None:
        metadata["resolved_url"] = document.resolved_url
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
            raise ReferenceError(
                "Reference revision race did not produce a revision",
                code="storage_failed",
            )
        return existing, False
    except OSError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise ReferenceError(
            "cannot store Reference revision", code="storage_failed"
        ) from exc
    return metadata, True


def read_rejection(
    identity_dir: Path, source: str, digest: str
) -> dict[str, Any] | None:
    if not _SOURCE_ID_RE.fullmatch(source) or not _REVISION_ID_RE.fullmatch(digest):
        return None
    path = rejection_root(identity_dir) / source / digest / "metadata.json"
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ReferenceError("cannot read Reference rejection", code="storage_failed") from exc
    try:
        scope = KnowledgeScope.parse(metadata.get("knowledge_scope"), legacy_global=True)
    except (AttributeError, KnowledgeScopeError) as exc:
        raise ReferenceError("invalid Reference rejection scope", code="storage_failed") from exc
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema") != REJECTION_SCHEMA
        or metadata.get("source_id") != source
        or metadata.get("content_digest") != digest
        or metadata.get("revision_id") != digest
        or source_id(metadata.get("source_url", "")) != source
        or not isinstance(metadata.get("judgment"), str)
        or not isinstance(metadata.get("rejected_at"), str)
    ):
        raise ReferenceError("invalid Reference rejection metadata", code="storage_failed")
    metadata["knowledge_scope"] = scope.to_dict()
    return metadata


def list_rejections(identity_dir: Path) -> list[dict[str, Any]]:
    """List compact rejection evidence without exposing rejected bodies."""
    base = rejection_root(identity_dir)
    if not base.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for metadata_path in sorted(base.glob("web-*/*/metadata.json")):
        source = metadata_path.parent.parent.name
        digest = metadata_path.parent.name
        item = read_rejection(identity_dir, source, digest)
        if item is not None:
            result.append(item)
    result.sort(
        key=lambda item: (item["rejected_at"], item["source_id"]), reverse=True
    )
    return result


def store_rejection(
    identity_dir: Path,
    document: FetchedDocument,
    *,
    rejected_at: str,
    judgment: str,
    knowledge_scope: KnowledgeScope | dict[str, str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Persist only identity, digest, judgment, and locator for rejected text."""
    sid = source_id(document.source_url)
    digest = document.digest
    existing = read_rejection(identity_dir, sid, digest)
    if existing is not None:
        return existing, False
    try:
        scope = KnowledgeScope.parse(knowledge_scope, legacy_global=True)
    except KnowledgeScopeError as exc:
        raise ReferenceError(str(exc), code="invalid_scope") from exc
    metadata = {
        "schema": REJECTION_SCHEMA,
        "source_id": sid,
        "source_url": document.source_url,
        "revision_id": digest,
        "content_digest": digest,
        "rejected_at": rejected_at,
        "judgment": judgment,
        "knowledge_scope": scope.to_dict(),
        "evidence_locator": ReferenceLocator(
            document.source_url, sid, digest, digest, kind="web_reference_rejection"
        ).to_dict(),
    }
    source_dir = rejection_root(identity_dir) / sid
    source_dir.mkdir(parents=True, exist_ok=True)
    target = source_dir / digest
    temp = source_dir / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temp.mkdir()
    try:
        encoded = json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _durable_write(temp / "metadata.json", encoded)
        os.rename(temp, target)
        _fsync_dir(source_dir)
    except FileExistsError:
        shutil.rmtree(temp, ignore_errors=True)
        existing = read_rejection(identity_dir, sid, digest)
        if existing is None:
            raise ReferenceError(
                "Reference rejection race did not produce evidence",
                code="storage_failed",
            )
        return existing, False
    except OSError as exc:
        shutil.rmtree(temp, ignore_errors=True)
        raise ReferenceError(
            "cannot store Reference rejection", code="storage_failed"
        ) from exc
    return metadata, True


def read_source_health(identity_dir: Path) -> list[dict[str, Any]]:
    root = source_health_root(identity_dir)
    if not root.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(root.glob("web-*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result.append(
                {
                    "schema": SOURCE_HEALTH_SCHEMA,
                    "source_id": path.stem,
                    "source_kind": "unknown",
                    "status": "error",
                    "phase": "health",
                    "error_code": "health_corrupt",
                }
            )
            continue
        if (
            not isinstance(value, dict)
            or value.get("schema") != SOURCE_HEALTH_SCHEMA
            or value.get("source_id") != path.stem
        ):
            value = {
                "schema": SOURCE_HEALTH_SCHEMA,
                "source_id": path.stem,
                "source_kind": "unknown",
                "status": "error",
                "phase": "health",
                "error_code": "health_corrupt",
            }
        result.append(value)
    return result


def write_source_health(
    identity_dir: Path,
    *,
    source_id_value: str,
    source_kind: str,
    attempted_at: str,
    status: str,
    phase: str,
    digest: str | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    """Atomically update bounded health without URLs, bodies, or credentials."""
    root = source_health_root(identity_dir)
    target = root / f"{source_id_value}.json"
    try:
        previous = json.loads(target.read_text(encoding="utf-8"))
        if (
            not isinstance(previous, dict)
            or previous.get("schema") != SOURCE_HEALTH_SCHEMA
            or previous.get("source_id") != source_id_value
        ):
            previous = None
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        previous = None
    attempts = int((previous or {}).get("attempts", 0)) + 1
    successes = int((previous or {}).get("successes", 0))
    health: dict[str, Any] = {
        "schema": SOURCE_HEALTH_SCHEMA,
        "source_id": source_id_value,
        "source_kind": source_kind,
        "status": status,
        "phase": phase,
        "attempts": attempts,
        "successes": successes + (1 if status == "healthy" else 0),
        "consecutive_failures": (
            0
            if status == "healthy"
            else int((previous or {}).get("consecutive_failures", 0)) + 1
        ),
        "last_attempt_at": attempted_at,
    }
    if digest is not None:
        health["last_digest"] = digest
    elif previous and "last_digest" in previous:
        health["last_digest"] = previous["last_digest"]
    if status == "healthy":
        health["last_success_at"] = attempted_at
    elif previous and "last_success_at" in previous:
        health["last_success_at"] = previous["last_success_at"]
    if error_code is not None:
        health["error_code"] = error_code[:80]
    root.mkdir(parents=True, exist_ok=True)
    temp = root / f".{source_id_value}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        _durable_write(
            temp, json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        os.replace(temp, target)
        _fsync_dir(root)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise ReferenceError("cannot store web source health", code="storage_failed") from exc
    return health


def _validate_metadata(metadata: Any, source: str, revision: str) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise ValueError("Reference metadata is not an object")
    try:
        scope = KnowledgeScope.parse(metadata.get("knowledge_scope"), legacy_global=True)
    except KnowledgeScopeError as exc:
        raise ValueError("Reference metadata has invalid Knowledge Scope") from exc
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
    metadata["knowledge_scope"] = scope.to_dict()
    return metadata


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
