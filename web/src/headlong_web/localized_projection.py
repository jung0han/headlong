"""Replaceable localized views over append-only assistant records.

The Activity Ledger remains canonical.  This module only overlays human-readable
fields when both a stable record id and the digest of its current source text
match a stored translation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from headlong_web import model_gateway

PROJECTION_SCHEMA = "headlong.localized-pending-projection/v1"
SUPPORTED_LANGUAGES = frozenset({"en", "ko"})
DEFAULT_LANGUAGE = "en"
_HANGUL_RE = re.compile(r"[가-힣]")
_MAX_TITLE = 160
_MAX_CONTENT = 1200


class LocalizedProjectionError(ValueError):
    """A localized view or translation violated the projection contract."""


def configured_language() -> str:
    """Return the configured human-output language."""
    value = os.environ.get("HEADLONG_ASSISTANT_LANGUAGE", DEFAULT_LANGUAGE)
    language = value.strip().lower().split("-", 1)[0]
    if language not in SUPPORTED_LANGUAGES:
        raise LocalizedProjectionError(
            "HEADLONG_ASSISTANT_LANGUAGE must be en or ko"
        )
    return language


def human_output_instruction(language: str | None = None) -> str:
    """Return a compact model instruction for every human-readable field."""
    selected = _language(language)
    if selected == "ko":
        return (
            "Write all human-readable fields in Korean. Preserve code identifiers, "
            "commands, file paths, URLs, model names, and issue IDs verbatim."
        )
    return "Write all human-readable fields in English."


def localize_items(
    identity_dir: Path,
    language: str,
    kind: str,
    items: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Overlay matching translations without mutating the supplied records."""
    selected = _language(language)
    item_kind = _kind(kind)
    translations = _read(identity_dir, selected)
    output: list[dict[str, Any]] = []
    for source in items:
        if not isinstance(source, dict):
            raise LocalizedProjectionError("localized source item must be an object")
        item = deepcopy(source)
        if selected == "ko":
            default_title = _korean_title(item_kind, item)
            if default_title:
                item["title"] = default_title
        item_id = _item_id(item_kind, source)
        stored = translations.get(item_id)
        if (
            stored is not None
            and stored.get("kind") == item_kind
            and stored.get("source_digest") == source_digest(item_kind, source)
        ):
            title = stored.get("title")
            content = stored.get("content")
            if isinstance(title, str) and title:
                item["title"] = title
            if isinstance(content, str) and content:
                item["content"] = content
                if item_kind == "archive":
                    item["completion_rationale"] = content
        output.append(item)
    return output


def has_translation(
    identity_dir: Path, language: str, kind: str, item: dict[str, Any]
) -> bool:
    """Return whether the exact current source revision has a stored translation."""
    selected = _language(language)
    item_kind = _kind(kind)
    record = _read(identity_dir, selected).get(_item_id(item_kind, item))
    return bool(
        record
        and record.get("kind") == item_kind
        and record.get("source_digest") == source_digest(item_kind, item)
    )


def translation_record(
    kind: str,
    source: dict[str, Any],
    *,
    content: str,
    title: str = "",
) -> dict[str, str]:
    """Bind translated display fields to one exact source revision."""
    item_kind = _kind(kind)
    clean_content = _text(content, "translated content", _MAX_CONTENT)
    clean_title = _optional_text(title, "translated title", _MAX_TITLE)
    return {
        "kind": item_kind,
        "item_id": _item_id(item_kind, source),
        "source_digest": source_digest(item_kind, source),
        "title": clean_title,
        "content": clean_content,
    }


def write_translations(
    identity_dir: Path, language: str, records: Iterable[dict[str, Any]]
) -> int:
    """Atomically merge validated translations into a replaceable view file."""
    selected = _language(language)
    current = _read(identity_dir, selected)
    changed = 0
    for raw in records:
        record = _translation(raw, selected)
        if current.get(record["item_id"]) != record:
            current[record["item_id"]] = record
            changed += 1
    if not changed:
        return 0
    path = projection_path(identity_dir, selected)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    payload = {
        "schema": PROJECTION_SCHEMA,
        "language": selected,
        "items": current,
    }
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(
                payload,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise LocalizedProjectionError("cannot write localized projection") from exc
    return changed


def projection_path(identity_dir: Path, language: str) -> Path:
    return (
        identity_dir
        / "assistant"
        / "projections"
        / "localized-pending"
        / f"{_language(language)}.json"
    )


def source_digest(kind: str, item: dict[str, Any]) -> str:
    item_kind = _kind(kind)
    source = {"content": _source_content(item_kind, item)}
    if item_kind == "proposal":
        source["title"] = _optional_text(item.get("title"), "source title", _MAX_TITLE)
    encoded = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def translation_targets(
    kind: str, items: Iterable[dict[str, Any]]
) -> list[dict[str, str]]:
    """Normalize pending source records for one bounded translation call."""
    item_kind = _kind(kind)
    output = []
    for item in items:
        output.append(
            {
                "id": _item_id(item_kind, item),
                "title": (
                    _optional_text(item.get("title"), "source title", _MAX_TITLE)
                    if item_kind == "proposal"
                    else (
                        "Codex Session Archive Candidate"
                        if item_kind == "archive"
                        else ""
                    )
                ),
                "content": _source_content(item_kind, item),
            }
        )
    return output


def translation_result_schema(
    targets: list[dict[str, str]], language: str
) -> model_gateway.StructuredResultSchema:
    """Build an exact-id schema for one translation batch."""
    selected = _language(language)
    ids = [target["id"] for target in targets]
    if not ids or len(ids) != len(set(ids)):
        raise LocalizedProjectionError("translation batch ids must be unique")
    document = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(ids),
                "maxItems": len(ids),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "enum": ids},
                        "title": {"type": "string", "maxLength": _MAX_TITLE},
                        "content": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": _MAX_CONTENT,
                        },
                    },
                    "required": ["id", "title", "content"],
                },
            }
        },
        "required": ["items"],
    }

    def validate(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != {"items"}:
            raise ValueError("translation result must contain exactly items")
        rows = value["items"]
        if not isinstance(rows, list) or len(rows) != len(ids):
            raise ValueError("translation result item count does not match")
        validated: dict[str, dict[str, str]] = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"id", "title", "content"}:
                raise ValueError("translation result fields do not match")
            item_id = row.get("id")
            if item_id not in ids or item_id in validated:
                raise ValueError("translation result ids do not match")
            title = _optional_text(row.get("title"), "translated title", _MAX_TITLE)
            content = _text(row.get("content"), "translated content", _MAX_CONTENT)
            if selected == "ko" and not _HANGUL_RE.search(content):
                raise ValueError("Korean translated content must contain Hangul")
            if selected == "ko" and title and not _HANGUL_RE.search(title):
                raise ValueError("Korean translated title must contain Hangul")
            validated[item_id] = {"id": item_id, "title": title, "content": content}
        if set(validated) != set(ids):
            raise ValueError("translation result omitted an item")
        return {"items": [validated[item_id] for item_id in ids]}

    return model_gateway.StructuredResultSchema(
        name="localized_pending_items", document=document, validate=validate
    )


def records_from_result(
    kind: str,
    sources: list[dict[str, Any]],
    result: dict[str, Any],
) -> list[dict[str, str]]:
    item_kind = _kind(kind)
    by_id = {_item_id(item_kind, source): source for source in sources}
    return [
        translation_record(
            item_kind,
            by_id[row["id"]],
            title=row["title"],
            content=row["content"],
        )
        for row in result["items"]
    ]


def _read(identity_dir: Path, language: str) -> dict[str, dict[str, str]]:
    path = projection_path(identity_dir, language)
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalizedProjectionError("cannot read localized projection") from exc
    if (
        not isinstance(value, dict)
        or set(value) != {"schema", "language", "items"}
        or value.get("schema") != PROJECTION_SCHEMA
        or value.get("language") != language
        or not isinstance(value.get("items"), dict)
    ):
        raise LocalizedProjectionError("unsupported localized projection")
    output = {}
    for item_id, raw in value["items"].items():
        record = _translation(raw, language)
        if item_id != record["item_id"]:
            raise LocalizedProjectionError("localized projection id does not match")
        output[item_id] = record
    return output


def _translation(value: Any, language: str) -> dict[str, str]:
    required = {"kind", "item_id", "source_digest", "title", "content"}
    if not isinstance(value, dict) or set(value) != required:
        raise LocalizedProjectionError("localized translation fields do not match")
    record = {
        "kind": _kind(value.get("kind")),
        "item_id": _text(value.get("item_id"), "item id", 200),
        "source_digest": _text(value.get("source_digest"), "source digest", 64),
        "title": _optional_text(value.get("title"), "translated title", _MAX_TITLE),
        "content": _text(value.get("content"), "translated content", _MAX_CONTENT),
    }
    if not re.fullmatch(r"[0-9a-f]{64}", record["source_digest"]):
        raise LocalizedProjectionError("localized source digest is invalid")
    if language == "ko" and not _HANGUL_RE.search(record["content"]):
        raise LocalizedProjectionError("Korean translated content must contain Hangul")
    if language == "ko" and record["title"] and not _HANGUL_RE.search(record["title"]):
        raise LocalizedProjectionError("Korean translated title must contain Hangul")
    return record


def _source_content(kind: str, item: dict[str, Any]) -> str:
    field = "completion_rationale" if kind == "archive" else "content"
    return _text(item.get(field), f"{kind} source content", _MAX_CONTENT)


def _item_id(kind: str, item: dict[str, Any]) -> str:
    field = {
        "proposal": "proposal_id",
        "archive": "candidate_id",
        "memory": "event_id",
    }[kind]
    return _text(item.get(field), f"{kind} id", 200)


def _korean_title(kind: str, item: dict[str, Any]) -> str:
    if kind == "archive":
        return "Codex 세션 보관 후보"
    if kind != "proposal":
        return ""
    proposal_type = item.get("proposal_type")
    label = "작업 개선 제안" if proposal_type == "work" else "Observer 개선 제안"
    evidence = {
        "user_correction": "사용자 교정",
        "test_failure": "테스트 실패",
        "tool_failure": "도구 실패",
        "reviewer_finding": "리뷰 지적",
        "observer_failure": "Observer 실패",
        "observer_regression": "Observer 회귀",
        "inferred_pattern": "반복 패턴",
    }.get(str(item.get("evidence_kind")), "근거")
    return f"{evidence}에 따른 {label}"


def _kind(value: Any) -> str:
    if value not in {"proposal", "archive", "memory"}:
        raise LocalizedProjectionError("localized item kind is unsupported")
    return value


def _language(value: str | None) -> str:
    language = (
        configured_language()
        if value is None
        else value.strip().lower().split("-", 1)[0]
    )
    if language not in SUPPORTED_LANGUAGES:
        raise LocalizedProjectionError("localized language must be en or ko")
    return language


def _text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise LocalizedProjectionError(f"{label} must be a non-empty compact string")
    return value.strip()


def _optional_text(value: Any, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value.strip()) > limit:
        raise LocalizedProjectionError(f"{label} must be a compact string")
    return value.strip()
