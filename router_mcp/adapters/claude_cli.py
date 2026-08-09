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
from router_mcp.domain import progress
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.task import Task
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)


class ClaudeCliAdapter(ProviderAdapter):
    name = "claude-cli"
    env_var = "CLAUDE_COMMAND"
    supported_tasks = {Task.CLI}
    SUPPORTED_PARAMS = {Task.CLI: ("cwd", "timeout_sec", "model", "web_search")}

    def __init__(
        self,
        command: str,
        timeout_sec: int,
        allowed_cwd_roots: list[str],
        default_cwd: str | None = None,
        subscription: bool = True,
    ) -> None:
        self._argv = shlex.split(command) or ["claude"]
        self._timeout = timeout_sec
        self._allowed_roots = allowed_cwd_roots
        self._default_cwd = default_cwd or os.getcwd()
        # Under a subscription login the CLI still reports `total_cost_usd` —
        # what the work would have cost on the API. That is useful information
        # and fake money, and adding it to a real total inflates the bill. The
        # CLI does not say which mode it is in, so this is configuration
        # (`CLAUDE_CLI_SUBSCRIPTION`), defaulting to the way this host runs it.
        self._subscription = subscription

    def is_configured(self) -> bool:
        return resolve_executable(self._argv[0]) is not None

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        prompt = (request.text or "").strip()
        if not prompt:
            raise ProviderError("run_cli requires a non-empty `prompt`")
        cwd = validate_cwd(request.params.get("cwd"), self._allowed_roots, self._default_cwd)
        timeout = int(request.params.get("timeout_sec", self._timeout))
        command = self._build_command(request, prompt, cwd)
        requested_model = request.params.get("model")

        result = await run_subprocess(
            command, cwd=cwd, timeout_sec=timeout, on_line=self._report
        )
        data = self._parse_stdout(result.stdout, result.return_code, result.stderr)

        text = data.get("result")
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("claude CLI returned no result text")
        usage = self._usage(data.get("usage"))
        cost = data.get("total_cost_usd")
        raw_meta: dict[str, Any] = {
            "return_code": result.return_code,
            "engine": "claude",
            "session_id": data.get("session_id"),
        }
        if request.params.get("web_search"):
            raw_meta["web_search"] = True
        return RouterResponse(
            task=Task.CLI,
            provider=self.name,
            model_id=str(requested_model or model.model_id),
            usage=usage,
            text=text.strip(),
            cost_override_usd=float(cost) if isinstance(cost, (int, float)) else None,
            cost_is_notional=self._subscription,
            raw_meta=raw_meta,
        )

    def _build_command(self, request: RouterRequest, prompt: str, cwd: str) -> list[str]:
        exe = resolve_executable(self._argv[0]) or self._argv[0]
        # `stream-json` rather than `json`: the same final object arrives, but it
        # is preceded by one line per turn, which is what makes a long run
        # legible while it runs. `--verbose` is what the CLI requires to stream
        # in print mode.
        command = [exe, *self._argv[1:], "-p", prompt,
                   "--output-format", "stream-json", "--verbose",
                   "--dangerously-skip-permissions"]
        requested_model = request.params.get("model")
        if requested_model:
            command += ["--model", str(requested_model)]
        if request.params.get("cwd"):
            command += ["--add-dir", cwd]
        if request.params.get("web_search"):
            # Explicitly whitelisted, not merely un-forbidden: today the
            # skip-permissions flag already lets WebSearch run, but the
            # whitelist keeps working the day that flag is tightened. An
            # unchecked box stays out of the call — it does not forbid the
            # model to search on its own.
            command += ["--allowedTools", "WebSearch"]
        return command

    @staticmethod
    def _report(line: str) -> None:
        """One timeline step per streamed line, while the run is going."""
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        etype = str(event.get("type") or "")
        if etype == "system" and event.get("subtype") == "init":
            progress.emit("cli", f"сессия {str(event.get('session_id') or '')[:8]}")
        elif etype == "system" and event.get("subtype") == "api_retry":
            progress.emit(
                "cli",
                f"повтор запроса к API ({event.get('error_status')})",
                attempt=event.get("attempt"),
            )
        elif etype == "assistant":
            message = event.get("message")
            blocks = message.get("content") if isinstance(message, dict) else None
            for block in blocks or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    progress.emit("cli", f"инструмент: {block.get('name')}")
                elif block.get("type") == "text" and str(block.get("text") or "").strip():
                    progress.emit("cli", "текст от агента")
        elif etype == "result":
            progress.emit("cli", "результат получен")

    @staticmethod
    def _parse_stdout(stdout: str, return_code: int, stderr: str) -> dict[str, Any]:
        text = stdout.strip()
        if not text:
            raise ProviderError(
                f"claude CLI produced no output (exit {return_code}): {stderr[:500]}"
            )
        data = ClaudeCliAdapter._final_object(text)
        if data is None:
            raise ProviderError(f"claude CLI returned non-JSON output: {text[:500]}")
        if not isinstance(data, dict):
            raise ProviderError("claude CLI returned an unexpected payload")
        if data.get("is_error"):
            raise ProviderError(str(data.get("result") or "claude CLI reported an error"))
        return data

    @staticmethod
    def _final_object(text: str) -> dict[str, Any] | None:
        """The result object, from either output format.

        Under `stream-json` it is the line whose `type` is `result`; the whole
        output is still parsed as one object first, so an older CLI (or a
        `--output-format json` left in `CLAUDE_COMMAND`) keeps working.
        """
        try:
            whole = json.loads(text)
        except json.JSONDecodeError:
            whole = None
        if isinstance(whole, dict):
            return whole
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                return event
        return None

    @staticmethod
    def _usage(raw: Any) -> Usage:
        if not isinstance(raw, dict):
            return Usage()
        # Same shape as the Anthropic API: cache reads and writes sit *outside*
        # `input_tokens`, while Usage's contract is that `prompt_tokens` is the
        # whole prompt. A CLI turn is mostly cache, so leaving them out made a
        # long session look like it had sent almost nothing.
        cache_read = raw.get("cache_read_input_tokens")
        cache_write = raw.get("cache_creation_input_tokens")
        prompt = (
            (raw.get("input_tokens") or 0) + (cache_read or 0) + (cache_write or 0)
        ) or None
        completion = raw.get("output_tokens")
        total = (prompt or 0) + (completion or 0) if (prompt or completion) else None
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            raw=raw,
        )
