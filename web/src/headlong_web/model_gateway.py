"""One bounded model subprocess gateway for Personal Assistant services."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from headlong_web import control, discovery


class ModelGatewayError(RuntimeError):
    """A model could not run or returned a malformed bounded result."""


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
    ) -> str:
        env = control.identity_env(self.identity, self.root)
        cmd = control._wrap(
            "llm",
            "--no-stream",
            "-t",
            str(token_timeout),
            "--system-prompt",
            system,
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
            raise ModelGatewayError(f"{operation} failed: {detail}")
        output = proc.stdout.strip()
        if not output or (max_chars is not None and len(output) > max_chars):
            raise ModelGatewayError(f"{operation} is empty or exceeds compact limits")
        return output

    def complete_json(
        self,
        prompt: str,
        *,
        system: str,
        token_timeout: int,
        operation: str,
    ) -> dict[str, Any]:
        output = self.complete_text(
            prompt,
            system=system,
            token_timeout=token_timeout,
            operation=operation,
        )
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ModelGatewayError(f"{operation} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ModelGatewayError(f"{operation} JSON must be an object")
        return value
