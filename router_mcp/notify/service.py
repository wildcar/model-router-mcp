"""Which failures reach the operator, and in what words.

Three filters stand between a failed run and a message, in this order:

1. **Muted codes.** `NOTIFY_MUTE_CODES` lists the error codes that are a
   property of the request rather than of the service — a refused picture
   (`content_filter`), a `max_tokens` too small (`response_truncated`), a model
   that now and then answers with nothing (`empty_response`). They
   stay in the log, where the journal's status filter finds them.
2. **Muted apps and callers.** A test bench or a client known to send garbage
   can be excluded by `request_logs.app` or by `external_user_id`.
3. **One message per failure kind per window.** The key is (code, provider,
   model); repeats inside `NOTIFY_COOLDOWN_SECONDS` are counted, not sent, and
   the count rides along on the next message («ещё 12 таких за 10 мин»).

Delivery is fire-and-forget from the router's point of view: `failed()` schedules
a task and returns, so a mail server that hangs costs the failing request
nothing. Channel errors are logged, never raised into the run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol

from router_mcp.domain.errors import NotConfigured, RouterError

if TYPE_CHECKING:
    from router_mcp.config import Settings
    from router_mcp.storage.database import RequestLogRecord

logger = logging.getLogger(__name__)

# Errors that are not `RouterError` were normalized by the router before they
# got here, so every failure has a code; this is the one the base class carries.
_INTERNAL = "internal_error"


class Channel(Protocol):
    name: str

    def describe(self) -> dict[str, Any]: ...

    async def send(self, subject: str, lines: list[str]) -> None: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class Failure:
    """What the message is built from — the log row's facts, not the exception."""

    code: str
    type_name: str
    message: str
    log_id: int
    provider: str | None = None
    model_id: str | None = None
    task: str | None = None
    app: str | None = None
    user: str = ""
    channel: str = "api"
    provider_status: int | None = None
    at: datetime | None = None

    @classmethod
    def from_error(cls, exc: BaseException, rec: "RequestLogRecord", log_id: int) -> "Failure":
        code = exc.code if isinstance(exc, RouterError) else _INTERNAL
        return cls(
            code=code,
            type_name=type(exc).__name__,
            message=str(exc),
            log_id=log_id,
            provider=rec.service_provider_id,
            model_id=rec.model_id,
            task=rec.task or rec.modality,
            app=rec.app,
            user=rec.external_user_id,
            channel=rec.channel,
            provider_status=rec.provider_status,
            at=datetime.now(timezone.utc),
        )

    @property
    def key(self) -> tuple[str, str | None, str | None]:
        return (self.code, self.provider, self.model_id)


def _minutes(seconds: int) -> str:
    if seconds % 3600 == 0 and seconds >= 3600:
        hours = seconds // 3600
        return f"{hours} ч"
    if seconds % 60 == 0 and seconds >= 60:
        return f"{seconds // 60} мин"
    return f"{seconds} с"


class Notifier:
    def __init__(
        self,
        channels: list[Channel],
        *,
        muted_codes: set[str] | None = None,
        muted_apps: set[str] | None = None,
        muted_users: set[str] | None = None,
        cooldown_seconds: int = 600,
        base_url: str | None = None,
        service_name: str = "model-router",
        clock=time.monotonic,
    ) -> None:
        self._channels = list(channels)
        self._muted_codes = set(muted_codes or ())
        self._muted_apps = set(muted_apps or ())
        self._muted_users = set(muted_users or ())
        self._cooldown = max(0, int(cooldown_seconds))
        self._base_url = (base_url or "").rstrip("/") or None
        self._service = service_name
        self._clock = clock
        # (code, provider, model) -> [when last sent, repeats since]
        self._last: dict[tuple[str, str | None, str | None], list[float | int]] = {}
        self._tasks: set[asyncio.Task] = set()
        # What happened to the last few failures, for the console: an operator
        # who got no message wants to see *why* before touching the .env.
        self._recent: deque[dict[str, Any]] = deque(maxlen=20)
        self._counts = {"sent": 0, "muted": 0, "throttled": 0, "failed": 0}

    # -- state for the console ------------------------------------------------

    @property
    def enabled(self) -> bool:
        return bool(self._channels)

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "channels": [channel.describe() for channel in self._channels],
            "muted_codes": sorted(self._muted_codes),
            "muted_apps": sorted(self._muted_apps),
            "muted_users": sorted(self._muted_users),
            "cooldown_seconds": self._cooldown,
            "counts": dict(self._counts),
            "recent": list(self._recent),
        }

    # -- the filters -----------------------------------------------------------

    def silenced(self, failure: Failure) -> str | None:
        """Why this failure sends nothing, or None if it should."""
        if not self._channels:
            return "no_channels"
        if failure.code in self._muted_codes:
            return "muted_code"
        if failure.app and failure.app in self._muted_apps:
            return "muted_app"
        if failure.user in self._muted_users:
            return "muted_user"
        return None

    def _throttle(self, failure: Failure) -> tuple[bool, int]:
        """(send now?, repeats swallowed since the last message of this kind)."""
        now = self._clock()
        entry = self._last.get(failure.key)
        if entry is not None and self._cooldown and now - entry[0] < self._cooldown:
            entry[1] += 1
            return False, int(entry[1])
        repeats = int(entry[1]) if entry is not None else 0
        self._last[failure.key] = [now, 0]
        return True, repeats

    # -- the entry point the router calls -------------------------------------

    def failed(self, exc: BaseException, rec: "RequestLogRecord", log_id: int) -> None:
        """Called from the router after the error row is written. Never raises."""
        try:
            failure = Failure.from_error(exc, rec, log_id)
            reason = self.silenced(failure)
            if reason is not None:
                self._note(failure, reason)
                return
            send, repeats = self._throttle(failure)
            if not send:
                self._note(failure, "throttled")
                return
            task = asyncio.get_running_loop().create_task(self._deliver_guarded(failure, repeats))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:  # pragma: no cover - the run must never pay for this
            logger.warning("Notifier failed to schedule a message", exc_info=True)

    def _note(self, failure: Failure, outcome: str, detail: str | None = None) -> None:
        bucket = {"muted_code": "muted", "muted_app": "muted", "muted_user": "muted",
                  "no_channels": "muted"}.get(outcome, outcome)
        if bucket in self._counts:
            self._counts[bucket] += 1
        self._recent.appendleft({
            "at": (failure.at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
            "log_id": failure.log_id,
            "code": failure.code,
            "provider": failure.provider,
            "model_id": failure.model_id,
            "outcome": outcome,
            "detail": detail,
        })

    async def _deliver_guarded(self, failure: Failure, repeats: int) -> None:
        results = await self.deliver(failure, repeats)
        failed = [r for r in results if r["status"] != "ok"]
        if failed:
            self._note(failure, "failed", "; ".join(f"{r['channel']}: {r['detail']}" for r in failed))
        else:
            self._note(failure, "sent")

    async def deliver(self, failure: Failure, repeats: int = 0) -> list[dict[str, Any]]:
        """Send to every channel; one channel's failure does not stop the next."""
        subject, lines = self.render(failure, repeats)
        return await self._broadcast(subject, lines)

    async def _broadcast(self, subject: str, lines: list[str]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for channel in self._channels:
            try:
                await channel.send(subject, lines)
                results.append({"channel": channel.name, "status": "ok", "detail": None})
            except Exception as exc:
                logger.warning("Notification via %s failed: %s", channel.name, exc)
                results.append({
                    "channel": channel.name, "status": "error",
                    "detail": f"{type(exc).__name__}: {exc}"[:300],
                })
        return results

    # -- words -----------------------------------------------------------------

    def log_url(self, log_id: int) -> str | None:
        return f"{self._base_url}/logs?log={log_id}" if self._base_url else None

    def render(self, failure: Failure, repeats: int = 0) -> tuple[str, list[str]]:
        """The subject and the lines. Russian, like the rest of the console."""
        where = " · ".join(p for p in (failure.provider, failure.model_id, failure.task) if p)
        who = " · ".join(
            p for p in (
                f"приложение {failure.app}" if failure.app else None,
                f"метка {failure.user}" if failure.user else None,
                f"канал {failure.channel}" if failure.channel else None,
            ) if p
        )
        status = f"HTTP {failure.provider_status} · " if failure.provider_status else ""
        lines = [f"⛔ {self._service}: {failure.code}"]
        if where:
            lines.append(where)
        if who:
            lines.append(who)
        lines.append(f"{status}{failure.type_name}: {failure.message}"[:1200])
        link = self.log_url(failure.log_id)
        lines.append(f"Запись #{failure.log_id}" + (f": {link}" if link else ""))
        if repeats:
            lines.append(f"Ещё {repeats} таких за последние {_minutes(self._cooldown)}.")
        subject = f"[{self._service}] {failure.code}" + (f" · {where}" if where else "")
        return subject, lines

    # -- the console's button --------------------------------------------------

    async def send_test(self) -> list[dict[str, Any]]:
        """A message that proves the channels work, with the filters spelled out."""
        if not self._channels:
            raise NotConfigured(
                "Ни один канал оповещений не настроен: задайте NOTIFY_TELEGRAM_BOT_TOKEN и "
                "NOTIFY_TELEGRAM_CHAT_IDS или NOTIFY_EMAIL_TO и NOTIFY_SMTP_HOST",
                hint={"env_var": "NOTIFY_TELEGRAM_BOT_TOKEN"},
            )
        muted = ", ".join(sorted(self._muted_codes)) or "нет"
        lines = [
            f"✅ {self._service}: пробное оповещение",
            "Каналы: " + ", ".join(f"{c.name} ({c.describe()['detail']})" for c in self._channels),
            f"Не шлются коды: {muted}",
            f"Повторы одной ошибки: не чаще раза в {_minutes(self._cooldown)}",
        ]
        if self._base_url:
            lines.append(f"Консоль: {self._base_url}/")
        return await self._broadcast(f"[{self._service}] пробное оповещение", lines)

    async def aclose(self) -> None:
        """Give in-flight messages a moment: a failure at shutdown is still news."""
        pending = [t for t in self._tasks if not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=5)
        for task in pending:
            if not task.done():
                task.cancel()
        for channel in self._channels:
            try:
                await channel.close()
            except Exception:  # pragma: no cover - best effort
                logger.warning("Error closing notification channel %s", channel.name, exc_info=True)


def build_notifier(settings: "Settings") -> Notifier:
    """Channels from the environment. No credentials, no channels, no messages."""
    channels: list[Channel] = []
    chat_ids = settings.notify_chat_ids()
    if settings.notify_telegram_bot_token and chat_ids:
        from router_mcp.notify.telegram import TelegramChannel

        channels.append(TelegramChannel(
            settings.notify_telegram_bot_token, chat_ids,
            api_base=settings.notify_telegram_api_base,
        ))
    recipients = settings.notify_recipients()
    if recipients and settings.notify_smtp_host:
        from router_mcp.notify.email import EmailChannel

        channels.append(EmailChannel(
            recipients,
            host=settings.notify_smtp_host,
            port=settings.notify_smtp_port,
            user=settings.notify_smtp_user,
            password=settings.notify_smtp_password,
            starttls=settings.notify_smtp_starttls,
            sender=settings.notify_email_from,
        ))
    return Notifier(
        channels,
        muted_codes=settings.notify_muted_codes(),
        muted_apps=settings.notify_muted_apps(),
        muted_users=settings.notify_muted_users(),
        cooldown_seconds=settings.notify_cooldown_seconds,
        base_url=settings.self_app_url,
        service_name=settings.self_app_name,
    )
