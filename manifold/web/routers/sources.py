"""Sources router — EPG and M3U source management endpoints."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from manifold.services.m3u_ingest import M3uIngestService
from manifold.services.epg_ingest import EpgIngestService
from manifold.database import get_session
from manifold.models.epg import Epg

router = APIRouter()


# ── EPG ──────────────────────────────────────────────────────────────────

@router.get("/epg")
def list_epg():
    with get_session() as session:
        rows = session.query(
            Epg.id, Epg.channel_id, Epg.channel_name, Epg.last_updated
        ).order_by(Epg.channel_name).all()

    entries = []
    for row in rows:
        entries.append({
            "id": row[0],
            "channel_id": row[1],
            "channel_name": row[2],
            "last_updated": row[3].isoformat() if row[3] else None,
        })
    return entries


@router.put("/epg/mapping")
def update_epg_mapping(data: dict = Body(default={})):
    epg_id = data.get("epg_id")
    new_channel_name = data.get("channel_name")
    if not epg_id or not new_channel_name:
        return JSONResponse({"error": "epg_id and channel_name required"}, status_code=400)
    with get_session() as session:
        epg = session.query(Epg).filter_by(id=epg_id).first()
        if not epg:
            return JSONResponse({"error": "not found"}, status_code=404)
        epg.channel_name = new_channel_name
    return {"ok": True}


@router.post("/epg/bulk-delete")
def bulk_delete_epg(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        deleted = session.query(Epg).filter(Epg.id.in_(ids)).delete(synchronize_session="fetch")
    return {"ok": True, "deleted": deleted}


# ── EPG Sources ──────────────────────────────────────────────────────────

@router.get("/epg-sources")
def list_epg_sources():
    return EpgIngestService.get_sources()


@router.post("/epg-sources")
def add_epg_source(data: dict = Body(default={})):
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    m3u_source_id = data.get("m3u_source_id", "").strip()
    if not name or not url or not m3u_source_id:
        return JSONResponse({"error": "name, url, and m3u_source_id required"}, status_code=400)
    result = EpgIngestService.add_source(name, url, m3u_source_id)
    if "error" in result:
        return JSONResponse(result, status_code=400)
    return result


@router.put("/epg-sources/{source_id}")
def update_epg_source(source_id: str, data: dict = Body(default={})):
    from manifold.models.epg_source import EpgSource
    with get_session() as session:
        source = session.query(EpgSource).filter_by(id=source_id).first()
        if not source:
            return JSONResponse({"error": "not found"}, status_code=404)
        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                return JSONResponse({"error": "name cannot be empty"}, status_code=400)
            source.name = name
        if "url" in data:
            url = (data.get("url") or "").strip()
            if not url:
                return JSONResponse({"error": "url cannot be empty"}, status_code=400)
            source.url = url
        if "m3u_source_id" in data:
            mid = (data.get("m3u_source_id") or "").strip()
            if not mid:
                return JSONResponse({"error": "m3u_source_id cannot be empty"}, status_code=400)
            source.m3u_source_id = mid
    return {"ok": True}


@router.delete("/epg-sources/{source_id}")
def delete_epg_source(source_id: str):
    ok = EpgIngestService.delete_source(source_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@router.post("/epg-sources/bulk-delete")
def bulk_delete_epg_sources(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    from manifold.models.epg_source import EpgSource
    with get_session() as session:
        session.query(Epg).filter(Epg.epg_source_id.in_(ids)).delete(synchronize_session="fetch")
        deleted = session.query(EpgSource).filter(EpgSource.id.in_(ids)).delete(synchronize_session="fetch")
    return {"ok": True, "deleted": deleted}


@router.post("/epg-sources/ingest")
def ingest_all_epg():
    return EpgIngestService.ingest_all()


@router.post("/epg-sources/{source_id}/ingest")
def ingest_epg_source(source_id: str):
    result = EpgIngestService.ingest_source(source_id)
    if "error" in result and result.get("channels") is None:
        return JSONResponse(result, status_code=404)
    return result


# ── M3U Sources ──────────────────────────────────────────────────────────

@router.get("/m3u-sources")
def list_m3u_sources():
    return M3uIngestService.get_sources()


@router.post("/m3u-sources")
def add_m3u_source(data: dict = Body(default={})):
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    if not name or not url:
        return JSONResponse({"error": "name and url required"}, status_code=400)
    result = M3uIngestService.add_source(name, url)
    if "error" in result:
        return JSONResponse(result, status_code=409)
    return result


@router.put("/m3u-sources/{source_id}")
def update_m3u_source(source_id: str, data: dict = Body(default={})):
    from manifold.models.m3u_source import M3uSource
    with get_session() as session:
        source = session.query(M3uSource).filter_by(id=source_id).first()
        if not source:
            return JSONResponse({"error": "not found"}, status_code=404)
        if "stream_mode" in data:
            source.stream_mode = data["stream_mode"]
        if "auto_activate" in data:
            source.auto_activate = bool(data["auto_activate"])
        if "name" in data:
            name = (data.get("name") or "").strip()
            if not name:
                return JSONResponse({"error": "name cannot be empty"}, status_code=400)
            source.name = name
        if "url" in data:
            url = (data.get("url") or "").strip()
            if not url:
                return JSONResponse({"error": "url cannot be empty"}, status_code=400)
            source.url = url
    return {"ok": True}


@router.delete("/m3u-sources/{source_id}")
def delete_m3u_source(source_id: str):
    ok = M3uIngestService.delete_source(source_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@router.post("/m3u-sources/bulk-delete")
def bulk_delete_m3u_sources(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    from manifold.models.m3u_source import M3uSource
    with get_session() as session:
        deleted = session.query(M3uSource).filter(M3uSource.id.in_(ids)).delete(synchronize_session="fetch")
    return {"ok": True, "deleted": deleted}


@router.post("/m3u-sources/ingest")
def ingest_all_m3u():
    return M3uIngestService.ingest_all()


@router.post("/m3u-sources/{source_id}/ingest")
def ingest_m3u_source(source_id: str):
    result = M3uIngestService.ingest_source(source_id)
    if "error" in result and result.get("channels") is None:
        return JSONResponse(result, status_code=404)
    # seen_ids is an internal intermediate used by refresh_all — drop it from the
    # public response.
    result.pop("seen_ids", None)
    return result
