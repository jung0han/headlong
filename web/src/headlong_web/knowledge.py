"""Validated Knowledge Scope values shared by assistant domain modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class KnowledgeScopeError(ValueError):
    """A scope would cross a project boundary or has an invalid shape."""


@dataclass(frozen=True, slots=True)
class KnowledgeScope:
    """One global or project-local knowledge boundary."""

    kind: str
    project_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "global":
            if self.project_id is not None:
                raise KnowledgeScopeError("global Knowledge Scope has no project_id")
            return
        if self.kind != "project":
            raise KnowledgeScopeError("Knowledge Scope kind must be global or project")
        if (
            not isinstance(self.project_id, str)
            or not self.project_id.strip()
            or self.project_id != self.project_id.strip()
            or any(char in self.project_id for char in "\r\n")
        ):
            raise KnowledgeScopeError("project Knowledge Scope requires project_id")

    @classmethod
    def global_scope(cls) -> KnowledgeScope:
        return cls("global")

    @classmethod
    def project(cls, project_id: str) -> KnowledgeScope:
        return cls("project", project_id)

    @classmethod
    def parse(
        cls,
        value: KnowledgeScope | Mapping[str, Any] | None,
        *,
        legacy_global: bool = False,
    ) -> KnowledgeScope:
        if isinstance(value, cls):
            return value
        if value is None and legacy_global:
            return cls.global_scope()
        if not isinstance(value, Mapping):
            raise KnowledgeScopeError("Knowledge Scope must be an object")
        fields = set(value)
        kind = value.get("kind")
        if kind == "global" and fields == {"kind"}:
            return cls.global_scope()
        if kind == "project" and fields == {"kind", "project_id"}:
            return cls.project(value.get("project_id"))  # type: ignore[arg-type]
        raise KnowledgeScopeError("Knowledge Scope fields do not match its kind")

    def to_dict(self) -> dict[str, str]:
        if self.kind == "global":
            return {"kind": "global"}
        assert self.project_id is not None
        return {"kind": "project", "project_id": self.project_id}

    def eligible_for(self, project_id: str, *, include_global: bool = True) -> bool:
        return (include_global and self.kind == "global") or (
            self.kind == "project" and self.project_id == project_id
        )
