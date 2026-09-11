"""Jinja2-rendered pages: the seven screens of the console.

Pages are frames only — markup plus which page module drives it. Behaviour lives
in `static/js/pages/*.js`; no template contains logic in a `<script>` block
(§18 of docs/UI_CONCEPT_v2.md).

The old paths (`/registry`, `/`) still resolve: `/` is now «Прогон», and
`/registry` redirects to «Модели» so a bookmark does not 404.
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from router_mcp import __version__

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

pages_router = APIRouter()

# The bare package version was the bug: it reads 0.1.0 across every deploy, so
# `?v=` never changed and browsers kept running month-old page modules against
# a new API (found live: the journal drew 11 old cells under 15 new headers).
# The boot stamp changes on every restart, and every deploy restarts.
STATIC_VERSION = f"{__version__}.{int(time.time())}"


def _page(request: Request, template: str, active: str, script: str) -> HTMLResponse:
    # `version` busts the static cache: after a deploy the browser must not keep
    # running the previous client against the new API.
    return templates.TemplateResponse(
        request,
        template,
        {"active": active, "page_script": script, "version": STATIC_VERSION},
    )


@pages_router.get("/", response_class=HTMLResponse)
async def page_run(request: Request) -> HTMLResponse:
    return _page(request, "run.html", "run", "run")


@pages_router.get("/overview", response_class=HTMLResponse)
async def page_overview(request: Request) -> HTMLResponse:
    return _page(request, "overview.html", "overview", "overview")


@pages_router.get("/models", response_class=HTMLResponse)
async def page_models(request: Request) -> HTMLResponse:
    return _page(request, "models.html", "models", "models")


@pages_router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request) -> Response:
    # «Расход» was a tab of this screen and lived in the address as `?tab=spend`;
    # a bookmark from then lands on its own page now.
    if request.query_params.get("tab") == "spend":
        return RedirectResponse("/spend", status_code=301)
    return _page(request, "logs.html", "logs", "logs")


@pages_router.get("/spend", response_class=HTMLResponse)
async def page_spend(request: Request) -> HTMLResponse:
    return _page(request, "spend.html", "spend", "spend")


@pages_router.get("/images", response_class=HTMLResponse)
async def page_images(request: Request) -> HTMLResponse:
    return _page(request, "images.html", "images", "images")


@pages_router.get("/connections", response_class=HTMLResponse)
async def page_connections(request: Request) -> HTMLResponse:
    return _page(request, "connections.html", "connections", "connections")


@pages_router.get("/registry", include_in_schema=False)
async def page_registry_moved() -> RedirectResponse:
    """The registry screen split in two: models here, providers under
    «Подключения». Old links keep working."""
    return RedirectResponse("/models", status_code=301)
