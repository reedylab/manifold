"""APScheduler for periodic M3U/XMLTV regen and cleanup."""

import logging
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

_scheduler = None


def start_scheduler(app):
    global _scheduler

    from manifold.services.m3u_generator import M3UGeneratorService
    from manifold.services.xmltv_generator import XMLTVGeneratorService
    from manifold.services.event_cleanup import EventCleanupService
    from manifold.services.logo_manager import LogoManagerService
    from manifold.services.stream_manager import StreamManagerService
    from manifold.services.image_enricher import ImageEnricherService
    from manifold.services.m3u_ingest import M3uIngestService
    from manifold.services.epg_ingest import EpgIngestService
    from manifold.services.vpn_monitor import sample_latency, maybe_auto_rotate
    from manifold.config import get_setting

    _scheduler = BackgroundScheduler(daemon=True)

    def regen_job():
        try:
            M3UGeneratorService.generate()
            XMLTVGeneratorService.generate()
        except Exception as e:
            logger.error("M3U/XMLTV regen failed: %s", e)

    def cleanup_job():
        try:
            EventCleanupService.update_event_end_times()
            EventCleanupService.cleanup_expired()
        except Exception as e:
            logger.error("Event cleanup failed: %s", e)

    def logo_sync_job():
        try:
            LogoManagerService.sync_logos()
        except Exception as e:
            logger.error("Logo sync failed: %s", e)

    def stream_cleanup_job():
        try:
            StreamManagerService.cleanup_stale()
        except Exception as e:
            logger.error("Stream cleanup failed: %s", e)

    def image_enrichment_job():
        try:
            ImageEnricherService.enrich_all()
        except Exception as e:
            logger.error("Image enrichment failed: %s", e)

    def m3u_refresh_job():
        try:
            M3uIngestService.refresh_all()
            M3UGeneratorService.generate()
            XMLTVGeneratorService.generate()
        except Exception as e:
            logger.error("M3U refresh failed: %s", e)

    def epg_refresh_job():
        try:
            EpgIngestService.ingest_all()
            XMLTVGeneratorService.generate()
        except Exception as e:
            logger.error("EPG refresh failed: %s", e)

    def vpn_sample_job():
        try:
            sample_latency()
        except Exception as e:
            logger.error("VPN sample failed: %s", e)

    def vpn_rotate_job():
        try:
            maybe_auto_rotate()
        except Exception as e:
            logger.error("VPN rotate check failed: %s", e)

    regen_minutes = int(get_setting("scheduler_regen_minutes", "5") or "5")
    cleanup_hours = int(get_setting("scheduler_cleanup_hours", "1") or "1")

    _scheduler.add_job(regen_job, "interval", minutes=regen_minutes,
                       id="m3u_xmltv_regen", name="M3U + XMLTV Regeneration",
                       replace_existing=True)
    _scheduler.add_job(cleanup_job, "interval", hours=cleanup_hours,
                       id="event_cleanup", name="Event Cleanup",
                       replace_existing=True)
    _scheduler.add_job(logo_sync_job, "interval", minutes=30,
                       id="logo_sync", name="Logo Sync",
                       replace_existing=True)
    _scheduler.add_job(stream_cleanup_job, "interval", seconds=60,
                       id="stream_cleanup", name="Stream Cleanup",
                       replace_existing=True)

    image_enrichment_hours = int(get_setting("scheduler_image_enrichment_hours", "6") or "6")
    _scheduler.add_job(image_enrichment_job, "interval", hours=image_enrichment_hours,
                       id="image_enrichment", name="Image Enrichment",
                       replace_existing=True)

    m3u_refresh_hours = int(get_setting("scheduler_m3u_refresh_hours", "4") or "4")
    _scheduler.add_job(m3u_refresh_job, "interval", hours=m3u_refresh_hours,
                       id="m3u_refresh", name="M3U Playlist Refresh",
                       replace_existing=True)

    epg_refresh_hours = int(get_setting("scheduler_epg_refresh_hours", "12") or "12")
    _scheduler.add_job(epg_refresh_job, "interval", hours=epg_refresh_hours,
                       id="epg_refresh", name="EPG Data Refresh",
                       replace_existing=True)

    # VPN: latency sampler runs every 60s; rotate job runs every 60s but
    # internally checks vpn_auto_rotate_minutes (default 0 = disabled)
    _scheduler.add_job(vpn_sample_job, "interval", seconds=60,
                       id="vpn_sample", name="VPN Latency Sampler",
                       replace_existing=True)
    _scheduler.add_job(vpn_rotate_job, "interval", seconds=60,
                       id="vpn_rotate", name="VPN Auto-Rotate Check",
                       replace_existing=True)

    _scheduler.start()
    logger.info("Scheduler started: regen=%dm, cleanup=%dh, logo_sync=30m, stream_cleanup=60s, "
                "image_enrichment=%dh, m3u_refresh=%dh, epg_refresh=%dh, vpn_sample=60s",
                regen_minutes, cleanup_hours, image_enrichment_hours, m3u_refresh_hours, epg_refresh_hours)

    # Take an immediate VPN sample so the chart isn't empty for the first 60s
    try:
        sample_latency()
    except Exception:
        pass

    # Run initial generation on startup
    regen_job()


def get_scheduler():
    return _scheduler


def get_jobs_info():
    """Return scheduler job details for the API."""
    if not _scheduler:
        return []
    jobs = []
    for job in _scheduler.get_jobs():
        trigger = job.trigger
        interval = None
        if hasattr(trigger, "interval"):
            interval = int(trigger.interval.total_seconds())
        jobs.append({
            "id": job.id,
            "name": job.name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "interval_seconds": interval,
        })
    return jobs


def update_job_interval(job_id, seconds):
    """Update a job's interval."""
    if not _scheduler:
        return False
    job = _scheduler.get_job(job_id)
    if not job:
        return False
    _scheduler.reschedule_job(job_id, trigger="interval", seconds=seconds)
    logger.info("Rescheduled job %s to every %d seconds", job_id, seconds)
    return True


def run_job_now(job_id):
    """Trigger a job to run immediately."""
    if not _scheduler:
        return False
    job = _scheduler.get_job(job_id)
    if not job:
        return False
    job.func()
    return True
