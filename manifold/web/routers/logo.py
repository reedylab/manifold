"""Logo router — serve and upload channel logos."""

import re

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from manifold.services.logo_manager import LogoManagerService

router = APIRouter()

MANIFEST_ID_RE = re.compile(r"^[a-f0-9-]+$")


@router.get("/{manifest_id}")
def serve_logo(manifest_id: str):
    if not MANIFEST_ID_RE.match(manifest_id):
        raise HTTPException(status_code=400)
    path = LogoManagerService.get_logo_path(manifest_id)
    if not path:
        raise HTTPException(status_code=404)
    return FileResponse(path, media_type="image/png")


@router.post("/{manifest_id}")
async def upload_logo(manifest_id: str, request: Request):
    if not MANIFEST_ID_RE.match(manifest_id):
        raise HTTPException(status_code=400)
    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        form = await request.form()
        f = form.get("file")
        if not f:
            return JSONResponse({"error": "no file"}, status_code=400)
        data = await f.read()
    else:
        data = await request.body()
    if not data or len(data) < 100:
        return JSONResponse({"error": "empty or too small"}, status_code=400)
    ok = LogoManagerService.save_logo(manifest_id, data)
    if not ok:
        return JSONResponse({"error": "save failed"}, status_code=500)
    from manifold.database import get_session
    from manifold.models.manifest import Manifest
    with get_session() as session:
        m = session.query(Manifest).filter_by(id=manifest_id).first()
        if m:
            m.logo_cached = True
    return {"ok": True}
