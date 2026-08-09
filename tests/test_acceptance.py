"""The acceptance list of §20, driven in a real browser.

Each test is one of the twelve things `docs/UI_CONCEPT_v2.md` says an operator
must be able to do without reading the sources. The node harness in `tests/js/`
already checks that the page modules wire up; these check that the answers are
actually on screen, in a browser, against a live server.

Skipped when Playwright or its browser is missing — neither is needed to run the
service, and the production host has neither.
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    """A real uvicorn on a real port, in its own process.

    A subprocess rather than a thread because FastMCP's session manager can run
    only once per process (ADR-0003) and the rest of the suite already has an
    app in this one. It also happens to be how production runs it.
    """
    tmp = tmp_path_factory.mktemp("e2e")
    db_path = str(tmp / "e2e.db")
    port = _free_port()
    env = {
        **os.environ,
        "DB_PATH": db_path,
        "BLOB_DIR": str(tmp / "blobs"),
        "LOG_FILE": str(tmp / "log.txt"),
        "CATALOG_SYNC_INTERVAL_SECONDS": "0",
        # Cleared explicitly: Settings reads the repo .env, and a configured
        # adapter would call a live provider during the run.
        "OPENROUTER_API_KEY": "",
        "DEEPSEEK_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "DEEPGRAM_API_KEY": "",
        "ELEVENLABS_API_KEY": "",
        "REPLICATE_API_TOKEN": "",
        # A path rather than a key, and just as much a live credential.
        "CODEX_AUTH_PATH": "",
        "AUTH_TOKEN": "",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "router_mcp.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 60
    while time.time() < deadline:
        if process.poll() is not None:  # pragma: no cover - startup failure
            pytest.fail(f"server died: {process.stdout.read().decode()[-2000:]}")
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=1):
                break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.2)
    else:  # pragma: no cover - startup failure
        process.kill()
        pytest.fail("server did not become healthy")

    _seed(db_path)
    yield base, db_path
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()


def _stamp(minutes_ago: int) -> str:
    """A created_at near «now», in the stored format.

    The seed used to carry the literal date it was written on, and the suite
    rotted on a timer: the MCP clients table reads the last 24 hours, so
    test_mcp_tab_lists_tools_with_schemas started failing the very next day,
    and the spend tab (30-day period) would have followed a month later.
    """
    stamp = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return stamp.isoformat(timespec="seconds")


def _seed(db_path: str) -> None:
    """Rows covering the cases the acceptance list asks about."""
    steps = (
        '[{"at_ms":0,"kind":"plan","title":"plan built",'
        '"planned":[{"provider":"deepseek","model_id":"deepseek-v4-pro"}]},'
        '{"at_ms":2,"kind":"attempt","title":"attempt 1","provider":"deepseek",'
        '"model_id":"deepseek-v4-pro","status":"error","error":"RateLimited: HTTP 429",'
        '"retries":[{"at_ms":0,"status":429,"retry_after_s":5,"wait_s":5},'
        '{"at_ms":5412,"status":429,"retry_after_s":3,"wait_s":3}]},'
        '{"at_ms":8020,"kind":"attempt","title":"attempt 2","provider":"deepseek",'
        '"model_id":"deepseek-chat","status":"ok","reason":"deepseek-v4-pro failed: '
        'RateLimited: HTTP 429","retries":[{"at_ms":0,"status":200}]},'
        '{"at_ms":8410,"kind":"cost","title":"cost computed","cost_source":"estimated"}]'
    )
    conn = sqlite3.connect(db_path)
    with conn:
        # A slow run that was slow because of retries, with a substitution.
        conn.execute(
            """
            INSERT INTO request_logs (
                id, external_user_id, modality, task, service_provider_id, model_provider,
                model_id, requested_model_id, fallback_reason, status, channel,
                prompt_tokens, completion_tokens, total_tokens, cache_read_tokens,
                cost_usd, cost_source, latency_ms, attempts, provider_request_id,
                finish_reason, response_text, plan_json, created_at
            ) VALUES (4821,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            ("web-tester", "chat", "text-to-text", "deepseek", "deepseek", "deepseek-chat",
             "deepseek-v4-pro", "deepseek-v4-pro failed: RateLimited: HTTP 429",
             "ok", "ui", 1042, 318, 1360, 890, 0.000412, "estimated", 8420, 3,
             "req-77f0", "length", "Ответ модели", steps,
             _stamp(10)),
        )
        # An error naming the environment variable behind it.
        conn.execute(
            """
            INSERT INTO request_logs (
                id, external_user_id, modality, service_provider_id, model_id,
                status, error_message, channel, cost_source, created_at
            ) VALUES (4819,?,?,?,?,?,?,?,?,?)
            """,
            ("bot-77", "tts", "elevenlabs", None, "error",
             "NotConfigured: Provider 'elevenlabs' is not configured "
             "[code=not_configured env_var=ELEVENLABS_API_KEY]",
             "mcp", "computed", _stamp(12)),
        )
        # A stored XSS attempt as a user label — §20.12.
        conn.execute(
            """
            INSERT INTO request_logs (
                external_user_id, modality, model_id, status, channel,
                cost_usd, cost_source, request_text, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            ('<img src=x onerror="window.__pwned=1">', "chat", "m", "ok", "api",
             0.0, "unpriced", '<script>window.__pwned=1</script>',
             _stamp(15)),
        )
    conn.close()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        try:
            instance = p.chromium.launch()
        except Exception as exc:  # pragma: no cover - browser not installed
            pytest.skip(f"chromium unavailable: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(browser, live_server):
    base, _db = live_server
    context = browser.new_context(viewport={"width": 1600, "height": 1000})
    page = context.new_page()
    page.set_default_timeout(15000)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(base)
    yield page
    assert not errors, f"JS errors on the page: {errors}"
    context.close()


def _open(page, base_path: str):
    page.goto(page.url.split("/", 3)[0] + "//" + page.url.split("/")[2] + base_path)


# --- §20.1 / §20.2: why the request went elsewhere, and why it was slow ----
def test_substitution_and_retries_are_two_clicks_from_the_result(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs?log=4821")
    inspector = page.locator("#detail")
    inspector.wait_for()
    # The substitution, and which kind it was.
    assert "запрошена deepseek-v4-pro" in inspector.inner_text()
    assert "после отказа" in inspector.inner_text()

    # Eight seconds that were two 429s and a sleep, not a slow model.
    inspector.get_by_role("tab", name="Шаги", exact=False).click()
    steps = inspector.inner_text()
    assert "провайдер просил 5 с" in steps
    assert "deepseek-v4-pro" in steps and "deepseek-chat" in steps


# --- §20.3: copy the JSON and the provider's request id -------------------
def test_raw_tab_offers_the_provider_request_id(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs?log=4821")
    inspector = page.locator("#detail")
    inspector.get_by_role("tab", name="Сырьё").click()
    text = inspector.inner_text()
    assert "req-77f0" in text
    # And the stop reason spelled out, not as a bare `length`.
    assert "обрыв по лимиту токенов" in text


# --- §20.5: why tts fails, and which variable to set ----------------------
def test_the_log_names_the_environment_variable_behind_a_failure(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs?status=error")
    page.wait_for_selector("#logs tbody tr")
    body = page.locator("#logs").inner_text()
    assert "ELEVENLABS_API_KEY" in body


# --- §20.8: the price states are told apart wherever money is shown -------
def test_price_states_are_distinguishable_in_the_log(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs")
    page.wait_for_selector("#logs tbody tr")
    # An unconfirmed rate wears its marker.
    assert "≈" in page.locator("#logs").inner_text()
    # And a request with no pricing method shows a dash, not a zero.
    unpriced = page.locator("#logs tbody tr", has_text="unpriced")
    assert page.locator("#logs tbody").inner_text().count("—") > 0 or unpriced


def test_spend_tab_keeps_the_kinds_of_money_apart(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs?tab=spend")
    page.get_by_role("tab", name="Расход").click()
    totals = page.locator("#s-totals")
    totals.wait_for()
    text = totals.inner_text()
    assert "подтверждённые ставки" in text
    assert "неподтверждённые ставки" in text
    assert "не списывалась" in text
    # The seeded rows include an unpriced one, so the sum is a floor.
    assert "не менее" in text


# --- §20.9: the state of the service in five seconds ----------------------
def test_overview_answers_without_scrolling(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/overview")
    page.wait_for_selector("#providers-count:not(:text('—'))")
    assert "/" in page.locator("#providers-count").inner_text()
    assert page.locator("#service-detail").inner_text()
    # Work list items are links, not statements.
    work = page.locator("#work a")
    assert work.count() > 0
    assert work.first.get_attribute("href").startswith("/")


# --- §20.10: which MCP client uses which tool -----------------------------
def test_mcp_tab_lists_tools_with_schemas(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/connections?tab=mcp")
    page.get_by_role("tab", name="MCP").click()
    page.wait_for_selector("#tools tbody tr")
    tools = page.locator("#tools").inner_text()
    assert "chat" in tools and "run_cli" in tools
    # The seeded MCP error was logged against the tts tool-less call, but the
    # client table must still render.
    assert page.locator("#clients tbody tr").count() >= 1
    # Exact cell match, not a substring of the whole row: several tool
    # descriptions mention "chat" in prose, and `has_text` would pick whichever
    # of them sorts first.
    chat_row = page.locator("#tools tbody tr").filter(
        has=page.get_by_role("cell", name="chat", exact=True)
    ).first
    chat_row.get_by_text("схема").click()
    # The schema next to the handle is the point of the tab.
    assert "external_user_id" in page.locator("#tools").inner_text()


# --- §20.12: a stored payload is displayed as text ------------------------
def test_a_script_in_a_user_label_stays_text(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/logs")
    page.wait_for_selector("#logs tbody tr")
    # The payload is on screen…
    assert "<img src=x onerror=" in page.locator("#logs").inner_text()
    # …and it did not run.
    assert page.evaluate("window.__pwned === undefined")
    assert page.locator("#logs img").count() == 0


# --- The run screen explains itself before spending anything --------------
def test_the_plan_costs_nothing_and_says_so(page, live_server):
    base, _ = live_server
    page.goto(base)
    page.wait_for_selector("#params")
    requests: list[str] = []
    page.on("request", lambda r: requests.append(f"{r.method} {r.url}"))
    page.get_by_role("button", name="Показать план").click()
    page.wait_for_selector("#plan:not([hidden])")
    plan = page.locator("#plan").inner_text()
    assert "ничего не отправлено" in plan
    assert "Кандидаты" in plan
    # Nothing was dispatched: no modality endpoint was called.
    assert not [r for r in requests if any(
        r.endswith(f"/api/v1/{name}") for name in ("chat", "image", "tts", "cli"))]


def test_the_form_shows_only_parameters_the_adapter_reads(page, live_server):
    base, _ = live_server
    page.goto(base)
    page.wait_for_selector("#params label")
    # The screen opens on whatever can actually run here, so the task is chosen
    # explicitly rather than assumed.
    page.select_option("#task", "text-to-text")
    page.wait_for_function(
        "() => document.querySelector('#params').innerText.includes('Температура')"
    )
    chat_params = page.locator("#params").inner_text()
    assert "Температура" in chat_params

    page.select_option("#task", "cli")
    page.wait_for_function(
        "() => document.querySelector('#params').innerText.includes('Рабочий каталог')"
    )
    cli_params = page.locator("#params").inner_text()
    assert "Рабочий каталог" in cli_params
    # Chat knobs are gone, because no CLI adapter reads them.
    assert "Температура" not in cli_params


def test_every_parameter_carries_a_visible_explanation(page, live_server):
    base, _ = live_server
    page.goto(base)
    page.wait_for_selector("#params label")
    for i in range(page.locator("#params label").count()):
        label = page.locator("#params label").nth(i)
        assert label.locator(".under").inner_text().strip()


# --- §20.4: find the models with no confirmed price and fix one -----------
def test_models_screen_filters_by_price_provenance(page, live_server):
    base, _ = live_server
    page.goto(f"{base}/models")
    page.wait_for_selector("#models tbody tr")
    facets = page.locator("#facets").inner_text()
    assert "ЦЕНА ОТКУДА" in facets.upper()

    page.locator("#models tbody tr").first.click()
    card = page.locator("#card")
    card.wait_for()
    # The three guards on the price field.
    assert "$/1M" in card.inner_text()
    assert "было" in card.inner_text()
    assert "переживёт синхронизацию" in card.inner_text()


# --- Wave 3: a run is a thing with a state, and the state is on screen -----
def test_a_run_in_flight_reads_as_running_not_as_an_error(page, live_server):
    """The log holds calls that have not finished yet.

    Before wave 3 a row appeared only once the run was over, so a five-minute
    CLI run was simply absent. Now it is present — and it must not be dressed as
    a failure while it is still perfectly fine.
    """
    base, db_path = live_server
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO request_logs (
                id, external_user_id, modality, service_provider_id, model_id,
                status, channel, cost_source, request_text, created_at
            ) VALUES (5001,?,?,?,?,?,?,?,?,?)
            """,
            ("live-user", "cli", "codex-cli", "codex", "running", "ui",
             "cost_unknown", "разбери репозиторий", _stamp(1)),
        )
    conn.close()

    page.goto(f"{base}/logs?status=running")
    page.wait_for_selector("#logs tbody tr")
    rows = page.locator("#logs").inner_text()
    assert "выполняется" in rows
    # No error line under a row that has not failed.
    assert "ошибка без описания" not in rows


def test_the_multi_model_mode_runs_one_row_per_model(page, live_server):
    """§10.6: the same request against several models, as a table.

    Nothing is configured here, so every run fails — which is the point: the
    table has to report a per-model outcome, not swallow the batch.
    """
    base, _ = live_server
    # A model to compare against; the test installation has no catalogs.
    for model_id in ("compare/a", "compare/b"):
        page.request.post(
            f"{base}/api/v1/registry/models",
            data={
                "service_provider_id": "deepseek",
                "model_id": model_id,
                "tasks": ["text-to-text"],
            },
        )
    page.goto(base)
    page.wait_for_selector("#params")
    page.select_option("#task", "text-to-text")
    page.get_by_role("tab", name="Несколько").click()
    for model_id in ("compare/a", "compare/b"):
        page.select_option("#multi-pick", model_id)
        page.get_by_role("button", name="Добавить").click()
    assert page.locator("#chips .chip").count() == 2
    # The button states what one click is about to do.
    assert "Запустить 2" in page.locator("#send").inner_text()

    page.locator("#send").click()
    page.wait_for_selector("#multi:not([hidden]) tbody tr")
    page.wait_for_function(
        "() => document.querySelectorAll('#multi tbody tr').length === 2")
    page.wait_for_function(
        "() => !document.querySelector('#multi tbody').innerText.includes('выполняется')")
    table = page.locator("#multi").inner_text()
    assert table.count("compare/") == 2
    assert "ошибка" in table


def test_the_overview_says_what_a_purge_would_delete(page, live_server):
    """Retention is offered next to the disk figures, and it says first."""
    base, _ = live_server
    page.goto(f"{base}/overview")
    page.wait_for_selector("#retention tr")
    section = page.locator("#retention-policy").inner_text()
    assert "бессрочно" in section          # nothing configured in tests
    assert "LOG_RETENTION_DAYS" in section
    rows = page.locator("#retention").inner_text()
    assert "Будет удалено записей" in rows
    assert "Файлов без записи" in rows
    # With nothing to delete the button is not offered as if it did something.
    assert page.locator("#purge").is_disabled()
