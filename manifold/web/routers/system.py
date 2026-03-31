"""System router — settings, scheduler, logs, vpn, system stats, streams, generate."""

import os
import logging

import psutil
from fastapi import APIRouter, Query, Body
from fastapi.responses import JSONResponse

from manifold.config import Config, get_setting, set_setting
from manifold.services.m3u_generator import M3UGeneratorService
from manifold.services.xmltv_generator import XMLTVGeneratorService
from manifold.scheduler import get_jobs_info, update_job_interval, run_job_now

logger = logging.getLogger(__name__)

router = APIRouter()

SETTING_KEYS = [
    "bridge_host", "bridge_port",
    "scheduler_regen_minutes", "scheduler_cleanup_hours", "scheduler_activation_hours",
    "dummy_epg_days", "dummy_epg_block_minutes",
    "scheduler_image_enrichment_hours", "tmdb_api_key", "fanart_api_key",
    "scheduler_m3u_refresh_hours", "scheduler_epg_refresh_hours",
]


# ── Generate ─────────────────────────────────────────────────────────────

@router.post("/generate")
def generate():
    m3u_count = M3UGeneratorService.generate()
    xmltv_result = XMLTVGeneratorService.generate()
    if isinstance(xmltv_result, dict):
        return {"ok": True, "m3u_channels": m3u_count, **xmltv_result}
    return {"ok": True, "m3u_channels": m3u_count, "xmltv_channels": xmltv_result}


# ── Scheduler ────────────────────────────────────────────────────────────

@router.get("/scheduler")
def list_scheduler():
    return get_jobs_info()


@router.put("/scheduler/{job_id}")
def update_scheduler_job(job_id: str, data: dict = Body(default={})):
    seconds = data.get("interval_seconds")
    if not seconds or int(seconds) < 10:
        return JSONResponse({"error": "interval_seconds must be >= 10"}, status_code=400)
    ok = update_job_interval(job_id, int(seconds))
    if not ok:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return {"ok": True}


@router.post("/scheduler/{job_id}/run")
def run_scheduler_job(job_id: str):
    ok = run_job_now(job_id)
    if not ok:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return {"ok": True}


# ── Streams ──────────────────────────────────────────────────────────────

@router.get("/streams")
def list_streams():
    from manifold.services.stream_manager import StreamManagerService
    return StreamManagerService.list_active()


@router.get("/streams/{manifest_id}")
def stream_status(manifest_id: str):
    from manifold.services.stream_manager import StreamManagerService
    status = StreamManagerService.get_status(manifest_id)
    if not status:
        return JSONResponse({"error": "not running"}, status_code=404)
    return status


@router.post("/streams/{manifest_id}/stop")
def stop_stream(manifest_id: str):
    from manifold.services.stream_manager import StreamManagerService
    StreamManagerService.stop_stream(manifest_id)
    return {"ok": True}


# ── Settings ─────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings():
    cfg = Config()
    result = {}
    for key in SETTING_KEYS:
        result[key] = get_setting(key)
    if not result.get("bridge_host"):
        result["bridge_host"] = cfg.BRIDGE_HOST
    if not result.get("bridge_port"):
        result["bridge_port"] = cfg.BRIDGE_PORT
    if not result.get("scheduler_regen_minutes"):
        result["scheduler_regen_minutes"] = "5"
    if not result.get("scheduler_cleanup_hours"):
        result["scheduler_cleanup_hours"] = "1"
    if not result.get("scheduler_activation_hours"):
        result["scheduler_activation_hours"] = "4"
    if not result.get("dummy_epg_days"):
        result["dummy_epg_days"] = "7"
    if not result.get("dummy_epg_block_minutes"):
        result["dummy_epg_block_minutes"] = "30"
    if not result.get("scheduler_image_enrichment_hours"):
        result["scheduler_image_enrichment_hours"] = "6"
    if not result.get("tmdb_api_key"):
        result["tmdb_api_key"] = ""
    if not result.get("scheduler_m3u_refresh_hours"):
        result["scheduler_m3u_refresh_hours"] = "4"
    if not result.get("scheduler_epg_refresh_hours"):
        result["scheduler_epg_refresh_hours"] = "12"
    result["pg_host"] = cfg.PG_HOST
    result["pg_port"] = cfg.PG_PORT
    result["pg_db"] = cfg.PG_DB
    return result


@router.post("/settings")
def save_settings(data: dict = Body(default={})):
    for key in SETTING_KEYS:
        if key in data:
            set_setting(key, str(data[key]))
    return {"ok": True}


# ── Logs ─────────────────────────────────────────────────────────────────

@router.get("/logs/tail")
def tail_logs(pos: int = Query(default=0)):
    log_path = os.path.join(Config.LOG_DIR, "manifold.log")

    if not os.path.isfile(log_path):
        return {"lines": "", "pos": 0}

    size = os.path.getsize(log_path)
    if pos > size:
        pos = 0

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(pos)
        lines = f.read()
        new_pos = f.tell()

    return {"lines": lines, "pos": new_pos}


# ── VPN ──────────────────────────────────────────────────────────────────

@router.get("/vpn/status")
def vpn_status():
    import requests as http_requests
    cfg = Config()
    if not cfg.GLUETUN_CONTROL_URL:
        return {"enabled": False, "status": "not configured"}
    try:
        auth = None
        if cfg.GLUETUN_CONTROL_USER:
            auth = (cfg.GLUETUN_CONTROL_USER, cfg.GLUETUN_CONTROL_PASS)

        r = http_requests.get(f"{cfg.GLUETUN_CONTROL_URL}/v1/openvpn/status", auth=auth, timeout=3)
        status_data = r.json()
        vpn_status = status_data.get("status", "unknown")

        ip = ""
        country = ""
        city = ""
        try:
            ip_resp = http_requests.get(f"{cfg.GLUETUN_CONTROL_URL}/v1/publicip/ip", auth=auth, timeout=3)
            ip_data = ip_resp.json()
            ip = ip_data.get("public_ip", "")
            country = ip_data.get("country", "")
            city = ip_data.get("city", ip_data.get("region", ""))
        except Exception:
            pass

        if not ip and vpn_status == "running":
            try:
                ext = http_requests.get("https://api.ipify.org?format=json", timeout=5)
                ip = ext.json().get("ip", "")
            except Exception:
                pass
            if ip and not city:
                try:
                    geo = http_requests.get(f"http://ip-api.com/json/{ip}?fields=city,country", timeout=3)
                    geo_data = geo.json()
                    city = geo_data.get("city", "")
                    country = geo_data.get("country", "")
                except Exception:
                    pass

        return {
            "enabled": True,
            "status": vpn_status,
            "ip": ip,
            "country": country,
            "city": city,
        }
    except Exception as e:
        return {"enabled": True, "status": "unreachable", "error": str(e)}


# ── System Stats ─────────────────────────────────────────────────────────

@router.get("/system/stats")
def system_stats():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    return {
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
    }
