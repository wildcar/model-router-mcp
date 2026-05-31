"""Jinja2-rendered pages: test console, registry admin, request logs."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

pages_router = APIRouter()


@pages_router.get("/", response_class=HTMLResponse)
async def page_test(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "test.html", {"active": "test"})


@pages_router.get("/registry", response_class=HTMLResponse)
async def page_registry(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "registry.html", {"active": "registry"})


@pages_router.get("/logs", response_class=HTMLResponse)
async def page_logs(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "logs.html", {"active": "logs"})
