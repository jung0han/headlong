"""Bounded Hacker News adapter for the native Web Source Bridge."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from headlong_web import references


SOURCE_URL = "https://news.ycombinator.com/"
API_ROOT = "https://hacker-news.firebaseio.com/v0"
MAX_STORIES = 5
MAX_FEED_ENTRIES = 20
MAX_COMMENTS_PER_STORY = 8
MAX_COMMENT_FETCHES_PER_STORY = 12
MAX_COMMENT_DEPTH = 2
MAX_COMMENT_QUEUE = 32
MAX_ARTICLE_CHARS = 40_000
MAX_DISCUSSION_CHARS = 30_000
MAX_FIELD_CHARS = 4_000


@dataclass(frozen=True)
class CollectionFailure:
    """One bounded, non-sensitive failure within an HN collection run."""

    phase: str
    code: str


@dataclass(frozen=True)
class Collection:
    """Ordinary Reference candidates plus isolated source-adapter failures."""

    documents: tuple[references.FetchedDocument, ...]
    failures: tuple[CollectionFailure, ...]
    duplicates: int

    @property
    def digest(self) -> str:
        value = "\n".join(document.digest for document in self.documents)
        return hashlib.sha256(value.encode()).hexdigest()


def canonical_source_url(value: str) -> str:
    """Accept only the public HN front page as a recurring native source."""
    canonical = references.canonical_public_url(value)
    if canonical.rstrip("/") != SOURCE_URL.rstrip("/"):
        raise references.ReferenceError(
            "Hacker News sources must use https://news.ycombinator.com/",
            code="invalid_hacker_news_source",
        )
    return SOURCE_URL


def collect(
    *,
    fetch: Callable[[str], references.FetchedDocument] = references.fetch_public_document,
) -> Collection:
    """Collect a bounded top-story slice as ordinary article/discussion documents.

    Every network read goes through the Web Source Bridge's public, size-limited,
    redirect-limited fetcher. Item and article failures are retained as compact
    health evidence while unrelated candidates continue.
    """
    ids = _json(fetch(f"{API_ROOT}/topstories.json"), expected="story list")
    if not isinstance(ids, list):
        raise references.ReferenceError(
            "Hacker News returned an invalid story list", code="hn_invalid_feed"
        )

    documents: list[references.FetchedDocument] = []
    failures: list[CollectionFailure] = []
    seen_ids: set[int] = set()
    seen_urls: set[str] = set()
    duplicates = 0
    story_ids: list[int] = []
    for value in ids[:MAX_FEED_ENTRIES]:
        if not _valid_id(value):
            failures.append(CollectionFailure("hacker_news_feed", "hn_invalid_item_id"))
            continue
        if value in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(value)
        story_ids.append(value)
        if len(story_ids) == MAX_STORIES:
            break

    for story_id in story_ids:
        try:
            story = _item(fetch, story_id)
        except references.ReferenceError as exc:
            failures.append(CollectionFailure("hacker_news_item", exc.code))
            continue
        if story is None:
            failures.append(CollectionFailure("hacker_news_item", "hn_invalid_item"))
            continue

        discussion_url = f"{SOURCE_URL}item?id={story_id}"
        discussion, comment_failures = _discussion_document(
            story, discussion_url, fetch
        )
        failures.extend(comment_failures)
        documents.append(discussion)
        seen_urls.add(discussion.source_url)

        raw_article_url = story.get("url")
        if not isinstance(raw_article_url, str) or not raw_article_url.strip():
            continue
        try:
            article_url = references.canonical_public_url(raw_article_url)
        except references.ReferenceError as exc:
            failures.append(CollectionFailure("hacker_news_article", exc.code))
            continue
        if article_url in seen_urls:
            duplicates += 1
            continue
        seen_urls.add(article_url)
        try:
            article = fetch(article_url)
        except references.ReferenceError as exc:
            failures.append(CollectionFailure("hacker_news_article", exc.code))
            continue
        documents.append(
            references.FetchedDocument(
                source_url=article.source_url,
                media_type=article.media_type,
                text=article.text[:MAX_ARTICLE_CHARS],
                resolved_url=article.resolved_url,
            )
        )

    return Collection(tuple(documents), tuple(failures), duplicates)


def _discussion_document(
    story: dict,
    discussion_url: str,
    fetch: Callable[[str], references.FetchedDocument],
) -> tuple[references.FetchedDocument, list[CollectionFailure]]:
    lines = [
        f"Hacker News discussion: {_field(story.get('title'))}",
        f"Story id: {story['id']}",
        f"Author: {_field(story.get('by'))}",
        f"Score: {_integer_field(story.get('score'))}",
        f"Comment count: {_integer_field(story.get('descendants'))}",
    ]
    story_text = _html_field(story.get("text"))
    if story_text:
        lines.extend(("", "Story text:", story_text))
    lines.extend(("", "Discussion excerpts:"))

    failures: list[CollectionFailure] = []
    queue = [
        (item_id, 1)
        for item_id in _ids(story.get("kids"), limit=MAX_COMMENT_QUEUE)
    ]
    visited: set[int] = set()
    comments = 0
    fetches = 0
    while (
        queue
        and comments < MAX_COMMENTS_PER_STORY
        and fetches < MAX_COMMENT_FETCHES_PER_STORY
    ):
        comment_id, depth = queue.pop(0)
        if comment_id in visited:
            continue
        visited.add(comment_id)
        fetches += 1
        try:
            comment = _item(fetch, comment_id, expected_type="comment")
        except references.ReferenceError as exc:
            failures.append(CollectionFailure("hacker_news_discussion", exc.code))
            continue
        if comment is None:
            failures.append(
                CollectionFailure("hacker_news_discussion", "hn_invalid_comment")
            )
            continue
        text = _html_field(comment.get("text"))
        if text:
            comments += 1
            lines.append(f"- depth={depth} by={_field(comment.get('by'))}: {text}")
        if depth < MAX_COMMENT_DEPTH:
            remaining = MAX_COMMENT_QUEUE - len(queue)
            queue.extend(
                (item_id, depth + 1)
                for item_id in _ids(comment.get("kids"), limit=max(0, remaining))
            )

    text = "\n".join(lines)[:MAX_DISCUSSION_CHARS].strip()
    return (
        references.FetchedDocument(discussion_url, "text/plain", text),
        failures,
    )


def _item(
    fetch: Callable[[str], references.FetchedDocument],
    item_id: int,
    *,
    expected_type: str | None = None,
) -> dict | None:
    value = _json(fetch(f"{API_ROOT}/item/{item_id}.json"), expected="item")
    if (
        not isinstance(value, dict)
        or value.get("id") != item_id
        or value.get("deleted") is True
        or value.get("dead") is True
        or not isinstance(value.get("type"), str)
        or (expected_type is not None and value.get("type") != expected_type)
    ):
        return None
    if expected_type is None and value["type"] not in {"story", "job", "poll"}:
        return None
    return value


def _json(document: references.FetchedDocument, *, expected: str) -> object:
    if document.media_type != "application/json":
        raise references.ReferenceError(
            f"Hacker News {expected} was not JSON", code="hn_content_type_mismatch"
        )
    try:
        return json.loads(document.text)
    except json.JSONDecodeError as exc:
        raise references.ReferenceError(
            f"Hacker News returned invalid {expected}", code="hn_invalid_json"
        ) from exc


def _valid_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _ids(value: object, *, limit: int) -> list[int]:
    if not isinstance(value, list):
        return []
    return [item for item in value if _valid_id(item)][:limit]


def _html_field(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return references.sanitize_document(value[:MAX_FIELD_CHARS], "text/html")


def _field(value: object) -> str:
    if not isinstance(value, str):
        return "unknown"
    return " ".join(value[:MAX_FIELD_CHARS].split()) or "unknown"


def _integer_field(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "unknown"
