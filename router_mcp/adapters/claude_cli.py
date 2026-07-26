"""Claude CLI adapter — runs `claude -p ... --output-format json` one-shot.

Parses the single final JSON object for the result text, token usage, the
CLI-reported total_cost_usd (used as a cost override), and the session id.
The CLI inherits the process environment (so ANTHROPIC_API_KEY / its own config
are picked up).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from typing import Any

from router_mcp.adapters._subprocess import resolve_executable, run_subprocess, validate_cwd
from router_mcp.adapters.base import ProviderAdapter
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)


class ClaudeCliAdapter(ProviderAdapter):
    name = "claude-cli"
    env_var = "CLAUDE_COMMAND"
    supported_modalities = {Modality.CLI}
    SUPPORTED_PARAMS = {Modality.CLI: ("cwd", "timeout_sec", "model")}

    def __init__(
        self,
        command: str,
        timeout_sec: int,
        allowed_cwd_roots: list[str],
        default_cwd: str | None = None,
    ) -> None:
        self._argv = shlex.split(command) or ["claude"]
        self._timeout = timeout_sec
        self._allowed_roots = allowed_cwd_roots
        self._default_cwd = default_cwd or os.getcwd()

    def is_configured(self) -> bool:
        return resolve_executable(self._argv[0]) is not None

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        prompt = (request.text or "").strip()
        if not prompt:
            raise ProviderError("run_cli requires a non-empty `prompt`")
        cwd = validate_cwd(request.params.get("cwd"), self._allowed_roots, self._default_cwd)
        timeout = int(request.params.get("timeout_sec", self._timeout))

        exe = resolve_executable(self._argv[0]) or self._argv[0]
        command = [exe, *self._argv[1:], "-p", prompt, "--output-format", "json",
                   "--dangerously-skip-permissions"]
        requested_model = request.params.get("model")
        if requested_model:
            command += ["--model", str(requested_model)]
        if request.params.get("cwd"):
            command += ["--add-dir", cwd]

        result = await run_subprocess(command, cwd=cwd, timeout_sec=timeout)
        data = self._parse_stdout(result.stdout, result.return_code, result.stderr)

        text = data.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("claude CLI returned no result text")
        usage = self._usage(data.get("usage"))
        cost = data.get("total_cost_usd")
        return RouterResponse(
            modality=Modality.CLI,
            provider=self.name,
            model_id=str(requested_model or model.model_id),
            usage=usage,
            text=text.strip(),
            cost_override_usd=float(cost) if isinstance(cost, (int, float)) else None,
            raw_meta={
                "return_code": result.return_code,
                "engine": "claude",
                "session_id": data.get("session_id"),
            },
        )

    @staticmethod
    def _parse_stdout(stdout: str, return_code: int, stderr: str) -> dict[str, Any]:
        text = stdout.strip()
        if not text:
            raise ProviderError(
                f"claude CLI produced no output (exit {return_code}): {stderr[:500]}"
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            # --output-format json should emit one object; if not, surface raw.
            raise ProviderError(f"claude CLI returned non-JSON output: {text[:500]}") from exc
        if not isinstance(data, dict):
            raise ProviderError("claude CLI returned an unexpected payload")
        if data.get("is_error"):
            raise ProviderError(str(data.get("result") or "claude CLI reported an error"))
        return data

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        prompt = raw.get("input_tokens")
        completion = raw.get("output_tokens")
        total = (prompt or 0) + (completion or 0) if (prompt or completion) else None
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, raw=raw)
