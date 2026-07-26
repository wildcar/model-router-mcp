"""Codex CLI adapter — runs `codex exec` as a subprocess.

Builds the command the way telecodex_bot/runner.py does (bypass-approvals, --cd,
--skip-git-repo-check, --json), runs one-shot to completion, and parses the
JSON-lines stdout to aggregate the assistant text and token usage.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
from pathlib import Path
from typing import Any

from router_mcp.adapters._subprocess import resolve_executable, run_subprocess, validate_cwd
from router_mcp.adapters.base import ProviderAdapter
from router_mcp.domain import progress
from router_mcp.domain.errors import ProviderError
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.registry.models import RegistryModel

logger = logging.getLogger(__name__)


class CodexCliAdapter(ProviderAdapter):
    name = "codex-cli"
    env_var = "CODEX_COMMAND"
    supported_modalities = {Modality.CLI}
    SUPPORTED_PARAMS = {Modality.CLI: ("cwd", "timeout_sec")}

    def __init__(
        self,
        command: str,
        timeout_sec: int,
        allowed_cwd_roots: list[str],
        default_cwd: str | None = None,
    ) -> None:
        self._argv = shlex.split(command)
        self._timeout = timeout_sec
        self._allowed_roots = allowed_cwd_roots
        self._default_cwd = default_cwd or os.getcwd()

    def is_configured(self) -> bool:
        return bool(self._argv) and resolve_executable(self._argv[0]) is not None

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        prompt = (request.text or "").strip()
        if not prompt:
            raise ProviderError("run_cli requires a non-empty `prompt`")
        cwd = validate_cwd(request.params.get("cwd"), self._allowed_roots, self._default_cwd)
        timeout = int(request.params.get("timeout_sec", self._timeout))
        command = self._build_command(prompt, cwd)

        result = await run_subprocess(
            command, cwd=cwd, timeout_sec=timeout, on_line=self._report
        )
        text, token_info = self._parse_stdout(result.stdout)
        if result.return_code != 0 and not text:
            raise ProviderError(
                f"codex exited with code {result.return_code}: {result.stderr[:500]}"
            )
        usage = self._usage_from_token_info(token_info)
        return RouterResponse(
            modality=Modality.CLI,
            provider=self.name,
            model_id=model.model_id,
            usage=usage,
            text=text or None,
            raw_meta={"return_code": result.return_code, "engine": "codex"},
        )

    def _build_command(self, prompt: str, cwd: str) -> list[str]:
        exe = resolve_executable(self._argv[0]) or self._argv[0]
        base = [exe, *self._argv[1:]]
        if Path(self._argv[0]).name == "codex":
            base = [exe, "--dangerously-bypass-approvals-and-sandbox", "--cd", cwd, *self._argv[1:]]
            if len(base) >= 5 and base[4] == "exec":
                base = [*base[:5], "--skip-git-repo-check", *base[5:]]
        if "--json" not in base:
            base.append("--json")
        return [*base, "--", prompt]

    @staticmethod
    def _report(line: str) -> None:
        """Turn one stdout line into a timeline step, while the run is going.

        Deltas are skipped on purpose: a token-by-token feed would bury the
        events an operator is actually watching for (which command ran, which
        file was touched) under thousands of one-character steps.
        """
        line = line.strip()
        if not line or not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        etype = str(event.get("type", ""))
        if etype == "item.completed" and isinstance(event.get("item"), dict):
            item = event["item"]
            kind = str(item.get("type") or "item")
            title = (
                item.get("command")
                or item.get("path")
                or (item.get("text") or "")[:120]
                or kind
            )
            progress.emit("cli", f"{kind}: {title}")
        elif etype == "event_msg" and isinstance(event.get("payload"), dict):
            body = event["payload"]
            btype = str(body.get("type") or "")
            if btype == "token_count" and isinstance(body.get("info"), dict):
                info = body["info"]
                block = info.get("total_token_usage") or info.get("last_token_usage") or {}
                if isinstance(block, dict):
                    progress.emit(
                        "cli",
                        f"токенов: {block.get('total_tokens') or 0}",
                        prompt=block.get("input_tokens"),
                        completion=block.get("output_tokens"),
                    )
            elif btype in ("agent_message", "task_complete"):
                progress.emit("cli", "ответ агента получен")
            elif btype == "exec_command_begin":
                command = body.get("command")
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                progress.emit("cli", f"команда: {str(command or '')[:120]}")
        elif etype == "turn.completed":
            progress.emit("cli", "ход завершён")

    @staticmethod
    def _parse_stdout(stdout: str) -> tuple[str, dict[str, Any] | None]:
        """Aggregate assistant text (deltas + snapshots) and token usage."""
        text = ""
        token_info: dict[str, Any] | None = None
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            etype = str(event.get("type", ""))
            # token usage
            if etype == "turn.completed" and isinstance(event.get("usage"), dict):
                token_info = {"last_token_usage": event["usage"]}
            elif etype == "event_msg" and isinstance(event.get("payload"), dict):
                body = event["payload"]
                if body.get("type") == "token_count" and isinstance(body.get("info"), dict):
                    token_info = body["info"]
                elif body.get("type") in ("agent_message", "task_complete"):
                    msg = body.get("message") or body.get("last_agent_message")
                    if isinstance(msg, str) and msg.strip():
                        text = CodexCliAdapter._merge(text, msg.strip())
            # streamed deltas
            elif etype in ("item.message.delta", "message.delta", "response.output_text.delta"):
                delta = event.get("delta") or event.get("text")
                if isinstance(delta, str):
                    text += delta
            # completed item snapshot
            elif etype == "item.completed" and isinstance(event.get("item"), dict):
                item = event["item"]
                if item.get("type") == "agent_message":
                    msg = item.get("text")
                    if isinstance(msg, str) and msg.strip():
                        text = CodexCliAdapter._merge(text, msg.strip())
        return text.strip(), token_info

    @staticmethod
    def _merge(current: str, snapshot: str) -> str:
        if not current or snapshot.startswith(current):
            return snapshot
        if current.startswith(snapshot):
            return current
        for overlap in range(min(len(current), len(snapshot)), 0, -1):
            if current.endswith(snapshot[:overlap]):
                return current + snapshot[overlap:]
        return snapshot

    @staticmethod
    def _usage_from_token_info(info: dict[str, Any] | None) -> Usage:
        if not isinstance(info, dict):
            return Usage()
        block = info.get("total_token_usage") or info.get("last_token_usage") or info
        prompt = block.get("input_tokens") if isinstance(block, dict) else None
        completion = block.get("output_tokens") if isinstance(block, dict) else None
        total = block.get("total_tokens") if isinstance(block, dict) else None
        if total is None and (prompt or completion):
            total = (prompt or 0) + (completion or 0)
        return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, raw=info)
