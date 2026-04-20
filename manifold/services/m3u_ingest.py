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
from manifold.services.tag_rules import (
    apply_keyword_rules,
    compute_primary_tag,
    get_tag_rules,
)
from manifold.services.autonumber import AutoNumberer, get_number_ranges
from manifold.services.activation import get_activation_rules, should_be_active

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


def _compute_tags(
    extinf_line: str,
    title: str,
    channel_url: str,
    rules: dict,
) -> tuple[list[str], str | None]:
    """Return (sorted tags, primary_tag) for a channel.

    Keyword matching is driven by `rules` (see manifold.services.tag_rules).
    Event detection, VOD transformation, and group-title passthrough stay in
    code because they're structural rather than keyword-based.
    """
    title_lower = title.lower()
    domain_lower = urlparse(channel_url).netloc.lower()

    tags: set[str] = {'live'}
    tags.update(apply_keyword_rules(rules, title_lower, domain_lower))

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

    tag_list = sorted(tags)
    priority = rules.get('priority', []) or []
    primary = compute_primary_tag(tag_list, priority)
    return tag_list, primary


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
            return {"ok": True, "ingested": 0, "channels": 0, "warnings": []}

        total_channels = 0
        # Collapse per-tag warnings across sources so the UI gets one entry per
        # tag with a total unassigned count.
        merged: dict[str, dict] = {}
        for source_id, source_name, source_url in source_list:
            result = M3uIngestService.ingest_source(source_id)
            total_channels += result.get("channels", 0)
            for w in (result.get("warnings") or []):
                key = (w.get("type"), w.get("tag"))
                if key in merged:
                    merged[key]["unassigned"] += w.get("unassigned", 0)
                else:
                    merged[key] = dict(w)

        return {
            "ok": True,
            "ingested": len(source_list),
            "channels": total_channels,
            "warnings": list(merged.values()),
        }

    @staticmethod
    def _cleanup_disappeared(source_id: str, seen_ids: set) -> tuple[int, int]:
        """Handle manifests that were in this source but aren't anymore.

        - activation_mode in (force_on, force_off): never auto-deleted (user intent preserved)
        - Events: deleted immediately (ephemeral)
        - Auto + inactive: deleted immediately (never exposed, no grace needed)
        - Auto + active, first miss: marked stale + deactivated
        - Auto + active, stale >12h: deleted

        Returns (stale_count, deleted_count). Safe to call with empty seen_ids —
        skips cleanup so a transient fetch failure can't wipe an entire source.
        """
        STALE_GRACE_HOURS = 12
        if not seen_ids:
            return (0, 0)

        stale = 0
        deleted = 0
        with get_session() as session:
            all_source_manifests = (
                session.query(Manifest)
                .filter(Manifest.m3u_source_id == source_id)
                .all()
            )
            now = datetime.now(timezone.utc)
            for m in all_source_manifests:
                if m.id in seen_ids:
                    continue

                mode = m.activation_mode or "auto"
                if mode in ("force_on", "force_off"):
                    continue

                is_event = "event" in (m.tags or [])
                if is_event:
                    logger.info("Deleting disappeared event: %s", m.title)
                    session.delete(m)
                    deleted += 1
                    continue

                if not m.active:
                    logger.info("Deleting disappeared inactive channel: %s", m.title)
                    session.delete(m)
                    deleted += 1
                    continue

                if m.stale_since is None:
                    m.stale_since = now
                    m.active = False
                    logger.info("Channel went stale (deactivated): %s", m.title)
                    stale += 1
                else:
                    stale_hours = (now - m.stale_since).total_seconds() / 3600
                    if stale_hours >= STALE_GRACE_HOURS:
                        logger.info("Deleting stale channel (gone %.1fh): %s",
                                    stale_hours, m.title)
                        session.delete(m)
                        deleted += 1
                    else:
                        stale += 1
        return (stale, deleted)

    @staticmethod
    def refresh_all() -> dict:
        """Re-ingest all M3U sources and clean up stale/disappeared channels.

        - Events that disappear: deleted immediately
        - Live channels that disappear: marked stale (deactivated)
        - Live channels stale for >12h: deleted
        - Channels that reappear: stale status cleared automatically by ingest_source()
        """
        with get_session() as session:
            sources = session.query(M3uSource).all()
            source_list = [(s.id, s.name) for s in sources]

        if not source_list:
            return {"ok": True, "refreshed": 0, "stale": 0, "deleted": 0}

        total_stale = 0
        total_deleted = 0

        for source_id, source_name in source_list:
            result = M3uIngestService.ingest_source(source_id)
            seen_ids = result.get("seen_ids", set())
            if not seen_ids:
                logger.warning("M3U refresh: no channels seen for %s, skipping cleanup", source_name)
                continue
            # Cleanup now lives inside ingest_source, but double-calling is a
            # no-op because anything disappeared was already marked stale on
            # the ingest pass. The counts returned here aren't used by the
            # scheduler (the job logs its own summary) — leave them as 0 to
            # avoid double-counting.

        logger.info("M3U refresh complete: %d sources", len(source_list))
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

        # Fetch tag rules + number ranges + activation rules once — passed into
        # the loop to avoid N+1 DB hits.
        tag_rules = get_tag_rules()
        number_ranges = get_number_ranges()
        activation_rules = get_activation_rules()
        tags_auto_on = set(activation_rules.get("tags_auto_on") or [])

        # Bulk upsert all entries in a single session
        channel_count = 0
        seen_ids = set()
        with get_session() as session:
            # Build lookup caches
            all_manifests = session.query(Manifest).all()
            title_map = {}
            url_hash_map = {}
            taken_numbers: set[int] = set()
            for m in all_manifests:
                if m.title:
                    title_map[m.title] = m
                if m.url_hash:
                    url_hash_map[m.url_hash] = m
                if m.channel_number is not None:
                    taken_numbers.add(m.channel_number)
            numberer = AutoNumberer(number_ranges, taken_numbers)

            # Load header profile cache
            profile_rows = session.query(HeaderProfile.name, HeaderProfile.id).all()
            domain_profile_cache = {row.name: row.id for row in profile_rows}

            for channel_title, channel_url, extinf_line, tvg_id, tvg_logo in entries:
                url_hash = _md5(channel_url)
                source_domain = urlparse(channel_url).netloc
                computed_tags, primary_tag = _compute_tags(
                    extinf_line, channel_title, channel_url, tag_rules
                )
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
                    manifest.primary_tag = primary_tag
                    manifest.channel_number = numberer.assign(manifest.channel_number, primary_tag)
                    manifest.tvg_id = tvg_id
                    manifest.tvg_logo = tvg_logo
                    manifest.m3u_source_id = source_id
                    manifest.header_profile_id = header_profile_id
                    manifest.mime = "application/vnd.apple.mpegurl"
                    manifest.kind = "master"
                    # Additive activation: auto-mode channels follow tag rules;
                    # force_on/force_off rows preserve user intent across ingests.
                    # Match any tag in the channel's full tag list, not just primary.
                    if manifest.activation_mode == "auto":
                        manifest.active = any(t in tags_auto_on for t in computed_tags)
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
                        primary_tag=primary_tag,
                        channel_number=numberer.assign(None, primary_tag),
                        # auto_activate=True on the source now means "insert new
                        # channels as force_on" (user wants this source always
                        # on, bypassing tag rules). auto_activate=False new rows
                        # enter auto mode and additive rules decide active.
                        activation_mode=("force_on" if auto_activate else "auto"),
                        active=(True if auto_activate
                                else any(t in tags_auto_on for t in computed_tags)),
                    )
                    session.add(manifest)
                    title_map[channel_title] = manifest
                    url_hash_map[url_hash] = manifest

                seen_ids.add(manifest.id)
                channel_count += 1

        # Clean up channels that disappeared from this source. Without this,
        # deletions upstream (e.g. channelarr removing a channel) never
        # propagate to manifold until the 4-hour refresh_all cron runs. The
        # cleanup is safe — it only looks at manifests belonging to this
        # source and skips anything still present (seen_ids).
        stale, deleted = M3uIngestService._cleanup_disappeared(source_id, seen_ids)

        # Update source metadata
        with get_session() as session:
            source = session.query(M3uSource).filter_by(id=source_id).first()
            if source:
                source.channel_count = channel_count
                source.last_ingested_at = datetime.utcnow()

        logger.info("Ingested %d channels from %s (stale=%d, deleted=%d)",
                     channel_count, source_name, stale, deleted)

        # Auto-sync logos for newly ingested channels
        import threading
        def _sync():
            try:
                from manifold.services.logo_manager import LogoManagerService
                LogoManagerService.sync_logos()
            except Exception as e:
                logger.error("Post-ingest logo sync failed: %s", e)
        threading.Thread(target=_sync, daemon=True).start()

        return {
            "ok": True,
            "channels": channel_count,
            "seen_ids": seen_ids,
            "warnings": numberer.warnings(),
        }
