"""Media router — images, bumps, and file browser endpoints."""

import os
import threading

from fastapi import APIRouter, Query, Body, HTTPException
from fastapi.responses import Response, FileResponse, JSONResponse

router = APIRouter()

BROWSE_ROOT = os.getenv("BROWSE_ROOT", "/browse")


# ── Programme Images ─────────────────────────────────────────────────────

@router.post("/images/enrich")
def enrich_images():
    from manifold.services.image_enricher import ImageEnricherService
    status = ImageEnricherService.get_status()
    if status["running"]:
        return JSONResponse({"error": "enrichment already running"}, status_code=409)
    thread = threading.Thread(target=ImageEnricherService.enrich_all, daemon=True)
    thread.start()
    return {"ok": True, "message": "Image enrichment started"}


@router.get("/images/status")
def image_enrichment_status():
    from manifold.services.image_enricher import ImageEnricherService
    return ImageEnricherService.get_status()


@router.get("/images/stats")
def image_enrichment_stats():
    from manifold.services.image_enricher import ImageEnricherService
    return ImageEnricherService.get_stats()


@router.post("/images/stop")
def stop_image_enrichment():
    from manifold.services.image_enricher import ImageEnricherService
    ImageEnricherService.stop()
    return {"ok": True, "message": "Stop signal sent"}


# ── Bumps ────────────────────────────────────────────────────────────────

@router.get("/bumps")
def list_bumps():
    from manifold.services.bump_manager import BumpManager
    return BumpManager.get_all()


@router.post("/bumps/scan")
def scan_bumps():
    from manifold.services.bump_manager import BumpManager
    return BumpManager.scan()


@router.delete("/bumps/clip")
def delete_bump(data: dict = Body(default={})):
    from manifold.services.bump_manager import BumpManager
    path = data.get("path", "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    ok = BumpManager.delete_clip(path)
    if not ok:
        return JSONResponse({"error": "not found or outside bumps directory"}, status_code=404)
    return {"ok": True}


@router.post("/bumps/download")
def download_bump(data: dict = Body(default={})):
    from manifold.services.bump_manager import BumpManager
    url = data.get("url", "").strip()
    folder = data.get("folder", "").strip()
    resolution = data.get("resolution", "1080").strip()
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)
    if not folder:
        return JSONResponse({"error": "folder required"}, status_code=400)
    if resolution not in ("480", "720", "1080"):
        resolution = "1080"
    BumpManager.download_url(url, folder, resolution)
    return {"ok": True, "message": f"Downloading to {folder}/ (max {resolution}p)..."}


@router.get("/bumps/thumbnail")
def bump_thumbnail(path: str = Query(default="")):
    from manifold.services.bump_manager import BumpManager
    path = path.strip()
    if not path:
        raise HTTPException(status_code=400)
    data = BumpManager.get_thumbnail(path)
    if not data:
        raise HTTPException(status_code=404)
    return Response(content=data, media_type="image/jpeg")


@router.get("/bumps/preview")
def preview_bump(path: str = Query(default="")):
    from manifold.services.bump_manager import BUMPS_DIR
    path = path.strip()
    if not path:
        raise HTTPException(status_code=400)
    normalized = os.path.normpath(path)
    if not normalized.startswith(os.path.normpath(BUMPS_DIR)):
        raise HTTPException(status_code=403)
    if not os.path.isfile(normalized):
        raise HTTPException(status_code=404)
    return FileResponse(normalized)


# ── File Browser ─────────────────────────────────────────────────────────

@router.get("/browse")
def browse_files(path: str = Query(default="")):
    if not path:
        path = BROWSE_ROOT
    path = os.path.normpath(path)
    if not path.startswith(os.path.normpath(BROWSE_ROOT)):
        return JSONResponse({"error": "outside browse root"}, status_code=403)
    if not os.path.isdir(path):
        return JSONResponse({"error": "not a directory"}, status_code=400)

    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if name.startswith("."):
                continue
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "type": "dir"})
            elif os.path.isfile(full):
                ext = os.path.splitext(name)[1].lower()
                if ext in (".m3u", ".m3u8", ".txt", ".xml"):
                    entries.append({"name": name, "path": full, "type": "file"})
    except PermissionError:
        return JSONResponse({"error": "permission denied"}, status_code=403)

    parent = os.path.dirname(path) if path != "/" else None
    return {"path": path, "parent": parent, "entries": entries}
