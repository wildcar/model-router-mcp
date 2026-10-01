"""Shared fixtures.

One app per test session, deliberately: FastMCP's session manager can be run
only once per process (ADR-0003), which is also why production runs a single
uvicorn worker. Every module that needs the HTTP surface shares this client
rather than building a second app that would fail on the second lifespan.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from router_mcp.app import build_app
from router_mcp.config import Settings


@pytest.fixture(scope="session")
def app_db_path(tmp_path_factory) -> str:
    return str(tmp_path_factory.mktemp("app") / "app.db")


@pytest.fixture(scope="session")
def client(app_db_path, tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("app-data")
    settings = Settings(
        DB_PATH=app_db_path,
        BLOB_DIR=str(tmp_path / "blobs"),
        LOG_FILE=str(tmp_path / "log.txt"),
        # Every credential is cleared explicitly: Settings still reads the repo
        # .env, and a configured adapter would call the real API during tests.
        OPENROUTER_API_KEY=None,
        DEEPSEEK_API_KEY=None,
        ANTHROPIC_API_KEY=None,
        DEEPGRAM_API_KEY=None,
        ELEVENLABS_API_KEY=None,
        YANDEX_API_KEY=None,
        REPLICATE_API_TOKEN=None,
        # This one is a path, not a key, so it was missed while codex-oauth could
        # only generate images and no test asked for one. The moment it also
        # answered text, a chat run in the suite reached the live ChatGPT backend.
        CODEX_AUTH_PATH=None,
        CATALOG_SYNC_INTERVAL_SECONDS=0,
        # The console login is tested on its own app (test_web_login.py).
        WEB_AUTH_HTPASSWD=None,
    )
    with TestClient(build_app(settings)) as c:
        yield c
