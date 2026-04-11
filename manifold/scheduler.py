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
    from manifold.services.manifest_resolver import ManifestResolverService
    from manifold.config import get_setting
    from manifold.database import get_session
    from manifold.models.manifest import Manifest
    from datetime import datetime, timezone, timedelta

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

    def resolved_refresh_job():
        """Refresh resolved manifests that are actively being watched and near expiry.

        Demand-driven: only touches manifests with recent client access. Dormant
        channels are left alone — no upstream traffic, no bot-wall risk.
        The 403 safety net in the stream router handles wake-from-dormant cases.
        """
        try:
            now = datetime.now(timezone.utc)
            soon = now + timedelta(minutes=5)
            cooldown = now - timedelta(minutes=3)
            watching_window = now - timedelta(minutes=10)

            with get_session() as session:
                rows = (
                    session.query(Manifest.id)
                    .filter(Manifest.tags.contains(["resolved"]))
                    .filter(Manifest.active == True)
                    .filter(Manifest.last_accessed_at.isnot(None))
                    .filter(Manifest.last_accessed_at > watching_window)
                    .filter(
                        (Manifest.expires_at.is_(None)) |
                        (Manifest.expires_at < soon)
                    )
                    .filter(
                        (Manifest.last_refreshed_at.is_(None)) |
                        (Manifest.last_refreshed_at < cooldown)
                    )
                    .limit(5)
                    .all()
                )
                ids = [r[0] for r in rows]
            if not ids:
                return
            logger.info("Demand refresh: %d manifests due", len(ids))
            succeeded = failed = 0
            for mid in ids:
                result = ManifestResolverService.refresh_manifest(mid)
                if result.get("ok"):
                    succeeded += 1
                else:
                    failed += 1
                    logger.warning("Refresh failed for %s: %s", mid, result.get("error"))
            logger.info("Demand refresh complete: %d succeeded, %d failed", succeeded, failed)
        except Exception as e:
            logger.exception("Demand refresh job error: %s", e)

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

    # Demand-driven refresh: only touches manifests with recent client access.
    # If you're not watching, no upstream traffic, no bot-wall risk.
    _scheduler.add_job(resolved_refresh_job, "interval", seconds=60,
                       id="resolved_refresh", name="Resolved Manifest Refresh (demand-driven)",
                       replace_existing=True)

    _scheduler.start()
    logger.info("Scheduler started: regen=%dm, cleanup=%dh, logo_sync=30m, stream_cleanup=60s, "
                "image_enrichment=%dh, m3u_refresh=%dh, epg_refresh=%dh",
                regen_minutes, cleanup_hours, image_enrichment_hours, m3u_refresh_hours, epg_refresh_hours)

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
