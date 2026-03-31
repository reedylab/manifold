"""UI router — serves the single-page HTML."""

from fastapi import APIRouter, Request

from manifold.web.app import templates

router = APIRouter()


@router.get("/")
def index(request: Request):
    return templates.TemplateResponse("ui.html", {"request": request})
