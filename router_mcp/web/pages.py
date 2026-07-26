"""Jinja2-rendered pages: test console, registry admin, request logs.

Pages are frames only — markup plus which page module drives it. Behaviour lives
in `static/js/pages/*.js`; no template contains logic in a `<script>` block
(§18 of docs/UI_CONCEPT_v2.md).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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
async def page_test(request: Request) -> HTMLResponse:
    return _page(request, "test.html", "test", "test")


@pages_router.get("/registry", response_class=HTMLResponse)
async def page_registry(request: Request) -> HTMLResponse:
    return _page(request, "registry.html", "registry", "registry")


@pages_router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request) -> HTMLResponse:
    return _page(request, "logs.html", "logs", "logs")
