"""Failures reach the operator — the right ones, once, with a link to the row.

Three things are defended here: a provider's refusal text is classified into
the code the filter reads (a refused picture is not «provider_error»), the
filters and the cooldown keep 73 identical refusals from being 73 messages,
and the router hands the notifier exactly the failures that closed a run.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from router_mcp.adapters.base import (
    ProviderAdapter,
    RetryClientMixin,
    provider_error_for,
)
from router_mcp.blobs.store import BlobStore
from router_mcp.config import Settings
from router_mcp.domain.errors import (
    AccessDenied,
    ContentFiltered,
    InsufficientCredits,
    NotConfigured,
    ProviderError,
)
from router_mcp.domain.messages import RouterRequest, RouterResponse, Usage
from router_mcp.domain.modality import Modality
from router_mcp.domain.task import Task
from router_mcp.notify.email import EmailChannel
from router_mcp.notify.service import Failure, Notifier, build_notifier
from router_mcp.notify.telegram import TelegramChannel
from router_mcp.registry.models import RegistryModel
from router_mcp.registry.service import RegistryService
from router_mcp.routing.router import ModelRouter
from router_mcp.storage.database import Database, RequestLogRecord

# --- classification ---------------------------------------------------------

# The sentences as they stand in the production log (2026-08/09).
REFUSALS = [
    "Replicate prediction failed: Prediction failed: Async prediction failed: ModelError: "
    "The input or output was flagged as sensitive. Please try again with different inputs.",
    "Replicate prediction failed: The input or output was flagged as sensitive. (E005) (uIJ6l3ruRD)",
    "Replicate prediction failed: All generated images contained NSFW content. Try running it again",
    'HTTP 400: {"error":{"message":"The response was filtered due to the prompt triggering '
    'our content management policy.","code":400}}',
]
EMPTY_ACCOUNTS = [
    'HTTP 402: {"error":{"message":"Insufficient credits. Add more using '
    'https://openrouter.ai/settings/credits","code":402}}',
    "Insufficient Balance",
    "You exceeded your current quota, please check your plan and billing details.",
]


@pytest.mark.parametrize("message", REFUSALS)
def test_a_refusal_is_a_content_filter(message):
    exc = provider_error_for(message)
    assert isinstance(exc, ContentFiltered)
    assert exc.code == "content_filter" and exc.http_status == 422
    assert not exc.retriable


@pytest.mark.parametrize("message", EMPTY_ACCOUNTS)
def test_an_empty_account_is_insufficient_credits(message):
    assert isinstance(provider_error_for(message), InsufficientCredits)


def test_http_402_is_insufficient_credits_whatever_the_body_says():
    exc = provider_error_for("HTTP 402: {}", provider_status=402)
    assert isinstance(exc, InsufficientCredits) and exc.provider_status == 402


def test_anything_else_stays_a_plain_provider_error():
    exc = provider_error_for("Replicate model x returned no output")
    assert type(exc) is ProviderError


class _Client(RetryClientMixin):
    def __init__(self, handler) -> None:
        self._client = httpx.AsyncClient(
            base_url="https://p.test", transport=httpx.MockTransport(handler)
        )
        self._retries = 0


@pytest.mark.asyncio
async def test_the_retry_mixin_classifies_402_and_moderation_403():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pay":
            return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})
        if request.url.path == "/mod":
            return httpx.Response(
                403, json={"error": {"message": "Input flagged by moderation", "code": 403}}
            )
        return httpx.Response(403, json={"error": {"message": "bad key"}})

    client = _Client(handler)
    with pytest.raises(InsufficientCredits):
        await client._request_with_retry("POST", "/pay")
    with pytest.raises(ContentFiltered):
        await client._request_with_retry("POST", "/mod")
    # A 403 that is about the key is still what it always was.
    with pytest.raises(AccessDenied):
        await client._request_with_retry("POST", "/key")
    await client.close()


# --- the notifier's filters and cooldown ------------------------------------


class Capture:
    """A channel that remembers what it was asked to send."""

    name = "capture"

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, list[str]]] = []
        self.fail = fail

    def describe(self):
        return {"name": self.name, "targets": 1, "detail": "тест"}

    async def send(self, subject: str, lines: list[str]) -> None:
        if self.fail:
            raise RuntimeError("wire down")
        self.sent.append((subject, lines))

    async def close(self) -> None:
        return None


def _rec(**over) -> RequestLogRecord:
    base = dict(
        external_user_id="daypic", modality="image", task="text-to-image",
        service_provider_id="openrouter", model_id="openai/gpt-image", status="error",
        app="agentish.wildcar.org", channel="mcp", provider_status=402,
    )
    base.update(over)
    return RequestLogRecord(**base)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _flush(notifier: Notifier) -> None:
    if notifier._tasks:
        await asyncio.gather(*list(notifier._tasks))


@pytest.mark.asyncio
async def test_muted_codes_stay_in_the_log():
    cap = Capture()
    notifier = Notifier([cap], muted_codes={"content_filter"}, base_url="https://mr.test")
    notifier.failed(ContentFiltered("flagged as sensitive"), _rec(), 41)
    await _flush(notifier)
    assert cap.sent == []
    state = notifier.describe()
    assert state["counts"]["muted"] == 1
    assert state["recent"][0]["outcome"] == "muted_code"
    assert state["recent"][0]["log_id"] == 41


@pytest.mark.asyncio
async def test_muted_apps_and_users_stay_in_the_log():
    cap = Capture()
    notifier = Notifier([cap], muted_apps={"bench"}, muted_users={"qa"})
    notifier.failed(InsufficientCredits("no money"), _rec(app="bench"), 1)
    notifier.failed(InsufficientCredits("no money"), _rec(external_user_id="qa"), 2)
    await _flush(notifier)
    assert cap.sent == []
    assert [r["outcome"] for r in notifier.describe()["recent"]] == ["muted_user", "muted_app"]


@pytest.mark.asyncio
async def test_an_empty_account_is_sent_with_the_row_link():
    cap = Capture()
    notifier = Notifier(
        [cap], muted_codes={"content_filter"}, base_url="https://mr.test/",
        service_name="model-router",
    )
    exc = InsufficientCredits("Insufficient credits. Add more using https://x", provider_status=402)
    notifier.failed(exc, _rec(), 22771)
    await _flush(notifier)
    assert len(cap.sent) == 1
    subject, lines = cap.sent[0]
    assert subject == "[model-router] insufficient_credits · openrouter · openai/gpt-image · text-to-image"
    assert lines[0] == "⛔ model-router: insufficient_credits"
    assert "openrouter · openai/gpt-image · text-to-image" in lines
    assert "приложение agentish.wildcar.org · метка daypic · канал mcp" in lines
    assert any(line.startswith("HTTP 402 · InsufficientCredits: Insufficient credits") for line in lines)
    assert "Запись #22771: https://mr.test/logs?log=22771" in lines
    assert notifier.describe()["counts"]["sent"] == 1


@pytest.mark.asyncio
async def test_repeats_inside_the_window_are_counted_not_sent():
    cap = Capture()
    clock = Clock()
    notifier = Notifier([cap], cooldown_seconds=600, clock=clock)
    for log_id in range(1, 5):
        notifier.failed(InsufficientCredits("no money"), _rec(), log_id)
        clock.now += 10
    await _flush(notifier)
    assert len(cap.sent) == 1
    assert notifier.describe()["counts"]["throttled"] == 3

    # A different failure kind is its own key and goes out at once.
    notifier.failed(ProviderError("boom"), _rec(model_id="other"), 9)
    await _flush(notifier)
    assert len(cap.sent) == 2

    # Past the window the next one is sent and says how many were swallowed.
    clock.now += 600
    notifier.failed(InsufficientCredits("no money"), _rec(), 10)
    await _flush(notifier)
    assert len(cap.sent) == 3
    assert "Ещё 3 таких за последние 10 мин." in cap.sent[-1][1]


@pytest.mark.asyncio
async def test_a_channel_failure_is_recorded_and_never_raised():
    notifier = Notifier([Capture(fail=True)])
    notifier.failed(ProviderError("boom"), _rec(), 5)
    await _flush(notifier)
    state = notifier.describe()
    assert state["counts"]["failed"] == 1
    assert "wire down" in state["recent"][0]["detail"]


@pytest.mark.asyncio
async def test_no_channels_means_nothing_is_sent_and_the_test_button_says_why():
    notifier = Notifier([])
    assert not notifier.enabled
    notifier.failed(ProviderError("boom"), _rec(), 5)
    await _flush(notifier)
    assert notifier.describe()["recent"][0]["outcome"] == "no_channels"
    with pytest.raises(NotConfigured):
        await notifier.send_test()


@pytest.mark.asyncio
async def test_the_test_message_names_the_filters():
    cap = Capture()
    notifier = Notifier([cap], muted_codes={"content_filter", "bad_request"}, cooldown_seconds=900)
    results = await notifier.send_test()
    assert results == [{"channel": "capture", "status": "ok", "detail": None}]
    _, lines = cap.sent[0]
    assert "Не шлются коды: bad_request, content_filter" in lines
    assert "Повторы одной ошибки: не чаще раза в 15 мин" in lines


def test_build_notifier_from_settings_is_off_without_credentials():
    settings = Settings(
        NOTIFY_TELEGRAM_BOT_TOKEN=None, NOTIFY_TELEGRAM_CHAT_IDS="",
        NOTIFY_EMAIL_TO="", NOTIFY_SMTP_HOST=None,
    )
    notifier = build_notifier(settings)
    assert not notifier.enabled
    assert {"content_filter", "empty_response"} <= set(notifier.describe()["muted_codes"])


def test_build_notifier_from_settings_wires_both_channels():
    settings = Settings(
        NOTIFY_TELEGRAM_BOT_TOKEN="123:abc", NOTIFY_TELEGRAM_CHAT_IDS="1, -200",
        NOTIFY_EMAIL_TO="ops@example.org", NOTIFY_SMTP_HOST="smtp.example.org",
        NOTIFY_MUTE_CODES="content_filter", NOTIFY_MUTE_APPS="bench",
    )
    state = build_notifier(settings).describe()
    assert [c["name"] for c in state["channels"]] == ["telegram", "email"]
    assert state["channels"][0]["targets"] == 2
    assert state["muted_codes"] == ["content_filter"]
    assert state["muted_apps"] == ["bench"]


# --- the channels -----------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_posts_bold_escaped_html_to_every_chat():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"path": request.url.path, "body": json.loads(request.content)})
        return httpx.Response(200, json={"ok": True})

    channel = TelegramChannel(
        "123:abc", ["1", "-200"], api_base="https://tg.test", transport=httpx.MockTransport(handler)
    )
    await channel.send("subj", ["⛔ r: code", "a <b> & c"])
    await channel.close()
    assert [c["path"] for c in calls] == ["/bot123:abc/sendMessage"] * 2
    assert [c["body"]["chat_id"] for c in calls] == ["1", "-200"]
    assert calls[0]["body"]["text"] == "<b>⛔ r: code</b>\na &lt;b&gt; &amp; c"
    assert calls[0]["body"]["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_telegram_names_the_chat_and_the_reason_on_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    channel = TelegramChannel("123:abc", ["7"], transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="chat 7: HTTP 400 Bad Request: chat not found"):
        await channel.send("s", ["x"])
    await channel.close()


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.events: list[tuple] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append(("quit",))

    def starttls(self):
        self.events.append(("starttls",))

    def login(self, user, password):
        self.events.append(("login", user, password))

    def send_message(self, msg):
        self.events.append(("send", msg))


@pytest.mark.asyncio
async def test_email_sends_one_message_to_all_recipients():
    FakeSMTP.instances.clear()
    channel = EmailChannel(
        ["a@example.org", "b@example.org"], host="smtp.example.org", port=587,
        user="bot", password="pw", sender="router@example.org", smtp_factory=FakeSMTP,
    )
    await channel.send("[model-router] insufficient_credits", ["line one", "line two"])
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.example.org", 587)
    kinds = [e[0] for e in smtp.events]
    assert kinds == ["starttls", "login", "send", "quit"]
    msg = smtp.events[2][1]
    assert msg["Subject"] == "[model-router] insufficient_credits"
    assert msg["To"] == "a@example.org, b@example.org"
    assert msg["From"] == "router@example.org"
    assert msg.get_content().strip() == "line one\nline two"


# --- through the router -----------------------------------------------------


class FailingAdapter(ProviderAdapter):
    name = "vendor"
    supported_tasks = {Task.TEXT_TO_TEXT}

    def __init__(self, error: Exception | None) -> None:
        self.error = error

    def is_configured(self) -> bool:
        return True

    async def generate(self, request: RouterRequest, model: RegistryModel) -> RouterResponse:
        if self.error is not None:
            raise self.error
        return RouterResponse(
            task=Task.TEXT_TO_TEXT, provider=self.name, model_id=model.model_id,
            usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2), text="ok",
        )


def _model() -> RegistryModel:
    return RegistryModel(
        service_provider_id="vendor", model_provider="vendor", model_id="m",
        display_name="m", modalities=(Modality.CHAT,), tier="cheap",
        prompt_price=1e-7, completion_price=1e-7, source="manual", price_source_kind="manual",
    )


async def _router(tmp_path, error, notifier):
    db = Database(str(tmp_path / "n.db"))
    await db.init()
    await db.upsert_service_provider("vendor", "Vendor", "vendor", None, enabled=True)
    await db.upsert_registry_model(_model())
    adapter = FailingAdapter(error)
    router = ModelRouter(
        db, RegistryService(db, adapters={}), {"vendor": adapter},
        BlobStore(str(tmp_path / "b")), notifier=notifier,
    )
    return db, router


@pytest.mark.asyncio
async def test_a_failed_run_reaches_the_notifier_and_the_row_keeps_the_code(tmp_path):
    cap = Capture()
    notifier = Notifier([cap], muted_codes={"content_filter"})
    db, router = await _router(tmp_path, InsufficientCredits("no money", provider_status=402), notifier)
    try:
        req = RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", model_id="m", text="hi",
                            app_url="https://bench.example.org")
        with pytest.raises(InsufficientCredits) as info:
            await router.dispatch(req)
        await _flush(notifier)
        assert len(cap.sent) == 1
        assert f"Запись #{info.value.log_id}" in cap.sent[0][1]
        assert "приложение https://bench.example.org · метка u · канал api" in cap.sent[0][1]
        row = await db.get_request_log(info.value.log_id)
        assert row["error_code"] == "insufficient_credits"
        assert row["status"] == "error"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_refused_picture_is_logged_but_not_sent(tmp_path):
    cap = Capture()
    notifier = Notifier([cap], muted_codes={"content_filter"})
    db, router = await _router(tmp_path, ContentFiltered("flagged as sensitive"), notifier)
    try:
        req = RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", model_id="m", text="hi")
        with pytest.raises(ContentFiltered) as info:
            await router.dispatch(req)
        await _flush(notifier)
        assert cap.sent == []
        row = await db.get_request_log(info.value.log_id)
        assert row["error_code"] == "content_filter"
        assert notifier.describe()["recent"][0]["outcome"] == "muted_code"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_successful_run_tells_nobody(tmp_path):
    cap = Capture()
    notifier = Notifier([cap])
    db, router = await _router(tmp_path, None, notifier)
    try:
        req = RouterRequest(task=Task.TEXT_TO_TEXT, external_user_id="u", model_id="m", text="hi")
        await router.dispatch(req)
        await _flush(notifier)
        assert cap.sent == [] and notifier.describe()["recent"] == []
    finally:
        await db.close()


# --- the API ----------------------------------------------------------------


def test_the_console_sees_the_channels_and_the_filters(client):
    state = client.get("/api/v1/notifications").json()
    assert state["enabled"] is False and state["channels"] == []
    assert "content_filter" in state["muted_codes"]
    assert set(state["counts"]) == {"sent", "muted", "throttled", "failed"}


def test_the_test_button_answers_not_configured_without_channels(client):
    response = client.post("/api/v1/notifications/test")
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "not_configured"
    assert "NOTIFY_TELEGRAM_BOT_TOKEN" in body["message"]
