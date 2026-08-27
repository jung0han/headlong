"""Deterministic budgets and breadth-first frontier for public exploration."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode

from headlong_web import references


EXPLORATION_SCHEMA = "headlong.web-exploration/v1"
SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


@dataclass(frozen=True)
class ExplorationLimits:
    max_pages: int = 8
    max_depth: int = 2
    max_elapsed_seconds: float = 60.0
    max_stored_bytes: int = 2_000_000

    def __post_init__(self) -> None:
        if not 1 <= self.max_pages <= 100:
            raise ValueError("exploration max_pages must be between 1 and 100")
        if not 0 <= self.max_depth <= 5:
            raise ValueError("exploration max_depth must be between 0 and 5")
        if not 1 <= self.max_elapsed_seconds <= 3600:
            raise ValueError(
                "exploration max_elapsed_seconds must be between 1 and 3600"
            )
        if not 1 <= self.max_stored_bytes <= 50_000_000:
            raise ValueError(
                "exploration max_stored_bytes must be between 1 and 50000000"
            )

    def to_dict(self) -> dict[str, int | float]:
        return {
            "max_pages": self.max_pages,
            "max_depth": self.max_depth,
            "max_elapsed_seconds": self.max_elapsed_seconds,
            "max_stored_bytes": self.max_stored_bytes,
        }


@dataclass(frozen=True)
class VisitOutcome:
    links: tuple[str, ...] = ()
    fetched: bool = True
    selected: int = 0
    saved: int = 0
    duplicate_content: int = 0
    not_selected: int = 0
    stored_bytes: int = 0
    failure: dict[str, str] | None = None
    stop_reason: str | None = None


@dataclass(frozen=True)
class _Candidate:
    url: str
    depth: int
    search: bool = False


@dataclass
class _Frontier:
    limits: ExplorationLimits
    queue: deque[_Candidate] = field(default_factory=deque)
    seen: set[str] = field(default_factory=set)
    duplicate_urls: int = 0
    depth_limited: bool = False

    def add(self, url: str, depth: int, *, search: bool = False) -> None:
        try:
            canonical = references.canonical_public_url(url)
        except references.ReferenceError:
            return
        if canonical in self.seen:
            self.duplicate_urls += 1
            return
        self.seen.add(canonical)
        self.queue.append(_Candidate(canonical, depth, search))

    def add_links(self, links: tuple[str, ...], depth: int) -> None:
        if depth >= self.limits.max_depth:
            if links:
                self.depth_limited = True
            return
        for link in links:
            self.add(link, depth + 1)


def public_search_url(query: str) -> str:
    query = " ".join(query.split())
    if not query:
        raise ValueError("exploration query must not be empty")
    return SEARCH_ENDPOINT + "?" + urlencode({"q": query})


def run_bounded_exploration(
    query: str,
    *,
    limits: ExplorationLimits,
    visit: Callable[[str, int, bool, int, float], VisitOutcome],
    monotonic: Callable[[], float],
    seed_urls: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Visit a deterministic public frontier and report the exact stop boundary."""
    started = monotonic()
    deadline = started + limits.max_elapsed_seconds
    frontier = _Frontier(limits)
    if seed_urls:
        for url in seed_urls:
            frontier.add(url, 0)
    else:
        frontier.add(public_search_url(query), -1, search=True)
    result: dict[str, Any] = {
        "schema": EXPLORATION_SCHEMA,
        "limits": limits.to_dict(),
        "pages_attempted": 0,
        "pages_fetched": 0,
        "selected": 0,
        "saved": 0,
        "duplicate_urls": 0,
        "duplicate_content": 0,
        "not_selected": 0,
        "stored_bytes": 0,
        "failed": 0,
        "failures": [],
        "status": "ok",
        "stop_reason": "frontier_exhausted",
    }
    while frontier.queue:
        if monotonic() - started >= limits.max_elapsed_seconds:
            result["stop_reason"] = "elapsed_time"
            break
        if result["pages_attempted"] >= limits.max_pages:
            result["stop_reason"] = "page_count"
            break
        candidate = frontier.queue.popleft()
        result["pages_attempted"] += 1
        remaining = limits.max_stored_bytes - result["stored_bytes"]
        outcome = visit(
            candidate.url,
            candidate.depth,
            candidate.search,
            remaining,
            deadline,
        )
        if outcome.fetched:
            result["pages_fetched"] += 1
        if outcome.failure is not None:
            result["failed"] += 1
            result["failures"].append(outcome.failure)
            result["status"] = "degraded"
        for key in ("selected", "saved", "duplicate_content", "not_selected"):
            result[key] += getattr(outcome, key)
        result["stored_bytes"] += outcome.stored_bytes
        frontier.add_links(outcome.links, candidate.depth)
        if outcome.stop_reason is not None:
            result["stop_reason"] = outcome.stop_reason
            break
        if monotonic() - started >= limits.max_elapsed_seconds:
            result["stop_reason"] = "elapsed_time"
            break
        if result["stored_bytes"] >= limits.max_stored_bytes:
            result["stop_reason"] = "stored_bytes"
            break
    else:
        if frontier.depth_limited:
            result["stop_reason"] = "link_depth"
    result["duplicate_urls"] = frontier.duplicate_urls
    result["elapsed_seconds"] = max(0.0, monotonic() - started)
    return result
