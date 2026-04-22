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
    "scheduler_regen_minutes", "scheduler_cleanup_hours",
    "dummy_epg_days", "dummy_epg_block_minutes",
    "scheduler_image_enrichment_hours", "tmdb_api_key", "fanart_api_key",
    "scheduler_m3u_refresh_hours", "scheduler_epg_refresh_hours",
    "vpn_auto_rotate_minutes", "vpn_scheduled_rotate_time",
    "export_strategy", "export_local_path",
    "jellyfin_xmltv_path",
]


# ── Generate ─────────────────────────────────────────────────────────────

@router.post("/generate")
def generate():
    m3u_count = M3UGeneratorService.generate()
    xmltv_result = XMLTVGeneratorService.generate()
    try:
        from manifold.web.routers.integrations import auto_push_jellyfin
        auto_push_jellyfin()
    except Exception as e:
        logger.warning("Auto-push hook failed: %s", e)
    if isinstance(xmltv_result, dict):
        return {"ok": True, "m3u_channels": m3u_count, **xmltv_result}
    return {"ok": True, "m3u_channels": m3u_count, "xmltv_channels": xmltv_result}


# ── Scheduler ────────────────────────────────────────────────────────────

@router.get("/scheduler")
def list_scheduler():
    """List scheduler tasks. Hides vpn_* tasks entirely in local (non-VPN) mode
    so users running manifold without gluetun don't see VPN-related controls
    they can't act on. The latency sampler still runs in the background to
    power the Network Latency chart, just hidden from the task list."""
    cfg = Config()
    jobs = get_jobs_info()
    if not cfg.GLUETUN_CONTROL_URL:
        jobs = [j for j in jobs if not j["id"].startswith("vpn_")]
    return jobs


@router.put("/scheduler/{job_id}")
def update_scheduler_job(job_id: str, data: dict = Body(default={})):
    # Cron-style update for time-of-day jobs (e.g. vpn_scheduled_rotate)
    if "time" in data:
        from manifold.scheduler import update_vpn_scheduled_rotate
        if job_id != "vpn_scheduled_rotate":
            return JSONResponse({"error": "time field only supported for cron jobs"}, status_code=400)
        time_str = (data.get("time") or "").strip()
        ok = update_vpn_scheduled_rotate(time_str)
        if not ok:
            return JSONResponse({"error": "invalid time format (expected HH:MM)"}, status_code=400)
        # Persist so it survives restart
        set_setting("vpn_scheduled_rotate_time", time_str)
        return {"ok": True}

    # Interval-style update (existing behavior)
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
    if not result.get("vpn_auto_rotate_minutes"):
        result["vpn_auto_rotate_minutes"] = "0"
    if not result.get("export_strategy"):
        result["export_strategy"] = "url"
    if result.get("export_local_path") is None:
        result["export_local_path"] = ""
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


# ── Tag Rules ────────────────────────────────────────────────────────────

@router.get("/tag-rules")
def get_tag_rules_endpoint():
    from manifold.services.tag_rules import get_tag_rules
    return get_tag_rules()


@router.put("/tag-rules")
def put_tag_rules_endpoint(data: dict = Body(default={})):
    from manifold.services.tag_rules import set_tag_rules, recompute_tags_for_all
    if not isinstance(data, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    priority = data.get("priority")
    if priority is not None and not isinstance(priority, list):
        return JSONResponse({"error": "priority must be a list"}, status_code=400)
    set_tag_rules(data)
    # Apply the new rules to existing channels immediately so force_on /
    # absent-from-source rows get the new keyword matches without waiting for
    # their next ingest.
    recompute_result = recompute_tags_for_all()
    return {"ok": True, "retagged": recompute_result.get("retagged", 0)}


@router.post("/tag-rules/reset-defaults")
def reset_tag_rules_endpoint():
    from manifold.services.tag_rules import DEFAULT_TAG_RULES, set_tag_rules, get_tag_rules
    set_tag_rules(DEFAULT_TAG_RULES)
    return get_tag_rules()


# ── Number Ranges ────────────────────────────────────────────────────────

@router.get("/number-ranges")
def get_number_ranges_endpoint():
    from manifold.services.autonumber import get_number_ranges
    return get_number_ranges()


@router.put("/number-ranges")
def put_number_ranges_endpoint(data: dict = Body(default={})):
    from manifold.services.autonumber import set_number_ranges
    if not isinstance(data, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    for tag, spec in data.items():
        if not isinstance(spec, dict):
            return JSONResponse({"error": f"'{tag}' must be an object with start/end"}, status_code=400)
        try:
            start = int(spec.get("start"))
            end = int(spec.get("end"))
        except (TypeError, ValueError):
            return JSONResponse({"error": f"'{tag}' start/end must be integers"}, status_code=400)
        if start <= 0 or end < start:
            return JSONResponse({"error": f"'{tag}' requires 0 < start <= end"}, status_code=400)
    set_number_ranges(data)
    return {"ok": True}


@router.post("/number-ranges/reset-defaults")
def reset_number_ranges_endpoint():
    from manifold.services.autonumber import DEFAULT_NUMBER_RANGES, set_number_ranges, get_number_ranges
    set_number_ranges(DEFAULT_NUMBER_RANGES)
    return get_number_ranges()


# ── Activation Rules ─────────────────────────────────────────────────────

@router.get("/activation-rules")
def get_activation_rules_endpoint():
    from manifold.services.activation import get_activation_rules
    return get_activation_rules()


@router.put("/activation-rules")
def put_activation_rules_endpoint(data: dict = Body(default={})):
    from manifold.services.activation import set_activation_rules
    if not isinstance(data, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    tags_auto_on = data.get("tags_auto_on")
    if tags_auto_on is not None and not isinstance(tags_auto_on, list):
        return JSONResponse({"error": "tags_auto_on must be a list"}, status_code=400)
    set_activation_rules(data)
    return {"ok": True}


@router.post("/activation-rules/reset-defaults")
def reset_activation_rules_endpoint():
    from manifold.services.activation import DEFAULT_ACTIVATION_RULES, set_activation_rules, get_activation_rules
    set_activation_rules(DEFAULT_ACTIVATION_RULES)
    return get_activation_rules()


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

        # /v1/vpn/status is the generic endpoint (works for both WireGuard and OpenVPN).
        # Older gluetun versions only had /v1/openvpn/status — try generic first.
        try:
            r = http_requests.get(f"{cfg.GLUETUN_CONTROL_URL}/v1/vpn/status", auth=auth, timeout=3)
            r.raise_for_status()
            status_data = r.json()
        except Exception:
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


@router.get("/vpn/history")
def vpn_history(minutes: int = Query(default=60, ge=1, le=1440)):
    """Return recent VPN latency samples + summary stats for the dashboard chart."""
    from manifold.services.vpn_monitor import get_history, get_summary
    return {
        "summary": get_summary(),
        "samples": get_history(minutes),
    }


@router.get("/vpn/servers")
def vpn_servers(
    sort: str = Query(default="avg_rtt"),
    order: str = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
):
    """List known VPN servers with computed avg latency, success rate, etc.

    sort: avg_rtt | last_seen | total_samples | success_rate | first_seen | connected
    order: asc | desc (sensible defaults per sort)
    """
    from manifold.services.vpn_monitor import list_servers
    return {"servers": list_servers(sort=sort, order=order, limit=limit)}


@router.post("/vpn/rotate")
def vpn_rotate():
    """Manually cycle the gluetun WireGuard tunnel to get a new exit IP.

    Calls gluetun's control API (PUT /v1/vpn/status) to stop and start the
    tunnel, which picks a new random server from the configured SERVER_CITIES.
    Does NOT recreate the gluetun container — attached services keep their
    network namespace.
    """
    from manifold.services.vpn_monitor import rotate_vpn
    result = rotate_vpn(reason="manual")
    if not result.get("ok"):
        return JSONResponse(result, status_code=502)
    return result


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
