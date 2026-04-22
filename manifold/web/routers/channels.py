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
    """Rebuild channel numbers from the tag-driven rule engine.

    Wipes existing numbers in the chosen scope first, then reassigns from the
    per-tag ranges defined in settings. Manual numbers in-scope are overwritten.
    Out-of-scope channels keep their numbers and are treated as taken.

    Optional scope filters (no filter = all active channels):
      ids:  list of channel IDs to limit the rebuild to
      tags: list of primary_tag values to limit the rebuild to
    """
    from manifold.services.autonumber import AutoNumberer, get_number_ranges

    ids = data.get("ids") or None
    tags = data.get("tags") or None
    number_ranges = get_number_ranges()

    with get_session() as session:
        query = session.query(Manifest).filter(Manifest.active == True)
        if ids:
            query = query.filter(Manifest.id.in_(ids))
        if tags:
            query = query.filter(Manifest.primary_tag.in_(tags))
        candidates = query.all()
        # Pinned channels are user-locked — their numbers are preserved across
        # renumber. Their numbers still count as "taken" so auto channels
        # don't collide.
        pinned = [m for m in candidates if m.channel_number_pinned]
        in_scope = [m for m in candidates if not m.channel_number_pinned]
        in_scope_ids = {m.id for m in in_scope}

        taken: set[int] = set()
        for row_id, row_num in session.query(Manifest.id, Manifest.channel_number).filter(
            Manifest.channel_number.isnot(None)
        ):
            if row_id not in in_scope_ids:
                taken.add(row_num)

        for m in in_scope:
            m.channel_number = None

        numberer = AutoNumberer(number_ranges, taken)
        in_scope.sort(key=lambda m: (m.primary_tag or "~", m.title or ""))

        assigned = 0
        for m in in_scope:
            new_num = numberer.assign(None, m.primary_tag)
            if new_num is not None:
                m.channel_number = new_num
                assigned += 1

    return {"ok": True, "scope": len(in_scope), "assigned": assigned,
            "pinned_preserved": len(pinned)}


@router.post("/channels/{manifest_id}/reset-activation")
def reset_channel_activation(manifest_id: str):
    ok = ChannelManagerService.reset_activation(manifest_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    return {"ok": True, "activation_mode": "auto"}


@router.post("/channels/recompute-tags")
def recompute_all_tags():
    """Re-apply current tag rules to every channel based on its stored title
    and URL. Useful after editing rules when some channels aren't currently
    in their source M3U (force_on rows in particular)."""
    from manifold.services.tag_rules import recompute_tags_for_all
    return recompute_tags_for_all()


@router.post("/channels/bulk-activate")
def bulk_activate_channels(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        activated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: True, Manifest.activation_mode: "force_on"},
            synchronize_session="fetch",
        )
    return {"ok": True, "activated": activated}


@router.post("/channels/bulk-deactivate")
def bulk_deactivate_channels(data: dict = Body(default={})):
    ids = data.get("ids", [])
    if not ids:
        return JSONResponse({"error": "ids required"}, status_code=400)
    with get_session() as session:
        deactivated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: False, Manifest.activation_mode: "force_off"},
            synchronize_session="fetch",
        )
    return {"ok": True, "deactivated": deactivated}
