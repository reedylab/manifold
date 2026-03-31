"""Channels router — CRUD endpoints for channel management."""

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from manifold.services.channel_manager import ChannelManagerService
from manifold.database import get_session
from manifold.models.manifest import Manifest

router = APIRouter()


@router.get("/channels")
def list_channels():
    return ChannelManagerService.get_all_channels()


@router.put("/channels/{manifest_id}")
def update_channel(manifest_id: str, data: dict = Body(default={})):
    ok = ChannelManagerService.update_channel(manifest_id, data)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@router.delete("/channels/{manifest_id}")
def delete_channel(manifest_id: str):
    ok = ChannelManagerService.delete_channel(manifest_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True}


@router.post("/channels/bulk-delete")
def bulk_delete_channels(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        deleted = session.query(Manifest).filter(Manifest.id.in_(ids)).delete(synchronize_session="fetch")
    return {"ok": True, "deleted": deleted}


@router.post("/channels/{manifest_id}/toggle")
def toggle_channel(manifest_id: str, data: dict = Body(default={})):
    active = data.get("active", True)
    ok = ChannelManagerService.toggle_channel(manifest_id, active)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "active": active}


@router.post("/channels/renumber")
def renumber_channels(data: dict = Body(default={})):
    start = int(data.get("start", 1))
    ids = data.get("ids")
    with get_session() as session:
        query = (
            session.query(Manifest)
            .filter(Manifest.active == True)
            .filter(
                Manifest.tags.op("@>")('["live"]')
                | Manifest.tags.op("@>")('["event"]')
            )
        )
        if ids:
            query = query.filter(Manifest.id.in_(ids))
        channels = query.order_by(Manifest.channel_number.asc().nullslast(), Manifest.title).all()
        count = 0
        for i, m in enumerate(channels):
            m.channel_number = start + i
            count += 1
    return {"ok": True, "updated": count}


@router.post("/channels/bulk-activate")
def bulk_activate_channels(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        activated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: True}, synchronize_session="fetch"
        )
    return {"ok": True, "activated": activated}


@router.post("/channels/bulk-deactivate")
def bulk_deactivate_channels(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        deactivated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: False}, synchronize_session="fetch"
        )
    return {"ok": True, "deactivated": deactivated}
