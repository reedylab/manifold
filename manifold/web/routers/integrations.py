"""Integration management API for Jellyfin.

Triggers Jellyfin's guide refresh scheduled task after M3U/XMLTV regen so
Jellyfin picks up the new data without waiting for its own cache TTL.

Important: never modify Jellyfin tuner host URLs or XMLTV listing paths.
Those live in Jellyfin's Live TV settings with Jellyfin's own container
mount points and differ from manifold's. Overwriting them wipes channels.
"""

import logging
import threading

import requests
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from manifold.config import get_setting, set_setting

router = APIRouter(tags=["integrations"])
logger = logging.getLogger(__name__)

_TIMEOUT = 10


class JellyfinConfig(BaseModel):
    url: str = ""
    api_key: str = ""
    auto_refresh: bool = False
    rebind_mode: bool = False


class SyncRequest(BaseModel):
    m3u_source: str = ""
    epg_source: str = ""
    regenerate: bool = True


def _jf_headers(api_key: str) -> dict:
    return {"X-MediaBrowser-Token": api_key, "Content-Type": "application/json"}


def test_jellyfin(url: str, api_key: str) -> dict:
    try:
        r = requests.get(
            f"{url.rstrip('/')}/System/Info",
            headers=_jf_headers(api_key),
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        info = r.json()
        return {
            "ok": True,
            "server_name": info.get("ServerName", ""),
            "version": info.get("Version", ""),
        }
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Connection refused — check URL"}
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            return {"ok": False, "error": "Unauthorized — check API key"}
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def refresh_jellyfin(url: str, api_key: str) -> dict:
    """Trigger Jellyfin's guide data refresh scheduled task."""
    base = url.rstrip("/")
    headers = _jf_headers(api_key)
    try:
        r = requests.get(f"{base}/ScheduledTasks", headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        for task in r.json():
            name = (task.get("Name") or "").lower()
            key = (task.get("Key") or "").lower()
            if "guide" in name or "guide" in key:
                requests.post(
                    f"{base}/ScheduledTasks/Running/{task['Id']}",
                    headers=headers,
                    timeout=_TIMEOUT,
                )
                logger.info("[INTEGRATION] Triggered Jellyfin guide refresh")
                return {"ok": True}
        return {"ok": False, "error": "Guide refresh task not found"}
    except Exception as e:
        logger.exception("[INTEGRATION] Jellyfin refresh failed")
        return {"ok": False, "error": str(e)}


def rebind_jellyfin(url: str, api_key: str) -> dict:
    """Force Jellyfin to re-bind all XMLTV listings providers pointing at
    manifold.xml. Snapshots each matching provider config, DELETEs it, then
    re-POSTs with the same settings — which drops stale channel mappings and
    re-auto-matches against the current M3U/XMLTV. Guide refresh fires after.

    Non-manifold XMLTV providers (e.g. a separate channelarr listing) are
    left alone so this can't nuke other integrations.
    """
    base = url.rstrip("/")
    headers = _jf_headers(api_key)
    try:
        r = requests.get(f"{base}/System/Configuration/livetv", headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        livetv = r.json()
        providers = livetv.get("ListingProviders", []) or []

        rebound = 0
        for p in providers:
            if (p.get("Type") or "").lower() != "xmltv":
                continue
            path = (p.get("Path") or "").lower()
            if "manifold.xml" not in path:
                continue
            pid = p.get("Id")
            if not pid:
                continue
            # Snapshot all existing settings verbatim; drop the Id so Jellyfin
            # generates a new one and treats it as a fresh provider.
            fresh = {k: v for k, v in p.items() if k != "Id"}
            try:
                requests.delete(f"{base}/LiveTv/ListingProviders", headers=headers,
                                params={"id": pid}, timeout=_TIMEOUT)
                requests.post(f"{base}/LiveTv/ListingProviders", headers=headers,
                              json=fresh, timeout=_TIMEOUT).raise_for_status()
                rebound += 1
                logger.info("[INTEGRATION] Rebound Jellyfin XMLTV provider: %s", p.get("Path"))
            except Exception as e:
                logger.warning("[INTEGRATION] Rebind failed for provider %s: %s", pid, e)

        if rebound == 0:
            return {"ok": False, "error": "No XMLTV provider found pointing at manifold.xml"}

        # Refresh guide so the re-bound provider parses the XMLTV immediately.
        refresh_result = refresh_jellyfin(url, api_key)
        return {"ok": True, "rebound": rebound, "refresh": refresh_result}
    except Exception as e:
        logger.exception("[INTEGRATION] Jellyfin rebind failed")
        return {"ok": False, "error": str(e)}


def _refresh_or_rebind(url: str, api_key: str) -> dict:
    """Dispatch based on the persistent jellyfin_rebind_mode setting."""
    if get_setting("jellyfin_rebind_mode") == "true":
        return rebind_jellyfin(url, api_key)
    return refresh_jellyfin(url, api_key)


@router.get("/integrations/status")
def integrations_status():
    jf_url = get_setting("jellyfin_url", "") or ""
    jf_key = get_setting("jellyfin_api_key", "") or ""
    return {
        "jellyfin": {
            "url": jf_url,
            "api_key": jf_key,
            "auto_refresh": get_setting("jellyfin_auto_refresh") == "true",
            "rebind_mode": get_setting("jellyfin_rebind_mode") == "true",
            "configured": bool(jf_url and jf_key),
        }
    }


@router.put("/integrations/jellyfin/config")
def jellyfin_save_config(body: JellyfinConfig):
    set_setting("jellyfin_url", body.url)
    set_setting("jellyfin_api_key", body.api_key)
    set_setting("jellyfin_auto_refresh", "true" if body.auto_refresh else "false")
    set_setting("jellyfin_rebind_mode", "true" if body.rebind_mode else "false")
    return {"ok": True}


@router.post("/integrations/jellyfin/test")
def jellyfin_test():
    url = get_setting("jellyfin_url", "") or ""
    key = get_setting("jellyfin_api_key", "") or ""
    if not url or not key:
        return {"ok": False, "error": "Jellyfin URL and API key required"}
    return test_jellyfin(url, key)


@router.post("/integrations/jellyfin/refresh")
def jellyfin_refresh():
    url = get_setting("jellyfin_url", "") or ""
    key = get_setting("jellyfin_api_key", "") or ""
    if not url or not key:
        return {"ok": False, "error": "Jellyfin URL and API key required"}

    mode = "rebind" if get_setting("jellyfin_rebind_mode") == "true" else "refresh"

    def _run():
        result = _refresh_or_rebind(url, key)
        if not result.get("ok"):
            logger.warning("[INTEGRATION] Jellyfin %s failed: %s", mode, result.get("error"))

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"Jellyfin {mode} started", "mode": mode}


def auto_push_jellyfin():
    """Push updates to Jellyfin if auto-refresh is enabled. Called after M3U/XMLTV regen."""
    if get_setting("jellyfin_auto_refresh") != "true":
        return
    url = get_setting("jellyfin_url", "") or ""
    key = get_setting("jellyfin_api_key", "") or ""
    if not (url and key):
        return

    def _run():
        try:
            result = _refresh_or_rebind(url, key)
            if result.get("ok"):
                logger.info("[INTEGRATION] Auto-push to Jellyfin succeeded")
            else:
                logger.warning("[INTEGRATION] Auto-push to Jellyfin failed: %s", result.get("error"))
        except Exception as e:
            logger.warning("[INTEGRATION] Auto-push to Jellyfin error: %s", e)

    threading.Thread(target=_run, daemon=True).start()


@router.post("/integrations/sync")
def integrations_sync(body: SyncRequest):
    """Upstream-feeder hook: ingest named M3U and/or EPG sources, then regen.

    Meant to be called by apps that feed manifold (e.g. channelarr) after
    they publish updated playlists/EPG. The caller names the sources by
    human-readable name so no internal UUIDs leak across system boundaries.
    Regeneration chains to Jellyfin auto-push via the existing regen path.
    """
    from manifold.database import get_session
    from manifold.models.m3u_source import M3uSource
    from manifold.models.epg_source import EpgSource
    from manifold.services.m3u_ingest import M3uIngestService
    from manifold.services.epg_ingest import EpgIngestService
    from manifold.services.m3u_generator import M3UGeneratorService
    from manifold.services.xmltv_generator import XMLTVGeneratorService

    result = {"ok": True, "ingested": {}}
    m3u_src_id = None

    if body.m3u_source:
        with get_session() as s:
            src = s.query(M3uSource).filter(M3uSource.name == body.m3u_source).first()
            if not src:
                return JSONResponse(
                    {"ok": False, "error": f"M3U source {body.m3u_source!r} not found"},
                    status_code=404,
                )
            m3u_src_id = src.id
        logger.info("[INTEGRATION] Sync: ingesting M3U source %r", body.m3u_source)
        result["ingested"]["m3u"] = M3uIngestService.ingest_source(m3u_src_id)

    # If the caller named an EPG source explicitly, ingest that one. Otherwise,
    # if we just ingested an M3U source, auto-chain any EPG sources linked to
    # it so callers don't have to know the EPG name (channelarr pattern:
    # "push my M3U, manifold figures out which EPG ties to it").
    epg_ingests = []
    if body.epg_source:
        with get_session() as s:
            src = s.query(EpgSource).filter(EpgSource.name == body.epg_source).first()
            if not src:
                return JSONResponse(
                    {"ok": False, "error": f"EPG source {body.epg_source!r} not found"},
                    status_code=404,
                )
            epg_ingests.append((src.id, src.name))
    elif m3u_src_id:
        with get_session() as s:
            linked = s.query(EpgSource).filter(EpgSource.m3u_source_id == m3u_src_id).all()
            epg_ingests = [(r.id, r.name) for r in linked]

    for eid, ename in epg_ingests:
        logger.info("[INTEGRATION] Sync: ingesting EPG source %r", ename)
        # With multiple linked EPGs, return the list; single → preserve old shape.
        r = EpgIngestService.ingest_source(eid)
        if len(epg_ingests) == 1:
            result["ingested"]["epg"] = r
        else:
            result["ingested"].setdefault("epg", []).append({"name": ename, **r})

    if body.regenerate:
        logger.info("[INTEGRATION] Sync: regenerating outputs")
        m3u_count = M3UGeneratorService.generate()
        xmltv_result = XMLTVGeneratorService.generate()
        result["generated"] = {
            "m3u_channels": m3u_count,
            "xmltv": xmltv_result if isinstance(xmltv_result, dict) else {"xmltv_channels": xmltv_result},
        }
        try:
            auto_push_jellyfin()
        except Exception as e:
            logger.warning("[INTEGRATION] Sync: auto-push hook failed: %s", e)

    return result
