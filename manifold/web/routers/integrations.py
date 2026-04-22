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

# Serialize rebind calls — without this, two concurrent threads can each
# fetch the provider list, both iterate it, and each POST creates a brand-new
# provider, so running two rebinds at once doubles providers instead of
# rebinding in place.
_rebind_lock = threading.Lock()


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
    """Force Jellyfin to re-bind the XMLTV listings provider pointing at
    manifold.xml. DELETE the existing provider, POST a fresh copy with the
    same settings, then trigger guide refresh. Drops stale channel mappings
    and forces Jellyfin to rediscover every channel from the current M3U.

    Self-healing: if we find multiple manifold.xml providers (a previous
    runaway accumulated duplicates, or two concurrent rebinds raced), we
    delete all but one BEFORE the swap so the result is always a single
    provider. Non-manifold XMLTV providers (e.g. channelarr) are untouched.

    Never runs concurrently with itself (module-level lock). Bails with a
    clear error rather than adding duplicates if any DELETE doesn't confirm
    — the #1 mode of runaway accumulation was silent DELETE 500s while
    POSTs kept creating new providers.
    """
    if not _rebind_lock.acquire(blocking=False):
        logger.info("[INTEGRATION] Skipping rebind — another rebind is already running")
        return {"ok": False, "error": "rebind already in progress"}
    try:
        base = url.rstrip("/")
        headers = _jf_headers(api_key)

        r = requests.get(f"{base}/System/Configuration/livetv", headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
        livetv = r.json()
        providers = livetv.get("ListingProviders", []) or []

        matches = [p for p in providers
                   if (p.get("Type") or "").lower() == "xmltv"
                   and "manifold.xml" in (p.get("Path") or "").lower()
                   and p.get("Id")]

        if not matches:
            # Self-heal: Jellyfin has no XMLTV provider pointing at
            # manifold.xml (deleted, never configured, Jellyfin upgrade wiped
            # it, etc.). Create one bound to whatever M3U tuners are present
            # so the rest of the pipeline has something to talk to.
            tuners = livetv.get("TunerHosts", []) or []
            tuner_ids = [t.get("Id") for t in tuners if t.get("Id")]
            if not tuner_ids:
                return {"ok": False,
                        "error": "Cannot auto-create XMLTV provider: no TunerHosts in Jellyfin"}
            xmltv_path = get_setting("jellyfin_xmltv_path") or "/media/m3u/manifold.xml"
            fresh_provider = {
                "Type": "xmltv",
                "Path": xmltv_path,
                "EnableAllTuners": True,
                "EnabledTuners": tuner_ids,
                "EnableNewProgramIds": True,
            }
            try:
                pr = requests.post(f"{base}/LiveTv/ListingProviders",
                                   headers=headers, json=fresh_provider,
                                   timeout=_TIMEOUT)
                pr.raise_for_status()
            except Exception as e:
                logger.warning("[INTEGRATION] Auto-create XMLTV provider failed: %s", e)
                return {"ok": False, "error": f"auto-create failed: {e}"}
            logger.info("[INTEGRATION] Auto-created missing XMLTV provider at %s", xmltv_path)
            refresh_result = refresh_jellyfin(url, api_key)
            return {"ok": True, "created": 1, "refresh": refresh_result}

        # Self-healing: keep only the first, delete any extras up front.
        # This converges to a single provider even if the state is polluted.
        if len(matches) > 1:
            logger.warning("[INTEGRATION] Found %d manifold.xml providers — "
                           "deleting extras before rebind", len(matches))
            for extra in matches[1:]:
                try:
                    dr = requests.delete(f"{base}/LiveTv/ListingProviders",
                                         headers=headers,
                                         params={"id": extra["Id"]},
                                         timeout=_TIMEOUT)
                    dr.raise_for_status()
                except Exception as e:
                    logger.warning("[INTEGRATION] Could not delete duplicate %s: %s",
                                   extra["Id"], e)

        keeper = matches[0]
        pid = keeper["Id"]
        # Snapshot all existing settings verbatim; drop the Id so Jellyfin
        # generates a new one and treats it as a fresh provider.
        fresh = {k: v for k, v in keeper.items() if k != "Id"}

        # DELETE MUST succeed before we POST. Silent DELETE failures are
        # exactly how we ended up with tens of thousands of duplicates.
        try:
            dr = requests.delete(f"{base}/LiveTv/ListingProviders", headers=headers,
                                 params={"id": pid}, timeout=_TIMEOUT)
            dr.raise_for_status()
        except Exception as e:
            logger.warning("[INTEGRATION] DELETE of provider %s failed (%s) — "
                           "skipping POST to avoid duplicating", pid, e)
            return {"ok": False, "error": f"delete failed: {e}"}

        try:
            pr = requests.post(f"{base}/LiveTv/ListingProviders", headers=headers,
                               json=fresh, timeout=_TIMEOUT)
            pr.raise_for_status()
        except Exception as e:
            logger.warning("[INTEGRATION] POST of fresh provider failed: %s", e)
            return {"ok": False, "error": f"post failed: {e}"}

        rebound = 1
        logger.info("[INTEGRATION] Rebound Jellyfin XMLTV provider: %s", keeper.get("Path"))

        # Refresh guide so the re-bound provider parses the XMLTV immediately.
        refresh_result = refresh_jellyfin(url, api_key)
        return {"ok": True, "rebound": rebound, "refresh": refresh_result}
    except Exception as e:
        logger.exception("[INTEGRATION] Jellyfin rebind failed")
        return {"ok": False, "error": str(e)}
    finally:
        _rebind_lock.release()


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
