"""M3U playlist ingestion service — parses M3U playlists and creates manifests."""

import re
import uuid
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

from manifold.database import get_session
from manifold.models.manifest import Manifest, Capture, HeaderProfile
from manifold.models.m3u_source import M3uSource

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _extract_tvg_id(extinf_line: str) -> str | None:
    """Extract tvg-id from an EXTINF line."""
    m = re.search(r'tvg-id=["\']([^"\']*)["\']', extinf_line, re.I)
    return m.group(1).strip() if m else None


def _extract_tvg_logo(extinf_line: str) -> str | None:
    """Extract tvg-logo URL from an EXTINF line."""
    m = re.search(r'tvg-logo=["\']([^"\']*)["\']', extinf_line, re.I)
    return m.group(1).strip() if m else None


def _extract_clean_title(extinf_line: str, fallback: str = "Unknown") -> str:
    if not extinf_line.startswith("#EXTINF:"):
        return fallback
    parts = extinf_line.split(",", 1)
    if len(parts) < 2:
        return fallback
    raw = parts[1].strip()
    clean = re.sub(r'(tvg-name|group-title)=["\'][^"\']*["\']\s*,?\s*', '', raw, flags=re.I)
    clean = re.sub(r'^["\']|["\']$', '', clean)
    clean = re.sub(r'\s+', ' ', clean).strip()
    return clean or fallback


def _compute_tags(extinf_line: str, title: str, channel_url: str) -> list[str]:
    tags = set()
    title_lower = title.lower()

    tags.add('live')

    if any(k in title_lower for k in ['espn', 'sec network', 'nba', 'nfl', 'mlb', 'nhl', 'fanduel', 'sportsnet']):
        tags.add('sports')
    if any(k in title_lower for k in ['cnn', 'msnbc', 'fox news', 'bbc', 'newsmax', 'c-span']):
        tags.add('news')
    if any(k in title_lower for k in ['hbo', 'showtime', 'cinemax', 'tmc', 'movie']):
        tags.add('movies')
    if any(k in title_lower for k in ['disney', 'nick', 'cartoon', 'boomerang', 'universal kids']):
        tags.add('kids')

    domain = urlparse(channel_url).netloc.lower()
    if 'espn' in domain:
        tags.add('sports')
    if 'cnn' in domain:
        tags.add('news')

    if extinf_line:
        group_match = re.search(r'group-title=["\']([^"\']*)["\']', extinf_line, re.I)
        if group_match:
            group = group_match.group(1).strip().lower()
            if group and group not in {'live', 'uncategorized'}:
                tags.add(group)

    event_pattern = re.search(r'(\d{1,2}[\/\-]\d{1,2}.*?\d{1,2}:\d{2}\s*[AP]M)', title)
    has_teams = bool(re.search(r'\bvs\b|@|vs\.|at\s+|\s+@\s+', title, re.I))
    if event_pattern and has_teams:
        tags.add('event')
        if any(k in title_lower for k in ['ncaa', 'college', 'ncaaf', 'cfb']):
            tags.add('ncaaf')
        elif 'nba' in title_lower:
            tags.add('nba')
        elif 'nhl' in title_lower:
            tags.add('nhl')
        elif 'mlb' in title_lower:
            tags.add('mlb')
        elif any(k in title_lower for k in ['nfl', 'redzone']):
            tags.add('nfl')
        elif 'ufc' in title_lower:
            tags.add('ufc')
            tags.add('ppv')

    if any(k in title_lower for k in ['replay', 'highlight', 'condensed', 'full game', 'on demand', 'vod']):
        tags.discard('live')
        tags.add('vod')

    if not tags:
        tags.add('uncategorized')
    return sorted(list(tags))


class M3uIngestService:

    @staticmethod
    def get_sources():
        with get_session() as session:
            rows = session.query(M3uSource).order_by(M3uSource.name).all()
            return [
                {
                    "id": r.id,
                    "name": r.name,
                    "url": r.url,
                    "stream_mode": r.stream_mode or "passthrough",
                    "auto_activate": bool(r.auto_activate),
                    "channel_count": r.channel_count or 0,
                    "last_ingested_at": r.last_ingested_at.isoformat() if r.last_ingested_at else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]

    @staticmethod
    def add_source(name: str, url: str) -> dict:
        with get_session() as session:
            existing = session.query(M3uSource).filter_by(url=url).first()
            if existing:
                return {"error": "URL already exists", "id": existing.id}
            source = M3uSource(
                id=str(uuid.uuid4()),
                name=name,
                url=url,
                created_at=datetime.utcnow(),
            )
            session.add(source)
            session.flush()
            return {"ok": True, "id": source.id}

    @staticmethod
    def delete_source(source_id: str) -> bool:
        with get_session() as session:
            source = session.query(M3uSource).filter_by(id=source_id).first()
            if not source:
                return False
            session.delete(source)
        return True

    @staticmethod
    def ingest_all() -> dict:
        with get_session() as session:
            sources = session.query(M3uSource).all()
            source_list = [(s.id, s.name, s.url) for s in sources]

        if not source_list:
            return {"ok": True, "ingested": 0, "channels": 0}

        total_channels = 0
        for source_id, source_name, source_url in source_list:
            result = M3uIngestService.ingest_source(source_id)
            total_channels += result.get("channels", 0)

        return {"ok": True, "ingested": len(source_list), "channels": total_channels}

    @staticmethod
    def refresh_all() -> dict:
        """Re-ingest all M3U sources and clean up stale/disappeared channels.

        - Events that disappear: deleted immediately
        - Live channels that disappear: marked stale (deactivated)
        - Live channels stale for >12h: deleted
        - Channels that reappear: stale status cleared automatically by ingest_source()
        """
        STALE_GRACE_HOURS = 12

        with get_session() as session:
            sources = session.query(M3uSource).all()
            source_list = [(s.id, s.name) for s in sources]

        if not source_list:
            return {"ok": True, "refreshed": 0, "stale": 0, "deleted": 0}

        total_stale = 0
        total_deleted = 0

        for source_id, source_name in source_list:
            # Ingest returns seen_ids — the set of manifest IDs present in the playlist
            result = M3uIngestService.ingest_source(source_id)
            seen_ids = result.get("seen_ids", set())

            if not seen_ids:
                # If ingest failed or returned no channels, don't clean up
                # (could be a fetch error — don't wipe everything)
                logger.warning("M3U refresh: no channels seen for %s, skipping cleanup", source_name)
                continue

            # Find manifests belonging to this source that were NOT seen
            with get_session() as session:
                all_source_manifests = (
                    session.query(Manifest)
                    .filter(Manifest.m3u_source_id == source_id)
                    .all()
                )

                now = datetime.now(timezone.utc)

                for m in all_source_manifests:
                    if m.id in seen_ids:
                        continue  # Still in playlist — already handled by ingest_source

                    is_event = "event" in (m.tags or [])

                    if is_event:
                        # Events are ephemeral — delete immediately when gone
                        logger.info("Deleting disappeared event: %s", m.title)
                        session.delete(m)
                        total_deleted += 1
                    elif m.stale_since is None:
                        # First time missing — mark stale and deactivate
                        m.stale_since = now
                        m.active = False
                        logger.info("Channel went stale (deactivated): %s", m.title)
                        total_stale += 1
                    else:
                        # Already stale — check grace period
                        stale_hours = (now - m.stale_since).total_seconds() / 3600
                        if stale_hours >= STALE_GRACE_HOURS:
                            logger.info("Deleting stale channel (gone %.1fh): %s", stale_hours, m.title)
                            session.delete(m)
                            total_deleted += 1
                        else:
                            total_stale += 1

        logger.info("M3U refresh complete: %d sources, %d stale, %d deleted",
                     len(source_list), total_stale, total_deleted)
        return {
            "ok": True,
            "refreshed": len(source_list),
            "stale": total_stale,
            "deleted": total_deleted,
        }

    @staticmethod
    def ingest_source(source_id: str) -> dict:
        """Ingest a single M3U source — parses playlist entries without
        fetching individual stream URLs (the bridge handles streaming)."""
        with get_session() as session:
            source = session.query(M3uSource).filter_by(id=source_id).first()
            if not source:
                return {"error": "source not found"}
            source_name = source.name
            source_url = source.url
            auto_activate = bool(source.auto_activate)

        logger.info("Ingesting M3U source: %s (%s, auto_activate=%s)",
                     source_name, source_url, auto_activate)

        # Fetch the playlist — local file or remote URL
        try:
            if source_url.startswith("/") or source_url.startswith("file://"):
                file_path = source_url.replace("file://", "")
                with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
            else:
                r = requests.get(source_url, headers={"User-Agent": USER_AGENT}, timeout=30)
                r.raise_for_status()
                text = r.text
        except Exception as e:
            logger.error("Failed to fetch playlist %s: %s", source_url, e)
            return {"error": str(e), "channels": 0}

        lines = text.splitlines()

        # Get or create capture
        with get_session() as session:
            capture = session.query(Capture).filter_by(page_url=source_url).first()
            if not capture:
                capture = Capture(
                    id=str(uuid.uuid4()),
                    page_url=source_url,
                    user_agent=USER_AGENT,
                    context={"type": "m3u_playlist", "source_name": source_name},
                )
                session.add(capture)
                session.flush()
            capture_id = capture.id

        # Parse all EXTINF entries from the playlist
        entries = []
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            if ln.startswith("#EXTINF:"):
                channel_title = _extract_clean_title(ln, source_name)
                i += 1
                if i >= len(lines):
                    break
                channel_url = lines[i].strip()
                if not channel_url or channel_url.startswith("#"):
                    i += 1
                    continue
                tvg_id = _extract_tvg_id(ln)
                tvg_logo = _extract_tvg_logo(ln)
                entries.append((channel_title, channel_url, ln, tvg_id, tvg_logo))
            i += 1

        if not entries:
            logger.warning("No EXTINF entries found in %s", source_url)
            with get_session() as session:
                source = session.query(M3uSource).filter_by(id=source_id).first()
                if source:
                    source.channel_count = 0
                    source.last_ingested_at = datetime.utcnow()
            return {"ok": True, "channels": 0}

        # Bulk upsert all entries in a single session
        channel_count = 0
        seen_ids = set()
        with get_session() as session:
            # Build lookup caches
            all_manifests = session.query(Manifest).all()
            title_map = {}
            url_hash_map = {}
            for m in all_manifests:
                if m.title:
                    title_map[m.title] = m
                if m.url_hash:
                    url_hash_map[m.url_hash] = m

            # Load header profile cache
            profile_rows = session.query(HeaderProfile.name, HeaderProfile.id).all()
            domain_profile_cache = {row.name: row.id for row in profile_rows}

            for channel_title, channel_url, extinf_line, tvg_id, tvg_logo in entries:
                url_hash = _md5(channel_url)
                source_domain = urlparse(channel_url).netloc
                computed_tags = _compute_tags(extinf_line, channel_title, channel_url)
                header_profile_id = domain_profile_cache.get(source_domain)

                # Find existing manifest by title or URL hash
                manifest = title_map.get(channel_title) or url_hash_map.get(url_hash)

                if manifest:
                    # Update if URL changed
                    if manifest.url != channel_url:
                        manifest.url = channel_url
                        manifest.url_hash = url_hash
                        manifest.source_domain = source_domain
                        manifest.updated_at = datetime.utcnow()
                    # Update title if it changed (URL hash match)
                    if manifest.title != channel_title:
                        old_title = manifest.title
                        manifest.title = channel_title
                        if old_title in title_map:
                            del title_map[old_title]
                        title_map[channel_title] = manifest
                    manifest.tags = computed_tags
                    manifest.tvg_id = tvg_id
                    manifest.tvg_logo = tvg_logo
                    manifest.m3u_source_id = source_id
                    manifest.header_profile_id = header_profile_id
                    manifest.mime = "application/vnd.apple.mpegurl"
                    manifest.kind = "master"
                    # Auto-activate: bring channel back live if the source has
                    # the toggle on. We apply this to existing channels too,
                    # not just new ones — otherwise flipping the toggle on
                    # only affects never-seen channels, which means pre-existing
                    # inactive rows stay inactive forever. If auto_activate is
                    # off, leave active alone so manual changes stick.
                    if auto_activate:
                        manifest.active = True
                    # Channel is present in source — clear stale status
                    manifest.stale_since = None
                else:
                    # Use URL hash as sha256 placeholder for uniqueness
                    sha256_placeholder = hashlib.sha256(channel_url.encode("utf-8")).hexdigest()

                    manifest = Manifest(
                        id=str(uuid.uuid4()),
                        capture_id=capture_id,
                        m3u_source_id=source_id,
                        header_profile_id=header_profile_id,
                        url=channel_url,
                        url_hash=url_hash,
                        source_domain=source_domain,
                        mime="application/vnd.apple.mpegurl",
                        kind="master",
                        headers={},
                        requires_headers=False,
                        sha256=sha256_placeholder,
                        title=channel_title,
                        tvg_id=tvg_id,
                        tvg_logo=tvg_logo,
                        tags=computed_tags,
                        active=auto_activate,
                    )
                    session.add(manifest)
                    title_map[channel_title] = manifest
                    url_hash_map[url_hash] = manifest

                seen_ids.add(manifest.id)
                channel_count += 1

        # Update source metadata
        with get_session() as session:
            source = session.query(M3uSource).filter_by(id=source_id).first()
            if source:
                source.channel_count = channel_count
                source.last_ingested_at = datetime.utcnow()

        logger.info("Ingested %d channels from %s", channel_count, source_name)

        # Auto-sync logos for newly ingested channels
        import threading
        def _sync():
            try:
                from manifold.services.logo_manager import LogoManagerService
                LogoManagerService.sync_logos()
            except Exception as e:
                logger.error("Post-ingest logo sync failed: %s", e)
        threading.Thread(target=_sync, daemon=True).start()

        return {"ok": True, "channels": channel_count, "seen_ids": seen_ids}
