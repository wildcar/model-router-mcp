"""Jinja2-rendered pages: the five screens of the console.

Pages are frames only — markup plus which page module drives it. Behaviour lives
in `static/js/pages/*.js`; no template contains logic in a `<script>` block
(§18 of docs/UI_CONCEPT_v2.md).

The old paths (`/registry`, `/`) still resolve: `/` is now «Прогон», and
`/registry` redirects to «Модели» so a bookmark does not 404.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from router_mcp import __version__

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

pages_router = APIRouter()


def _page(request: Request, template: str, active: str, script: str) -> HTMLResponse:
    # `version` busts the static cache: after a deploy the browser must not keep
    # running the previous client against the new API.
    return templates.TemplateResponse(
        request,
        template,
        {"active": active, "page_script": script, "version": __version__},
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
async def page_logs(request: Request) -> HTMLResponse:
    return _page(request, "logs.html", "logs", "logs")


@pages_router.get("/connections", response_class=HTMLResponse)
async def page_connections(request: Request) -> HTMLResponse:
    return _page(request, "connections.html", "connections", "connections")


@pages_router.get("/registry", include_in_schema=False)
async def page_registry_moved() -> RedirectResponse:
    """The registry screen split in two: models here, providers under
    «Подключения». Old links keep working."""
    return RedirectResponse("/models", status_code=301)
