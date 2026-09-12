"""E-mail as a notification channel: plain SMTP from the standard library.

Blocking `smtplib` runs in a worker thread so the event loop — and the run the
notification is about — never waits on a mail server. No third-party client:
the message is a subject and a dozen lines of text.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EmailChannel:
    name = "email"

    def __init__(
        self,
        recipients: list[str],
        *,
        host: str,
        port: int = 587,
        user: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        sender: str | None = None,
        timeout_seconds: float = 15.0,
        # Tests hand in a fake; production leaves the default.
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._recipients = list(recipients)
        self._host = host
        self._port = port
        self._user = user
        self._password = password
        self._starttls = starttls
        self._sender = sender or user or f"model-router@{host}"
        self._timeout = timeout_seconds
        self._smtp_factory = smtp_factory or smtplib.SMTP

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "targets": len(self._recipients),
            "detail": f"адресов: {len(self._recipients)} · {self._host}:{self._port}",
        }

    def _send_blocking(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._sender
        msg["To"] = ", ".join(self._recipients)
        msg.set_content(body)
        with self._smtp_factory(self._host, self._port, timeout=self._timeout) as smtp:
            if self._starttls:
                smtp.starttls()
            if self._user and self._password:
                smtp.login(self._user, self._password)
            smtp.send_message(msg)

    async def send(self, subject: str, lines: list[str]) -> None:
        await asyncio.to_thread(self._send_blocking, subject, "\n".join(lines))

    async def close(self) -> None:  # pragma: no cover - nothing held open
        return None
