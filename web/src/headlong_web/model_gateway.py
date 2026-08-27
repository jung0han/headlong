"""One bounded model subprocess gateway for Personal Assistant services."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from headlong_web import control, discovery, envfile, operational_health


class ModelGatewayError(RuntimeError):
    """A model could not run or returned a malformed bounded result."""


class ModelResultInvalidError(ModelGatewayError):
    """A provider returned output that cannot be a complete bounded result."""


MAX_STRUCTURED_RESULT_CHARS = 64_000


@dataclass(frozen=True)
class StructuredResultSchema:
    """One caller-owned JSON Schema paired with its local validator."""

    name: str
    document: dict[str, Any]
    validate: Callable[[Any], dict[str, Any]]


class ModelGateway:
    """Own identity env, llm wrapping, subprocess policy, and JSON decoding."""

    def __init__(self, root: Path, identity: discovery.IdentityInfo):
        self.root = root.resolve()
        self.identity = identity

    def complete_text(
        self,
        prompt: str,
        *,
        system: str,
        token_timeout: int,
        operation: str,
        max_chars: int | None = None,
        route_args: tuple[str, ...] = (),
    ) -> str:
        env = self._route_env()
        cmd = control._wrap(
            "llm",
            "--no-stream",
            "-t",
            str(token_timeout),
            "--system-prompt",
            system,
            *route_args,
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
            raise ModelGatewayError(f"{operation} failed to run") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or "model call failed").strip().splitlines()[-1]
            if route_args and "structured result truncated" in detail:
                raise ModelResultInvalidError(f"{operation} failed: {detail}")
            raise ModelGatewayError(f"{operation} failed: {detail}")
        output = proc.stdout.strip()
        if not output or (max_chars is not None and len(output) > max_chars):
            raise ModelResultInvalidError(
                f"{operation} is empty or exceeds compact limits"
            )
        return output

    def complete_structured(
        self,
        prompt: str,
        *,
        system: str,
        token_timeout: int,
        operation: str,
        schema: StructuredResultSchema,
    ) -> dict[str, Any]:
        """Return one locally validated result using the route's best schema mode."""
        mode = "unknown"
        schema_path: Path | None = None
        try:
            env = self._route_env()
            mode = _structured_mode(env)
            attempts = 1 if mode == "strict" else 2
            if mode == "strict":
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", suffix=".json", delete=False
                ) as fh:
                    json.dump(schema.document, fh, separators=(",", ":"))
                    schema_path = Path(fh.name)
                route_args = ("--response-schema", schema.name, str(schema_path))
            else:
                route_args = ("--json-object",)

            last_error = "did not match the required schema"
            for attempt in range(attempts):
                retry_system = system
                if mode == "json_object":
                    retry_system += "\nReturn exactly one JSON object and no other text."
                if attempt:
                    retry_system += (
                        "\nThe previous JSON object was invalid. Return exactly one object "
                        "that satisfies the requested contract."
                    )
                try:
                    output = self.complete_text(
                        prompt,
                        system=retry_system,
                        token_timeout=token_timeout,
                        operation=operation,
                        max_chars=MAX_STRUCTURED_RESULT_CHARS,
                        route_args=route_args,
                    )
                    value = json.loads(output)
                    if not isinstance(value, dict):
                        raise ValueError("result is not an object")
                    validated = schema.validate(value)
                    self._record_structured(mode=mode, success=True)
                    return validated
                except json.JSONDecodeError:
                    last_error = "returned invalid JSON"
                except ValueError as exc:
                    last_error = str(exc) or last_error
                except ModelResultInvalidError as exc:
                    last_error = str(exc)
            raise ModelGatewayError(f"{operation} {last_error}")
        except ModelGatewayError:
            self._record_structured(
                mode=mode, success=False, error_code="invalid_result"
            )
            raise
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

    def _record_structured(
        self, *, mode: str, success: bool, error_code: str | None = None
    ) -> None:
        try:
            operational_health.record_structured_result(
                self.identity.path,
                mode=mode,
                success=success,
                error_code=error_code,
            )
        except OSError:
            # The result contract stays authoritative; health is diagnostic.
            pass

    def _route_env(self) -> dict[str, str]:
        """Resolve route configuration with the same root/identity precedence as llm."""
        env = control.identity_env(self.identity, self.root)
        inherited = set(os.environ)
        root_values = dict(envfile.parse_env_file(self.root / ".env"))
        identity_values = dict(envfile.parse_env_file(self.identity.path / ".env"))
        for key, value in root_values.items():
            if key not in inherited:
                env[key] = value
        for key, value in identity_values.items():
            if key not in inherited:
                env[key] = value
        return env


def _structured_mode(env: dict[str, str]) -> str:
    provider = env.get("LLM_PROVIDER", "").strip().lower()
    if provider not in {"openai", "openrouter", "opencode-go"}:
        raise ModelGatewayError(
            "Structured Model Results require an OpenAI-compatible model route"
        )
    configured = env.get("LLM_STRUCTURED_OUTPUT_MODE", "").strip().lower()
    if configured in {"strict", "json_object"}:
        return configured
    if configured:
        raise ModelGatewayError(
            "LLM_STRUCTURED_OUTPUT_MODE must be strict or json_object"
        )
    # An OpenAI-compatible wire format does not prove that the routed model
    # implements strict schemas. Stay on the compatibility path until the
    # operator or a capability probe records explicit support.
    return "json_object"
