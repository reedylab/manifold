"""Output router — serve generated M3U and XMLTV files."""

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from manifold.config import Config

router = APIRouter()


@router.get("/manifold.m3u")
def serve_m3u():
    path = os.path.join(Config.OUTPUT_DIR, "manifold.m3u")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="M3U not yet generated")
    return FileResponse(path, media_type="application/octet-stream", filename="manifold.m3u")


@router.get("/manifold.xml")
def serve_xmltv():
    path = os.path.join(Config.OUTPUT_DIR, "manifold.xml")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="XMLTV not yet generated")
    return FileResponse(path, media_type="application/octet-stream", filename="manifold.xml")


@router.get("/program-image/{filename}")
def serve_program_image(filename: str):
    program_image_dir = os.path.join(Config.DATA_DIR, "program_images")
    path = os.path.join(program_image_dir, filename)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        path, media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
