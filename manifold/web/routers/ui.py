"""UI router — serves the single-page HTML."""

import os
from pathlib import Path

from fastapi import APIRouter, Request

from manifold.web.app import templates

router = APIRouter()

_STATIC_DIR = Path(__file__).parent.parent / "static"


def _asset_version() -> str:
    """Max mtime of JS + CSS — bumps automatically whenever a static asset changes
    so the browser never serves a cached version that disagrees with the HTML."""
    try:
        js = (_STATIC_DIR / "app.js").stat().st_mtime
        css = (_STATIC_DIR / "style.css").stat().st_mtime
        return str(int(max(js, css)))
    except OSError:
        return "0"


@router.get("/")
def index(request: Request):
    return templates.TemplateResponse(
        "ui.html",
        {"request": request, "asset_version": _asset_version()},
    )
