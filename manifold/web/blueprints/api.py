"""API blueprint — all /api/* endpoints."""

import os
import logging

import psutil
from flask import Blueprint, jsonify, request

from manifold.config import Config, get_setting, set_setting
from manifold.services.channel_manager import ChannelManagerService
from manifold.services.m3u_generator import M3UGeneratorService
from manifold.services.xmltv_generator import XMLTVGeneratorService
from manifold.services.event_cleanup import EventCleanupService
from manifold.services.m3u_ingest import M3uIngestService
from manifold.services.epg_ingest import EpgIngestService
from manifold.scheduler import get_jobs_info, update_job_interval, run_job_now
from manifold.database import get_session
from manifold.models.epg import Epg
from manifold.models.manifest import Manifest

logger = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)


# ── Health ───────────────────────────────────────────────────────────────
@api_bp.route("/health")
def health():
    return jsonify({"status": "ok"})


# ── Channels ─────────────────────────────────────────────────────────────
@api_bp.route("/channels")
def list_channels():
    channels = ChannelManagerService.get_all_channels()
    return jsonify(channels)


@api_bp.route("/channels/<manifest_id>", methods=["PUT"])
def update_channel(manifest_id):
    data = request.get_json(force=True)
    ok = ChannelManagerService.update_channel(manifest_id, data)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@api_bp.route("/channels/<manifest_id>", methods=["DELETE"])
def delete_channel(manifest_id):
    ok = ChannelManagerService.delete_channel(manifest_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@api_bp.route("/channels/bulk-delete", methods=["POST"])
def bulk_delete_channels():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    deleted = 0
    with get_session() as session:
        deleted = session.query(Manifest).filter(Manifest.id.in_(ids)).delete(synchronize_session="fetch")
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/channels/<manifest_id>/toggle", methods=["POST"])
def toggle_channel(manifest_id):
    data = request.get_json(force=True)
    active = data.get("active", True)
    ok = ChannelManagerService.toggle_channel(manifest_id, active)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "active": active})


@api_bp.route("/channels/renumber", methods=["POST"])
def renumber_channels():
    """Auto-assign sequential channel numbers."""
    data = request.get_json(force=True)
    start = int(data.get("start", 1))
    ids = data.get("ids")  # optional — if provided, only renumber these channels
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
    return jsonify({"ok": True, "updated": count})


@api_bp.route("/channels/bulk-activate", methods=["POST"])
def bulk_activate_channels():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    with get_session() as session:
        activated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: True}, synchronize_session="fetch"
        )
    return jsonify({"ok": True, "activated": activated})


@api_bp.route("/channels/bulk-deactivate", methods=["POST"])
def bulk_deactivate_channels():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    with get_session() as session:
        deactivated = session.query(Manifest).filter(Manifest.id.in_(ids)).update(
            {Manifest.active: False}, synchronize_session="fetch"
        )
    return jsonify({"ok": True, "deactivated": deactivated})


# ── EPG ──────────────────────────────────────────────────────────────────
@api_bp.route("/epg")
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
    return jsonify(entries)


@api_bp.route("/epg/mapping", methods=["PUT"])
def update_epg_mapping():
    """Update the channel_name on an EPG entry to re-map it."""
    data = request.get_json(force=True)
    epg_id = data.get("epg_id")
    new_channel_name = data.get("channel_name")
    if not epg_id or not new_channel_name:
        return jsonify({"error": "epg_id and channel_name required"}), 400

    with get_session() as session:
        epg = session.query(Epg).filter_by(id=epg_id).first()
        if not epg:
            return jsonify({"error": "not found"}), 404
        epg.channel_name = new_channel_name

    return jsonify({"ok": True})


@api_bp.route("/epg/bulk-delete", methods=["POST"])
def bulk_delete_epg():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    with get_session() as session:
        deleted = session.query(Epg).filter(Epg.id.in_(ids)).delete(synchronize_session="fetch")
    return jsonify({"ok": True, "deleted": deleted})


# ── EPG Sources ──────────────────────────────────────────────────────────
@api_bp.route("/epg-sources")
def list_epg_sources():
    return jsonify(EpgIngestService.get_sources())


@api_bp.route("/epg-sources", methods=["POST"])
def add_epg_source():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    m3u_source_id = data.get("m3u_source_id", "").strip()
    if not name or not url or not m3u_source_id:
        return jsonify({"error": "name, url, and m3u_source_id required"}), 400
    result = EpgIngestService.add_source(name, url, m3u_source_id)
    if "error" in result:
        return jsonify(result), 400
    return jsonify(result)


@api_bp.route("/epg-sources/<source_id>", methods=["DELETE"])
def delete_epg_source(source_id):
    ok = EpgIngestService.delete_source(source_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@api_bp.route("/epg-sources/bulk-delete", methods=["POST"])
def bulk_delete_epg_sources():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    from manifold.models.epg_source import EpgSource
    with get_session() as session:
        # Delete related EPG entries first
        session.query(Epg).filter(Epg.epg_source_id.in_(ids)).delete(synchronize_session="fetch")
        deleted = session.query(EpgSource).filter(EpgSource.id.in_(ids)).delete(synchronize_session="fetch")
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/epg-sources/ingest", methods=["POST"])
def ingest_all_epg():
    result = EpgIngestService.ingest_all()
    return jsonify(result)


@api_bp.route("/epg-sources/<source_id>/ingest", methods=["POST"])
def ingest_epg_source(source_id):
    result = EpgIngestService.ingest_source(source_id)
    if "error" in result and result.get("channels") is None:
        return jsonify(result), 404
    return jsonify(result)


# ── M3U Sources ──────────────────────────────────────────────────────────
@api_bp.route("/m3u-sources")
def list_m3u_sources():
    return jsonify(M3uIngestService.get_sources())


@api_bp.route("/m3u-sources", methods=["POST"])
def add_m3u_source():
    data = request.get_json(force=True)
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    if not name or not url:
        return jsonify({"error": "name and url required"}), 400
    result = M3uIngestService.add_source(name, url)
    if "error" in result:
        return jsonify(result), 409
    return jsonify(result)


@api_bp.route("/m3u-sources/<source_id>", methods=["PUT"])
def update_m3u_source(source_id):
    data = request.get_json(force=True)
    from manifold.models.m3u_source import M3uSource
    with get_session() as session:
        source = session.query(M3uSource).filter_by(id=source_id).first()
        if not source:
            return jsonify({"error": "not found"}), 404
        if "stream_mode" in data:
            source.stream_mode = data["stream_mode"]
    return jsonify({"ok": True})


@api_bp.route("/m3u-sources/<source_id>", methods=["DELETE"])
def delete_m3u_source(source_id):
    ok = M3uIngestService.delete_source(source_id)
    if not ok:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@api_bp.route("/m3u-sources/bulk-delete", methods=["POST"])
def bulk_delete_m3u_sources():
    data = request.get_json(force=True)
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"error": "ids required"}), 400
    from manifold.models.m3u_source import M3uSource
    with get_session() as session:
        deleted = session.query(M3uSource).filter(M3uSource.id.in_(ids)).delete(synchronize_session="fetch")
    return jsonify({"ok": True, "deleted": deleted})


@api_bp.route("/m3u-sources/ingest", methods=["POST"])
def ingest_all_m3u():
    result = M3uIngestService.ingest_all()
    return jsonify(result)


@api_bp.route("/m3u-sources/<source_id>/ingest", methods=["POST"])
def ingest_m3u_source(source_id):
    result = M3uIngestService.ingest_source(source_id)
    if "error" in result and result.get("channels") is None:
        return jsonify(result), 404
    return jsonify(result)


# ── Generate ─────────────────────────────────────────────────────────────
@api_bp.route("/generate", methods=["POST"])
def generate():
    m3u_count = M3UGeneratorService.generate()
    xmltv_result = XMLTVGeneratorService.generate()
    if isinstance(xmltv_result, dict):
        return jsonify({"ok": True, "m3u_channels": m3u_count, **xmltv_result})
    return jsonify({"ok": True, "m3u_channels": m3u_count, "xmltv_channels": xmltv_result})


# ── Scheduler ────────────────────────────────────────────────────────────
@api_bp.route("/scheduler")
def list_scheduler():
    return jsonify(get_jobs_info())


@api_bp.route("/scheduler/<job_id>", methods=["PUT"])
def update_scheduler_job(job_id):
    data = request.get_json(force=True)
    seconds = data.get("interval_seconds")
    if not seconds or int(seconds) < 10:
        return jsonify({"error": "interval_seconds must be >= 10"}), 400
    ok = update_job_interval(job_id, int(seconds))
    if not ok:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"ok": True})


@api_bp.route("/scheduler/<job_id>/run", methods=["POST"])
def run_scheduler_job(job_id):
    ok = run_job_now(job_id)
    if not ok:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"ok": True})


# ── Streams ──────────────────────────────────────────────────────────────
@api_bp.route("/streams")
def list_streams():
    from manifold.services.stream_manager import StreamManagerService
    return jsonify(StreamManagerService.list_active())


@api_bp.route("/streams/<manifest_id>")
def stream_status(manifest_id):
    from manifold.services.stream_manager import StreamManagerService
    status = StreamManagerService.get_status(manifest_id)
    if not status:
        return jsonify({"error": "not running"}), 404
    return jsonify(status)


@api_bp.route("/streams/<manifest_id>/stop", methods=["POST"])
def stop_stream(manifest_id):
    from manifold.services.stream_manager import StreamManagerService
    StreamManagerService.stop_stream(manifest_id)
    return jsonify({"ok": True})


# ── Settings ─────────────────────────────────────────────────────────────
SETTING_KEYS = [
    "bridge_host", "bridge_port",
    "scheduler_regen_minutes", "scheduler_cleanup_hours", "scheduler_activation_hours",
    "dummy_epg_days", "dummy_epg_block_minutes",
    "scheduler_image_enrichment_hours", "tmdb_api_key", "fanart_api_key",
    "scheduler_m3u_refresh_hours", "scheduler_epg_refresh_hours",
]


@api_bp.route("/settings")
def get_settings():
    cfg = Config()
    result = {}
    for key in SETTING_KEYS:
        result[key] = get_setting(key)
    # Fill defaults from env
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
    # DB info (read-only)
    result["pg_host"] = cfg.PG_HOST
    result["pg_port"] = cfg.PG_PORT
    result["pg_db"] = cfg.PG_DB
    return jsonify(result)


@api_bp.route("/settings", methods=["POST"])
def save_settings():
    data = request.get_json(force=True)
    for key in SETTING_KEYS:
        if key in data:
            set_setting(key, str(data[key]))
    return jsonify({"ok": True})


# ── Logs ─────────────────────────────────────────────────────────────────
@api_bp.route("/logs/tail")
def tail_logs():
    log_path = os.path.join(Config.LOG_DIR, "manifold.log")
    pos = int(request.args.get("pos", 0))

    if not os.path.isfile(log_path):
        return jsonify({"lines": "", "pos": 0})

    size = os.path.getsize(log_path)
    if pos > size:
        pos = 0  # Log was rotated

    with open(log_path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(pos)
        lines = f.read()
        new_pos = f.tell()

    return jsonify({"lines": lines, "pos": new_pos})


# ── Bumps ────────────────────────────────────────────────────────────────
@api_bp.route("/bumps")
def list_bumps():
    from manifold.services.bump_manager import BumpManager
    return jsonify(BumpManager.get_all())


@api_bp.route("/bumps/scan", methods=["POST"])
def scan_bumps():
    from manifold.services.bump_manager import BumpManager
    return jsonify(BumpManager.scan())


@api_bp.route("/bumps/clip", methods=["DELETE"])
def delete_bump():
    from manifold.services.bump_manager import BumpManager
    data = request.get_json(force=True)
    path = data.get("path", "").strip()
    if not path:
        return jsonify({"error": "path required"}), 400
    ok = BumpManager.delete_clip(path)
    if not ok:
        return jsonify({"error": "not found or outside bumps directory"}), 404
    return jsonify({"ok": True})


@api_bp.route("/bumps/download", methods=["POST"])
def download_bump():
    from manifold.services.bump_manager import BumpManager
    data = request.get_json(force=True)
    url = data.get("url", "").strip()
    folder = data.get("folder", "").strip()
    resolution = data.get("resolution", "1080").strip()
    if not url:
        return jsonify({"error": "url required"}), 400
    if not folder:
        return jsonify({"error": "folder required"}), 400
    if resolution not in ("480", "720", "1080"):
        resolution = "1080"
    BumpManager.download_url(url, folder, resolution)
    return jsonify({"ok": True, "message": f"Downloading to {folder}/ (max {resolution}p)..."})


@api_bp.route("/bumps/thumbnail")
def bump_thumbnail():
    from manifold.services.bump_manager import BumpManager
    from flask import Response
    path = request.args.get("path", "").strip()
    if not path:
        abort(400)
    data = BumpManager.get_thumbnail(path)
    if not data:
        abort(404)
    return Response(data, mimetype="image/jpeg")


@api_bp.route("/bumps/preview")
def preview_bump():
    """Serve a bump clip for browser preview."""
    import os
    from manifold.services.bump_manager import BUMPS_DIR
    from flask import send_file
    path = request.args.get("path", "").strip()
    if not path:
        abort(400)
    normalized = os.path.normpath(path)
    if not normalized.startswith(os.path.normpath(BUMPS_DIR)):
        abort(403)
    if not os.path.isfile(normalized):
        abort(404)
    return send_file(normalized)


# ── VPN ──────────────────────────────────────────────────────────────────
@api_bp.route("/vpn/status")
def vpn_status():
    """Get VPN status from gluetun control server + external IP check."""
    import requests as http_requests
    cfg = Config()
    if not cfg.GLUETUN_CONTROL_URL:
        return jsonify({"enabled": False, "status": "not configured"})
    try:
        auth = None
        if cfg.GLUETUN_CONTROL_USER:
            auth = (cfg.GLUETUN_CONTROL_USER, cfg.GLUETUN_CONTROL_PASS)

        # Get VPN tunnel status
        r = http_requests.get(f"{cfg.GLUETUN_CONTROL_URL}/v1/openvpn/status", auth=auth, timeout=3)
        status_data = r.json()
        vpn_status = status_data.get("status", "unknown")

        # Get public IP — try gluetun first, fallback to external service
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

        # Fallback: get IP from external service if gluetun didn't provide it
        if not ip and vpn_status == "running":
            try:
                ext = http_requests.get("https://api.ipify.org?format=json", timeout=5)
                ip = ext.json().get("ip", "")
            except Exception:
                pass
            # Try geo lookup for the IP
            if ip and not city:
                try:
                    geo = http_requests.get(f"http://ip-api.com/json/{ip}?fields=city,country", timeout=3)
                    geo_data = geo.json()
                    city = geo_data.get("city", "")
                    country = geo_data.get("country", "")
                except Exception:
                    pass

        return jsonify({
            "enabled": True,
            "status": vpn_status,
            "ip": ip,
            "country": country,
            "city": city,
        })
    except Exception as e:
        return jsonify({"enabled": True, "status": "unreachable", "error": str(e)})


# ── Guide ────────────────────────────────────────────────────────────────
@api_bp.route("/guide")
def guide():
    """Return programme data for the TV guide grid, parsed from manifold.xml."""
    from lxml import etree
    from datetime import datetime, timedelta, timezone

    hours = int(request.args.get("hours", 12))
    cfg = Config()
    xmltv_path = os.path.join(cfg.OUTPUT_DIR, "manifold.xml")

    if not os.path.isfile(xmltv_path):
        return jsonify({"channels": [], "start": "", "end": ""})

    try:
        tree = etree.parse(xmltv_path)
        root = tree.getroot()
    except Exception:
        return jsonify({"channels": [], "start": "", "end": ""})

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=hours)

    # Parse channels
    channel_map = {}
    for chan in root.findall(".//channel"):
        cid = chan.get("id", "")
        name_el = chan.find("display-name")
        icon_el = chan.find("icon")
        channel_map[cid] = {
            "id": cid,
            "name": name_el.text if name_el is not None else cid,
            "logo": icon_el.get("src", "") if icon_el is not None else "",
            "programmes": [],
        }

    # Parse programmes
    def _parse_ts(ts):
        try:
            return datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    for prog in root.findall(".//programme"):
        cid = prog.get("channel", "")
        start = _parse_ts(prog.get("start", ""))
        stop = _parse_ts(prog.get("stop", ""))
        if not start or not stop:
            continue
        if stop <= window_start or start >= window_end:
            continue
        if cid not in channel_map:
            continue

        title_el = prog.find("title")
        desc_el = prog.find("desc")
        cat_el = prog.find("category")
        icon_el = prog.find("icon")

        channel_map[cid]["programmes"].append({
            "title": title_el.text if title_el is not None else "",
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "desc": desc_el.text if desc_el is not None else "",
            "category": cat_el.text if cat_el is not None else "",
            "icon": icon_el.get("src", "") if icon_el is not None else "",
        })

    # Only return channels that have programmes in the window
    channels = [ch for ch in channel_map.values() if ch["programmes"]]
    channels.sort(key=lambda c: c["name"])

    return jsonify({
        "channels": channels,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    })


# ── Programme Images ──────────────────────────────────────────────────
@api_bp.route("/images/enrich", methods=["POST"])
def enrich_images():
    """Trigger manual image enrichment run."""
    from manifold.services.image_enricher import ImageEnricherService
    import threading
    status = ImageEnricherService.get_status()
    if status["running"]:
        return jsonify({"error": "enrichment already running"}), 409
    thread = threading.Thread(target=ImageEnricherService.enrich_all, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Image enrichment started"})


@api_bp.route("/images/status")
def image_enrichment_status():
    """Get current enrichment progress."""
    from manifold.services.image_enricher import ImageEnricherService
    return jsonify(ImageEnricherService.get_status())


@api_bp.route("/images/stats")
def image_enrichment_stats():
    """Get image cache statistics."""
    from manifold.services.image_enricher import ImageEnricherService
    return jsonify(ImageEnricherService.get_stats())


@api_bp.route("/images/stop", methods=["POST"])
def stop_image_enrichment():
    """Stop running enrichment."""
    from manifold.services.image_enricher import ImageEnricherService
    ImageEnricherService.stop()
    return jsonify({"ok": True, "message": "Stop signal sent"})


# ── File Browser ─────────────────────────────────────────────────────────
BROWSE_ROOT = os.getenv("BROWSE_ROOT", "/browse")


@api_bp.route("/browse")
def browse_files():
    """Browse mounted filesystem. Returns directories and .m3u/.m3u8 files."""
    path = request.args.get("path", BROWSE_ROOT)
    path = os.path.normpath(path)
    if not path.startswith(os.path.normpath(BROWSE_ROOT)):
        return jsonify({"error": "outside browse root"}), 403
    if not os.path.isdir(path):
        return jsonify({"error": "not a directory"}), 400

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
        return jsonify({"error": "permission denied"}), 403

    parent = os.path.dirname(path) if path != "/" else None
    return jsonify({"path": path, "parent": parent, "entries": entries})


# ── System Stats ─────────────────────────────────────────────────────────
@api_bp.route("/system/stats")
def system_stats():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")

    return jsonify({
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
    })
