"""Telegram as a notification channel: one bot, one or more chats.

The Bot API is a plain HTTPS POST, so this is httpx rather than a bot
framework — the router only ever speaks, it never listens. The chat id is the
operator's own chat with the bot (send it `/start` once, read the id from
`getUpdates`) or a group the bot was added to.
"""

from __future__ import annotations

import html
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Telegram cuts a message at 4096 characters; the body is trimmed well below so
# the log link at the end is never the part that gets lost.
_MAX_TEXT = 3500


def _as_html(lines: list[str]) -> str:
    """The first line bold, the rest as they are, everything escaped.

    HTML parse mode is used only for that one bold line: Markdown parse modes
    make a provider message with an underscore or a bracket a 400 from
    Telegram, and an error about the error is the last thing this needs.
    """
    if not lines:
        return ""
    head, *rest = [html.escape(line) for line in lines]
    return "\n".join([f"<b>{head}</b>", *rest])[:_MAX_TEXT]


class TelegramChannel:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_ids: list[str],
        *,
        api_base: str = "https://api.telegram.org",
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._chat_ids = list(chat_ids)
        self._client = httpx.AsyncClient(
            base_url=f"{api_base.rstrip('/')}/bot{token}",
            timeout=timeout_seconds,
            transport=transport,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "targets": len(self._chat_ids),
            "detail": f"чатов: {len(self._chat_ids)}",
        }

    async def send(self, subject: str, lines: list[str]) -> None:
        """Deliver to every chat; the first chat that fails names the failure.

        `subject` is the e-mail's; Telegram gets the same first line in bold.
        """
        text = _as_html(lines)
        for chat_id in self._chat_ids:
            response = await self._client.post(
                "/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                detail = response.text[:300]
                try:
                    detail = str(response.json().get("description") or detail)
                except ValueError:
                    pass
                raise RuntimeError(f"Telegram chat {chat_id}: HTTP {response.status_code} {detail}")

    async def close(self) -> None:
        await self._client.aclose()
