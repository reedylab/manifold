"""EPG (XMLTV) ingestion service — fetches XMLTV data and matches to M3U channels via tvg-id."""

import uuid
import logging
from datetime import datetime

import requests
from lxml import etree

from manifold.database import get_session
from manifold.models.epg import Epg
from manifold.models.epg_source import EpgSource
from manifold.models.m3u_source import M3uSource
from manifold.models.manifest import Manifest

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class EpgIngestService:

    @staticmethod
    def get_sources():
        with get_session() as session:
            rows = (
                session.query(EpgSource, M3uSource.name)
                .outerjoin(M3uSource, EpgSource.m3u_source_id == M3uSource.id)
                .order_by(EpgSource.name)
                .all()
            )
            return [
                {
                    "id": r.EpgSource.id,
                    "name": r.EpgSource.name,
                    "url": r.EpgSource.url,
                    "m3u_source_id": r.EpgSource.m3u_source_id,
                    "m3u_source_name": r[1] or "Unknown",
                    "channel_count": r.EpgSource.channel_count or 0,
                    "last_ingested_at": r.EpgSource.last_ingested_at.isoformat() if r.EpgSource.last_ingested_at else None,
                    "created_at": r.EpgSource.created_at.isoformat() if r.EpgSource.created_at else None,
                }
                for r in rows
            ]

    @staticmethod
    def add_source(name: str, url: str, m3u_source_id: str) -> dict:
        with get_session() as session:
            # Verify M3U source exists
            m3u = session.query(M3uSource).filter_by(id=m3u_source_id).first()
            if not m3u:
                return {"error": "M3U source not found"}
            source = EpgSource(
                id=str(uuid.uuid4()),
                name=name,
                url=url,
                m3u_source_id=m3u_source_id,
                created_at=datetime.utcnow(),
            )
            session.add(source)
            session.flush()
            return {"ok": True, "id": source.id}

    @staticmethod
    def delete_source(source_id: str) -> bool:
        with get_session() as session:
            source = session.query(EpgSource).filter_by(id=source_id).first()
            if not source:
                return False
            # Also delete EPG entries for this source
            session.query(Epg).filter_by(epg_source_id=source_id).delete()
            session.delete(source)
        return True

    @staticmethod
    def ingest_all() -> dict:
        with get_session() as session:
            sources = session.query(EpgSource).all()
            source_list = [(s.id,) for s in sources]

        if not source_list:
            return {"ok": True, "ingested": 0, "channels": 0}

        total_channels = 0
        for (source_id,) in source_list:
            result = EpgIngestService.ingest_source(source_id)
            total_channels += result.get("channels", 0)

        return {"ok": True, "ingested": len(source_list), "channels": total_channels}

    @staticmethod
    def ingest_source(source_id: str) -> dict:
        """Fetch XMLTV data, parse channels and programmes, match to M3U channels by tvg-id."""
        with get_session() as session:
            source = session.query(EpgSource).filter_by(id=source_id).first()
            if not source:
                return {"error": "source not found"}
            source_name = source.name
            source_url = source.url
            m3u_source_id = source.m3u_source_id

        logger.info("Ingesting EPG source: %s (%s)", source_name, source_url)

        # Fetch the XMLTV data
        try:
            r = requests.get(source_url, headers={"User-Agent": USER_AGENT}, timeout=60)
            r.raise_for_status()
        except Exception as e:
            logger.error("Failed to fetch EPG %s: %s", source_url, e)
            return {"error": str(e), "channels": 0}

        # Parse XMLTV
        try:
            root = etree.fromstring(r.content)
        except Exception as e:
            logger.error("Failed to parse XMLTV from %s: %s", source_url, e)
            return {"error": f"XML parse error: {e}", "channels": 0}

        # Build lookup: tvg-ids that exist in manifests for this M3U source
        with get_session() as session:
            manifest_rows = (
                session.query(Manifest.tvg_id, Manifest.title)
                .filter(
                    Manifest.m3u_source_id == m3u_source_id,
                    Manifest.tvg_id.isnot(None),
                    Manifest.tvg_id != "",
                )
                .all()
            )
            # Map channel_id -> manifest title for matching
            tvg_to_title = {row.tvg_id: row.title for row in manifest_rows}

        logger.info("Found %d channels with tvg-id in M3U source", len(tvg_to_title))

        # Parse XMLTV channels (with icon URLs)
        xmltv_channels = {}  # channel_id -> (display_name, icon_url)
        for chan_el in root.findall(".//channel"):
            channel_id = chan_el.get("id", "").strip()
            if not channel_id:
                continue
            display_name_el = chan_el.find("display-name")
            display_name = display_name_el.text.strip() if display_name_el is not None and display_name_el.text else channel_id
            icon_el = chan_el.find("icon")
            icon_url = icon_el.get("src", "").strip() if icon_el is not None else None
            xmltv_channels[channel_id] = (display_name, icon_url)

        # Parse programmes grouped by channel
        programmes_by_channel = {}
        for prog_el in root.findall(".//programme"):
            channel_id = prog_el.get("channel", "").strip()
            if not channel_id:
                continue
            prog_xml = etree.tostring(prog_el, encoding="unicode", pretty_print=True)
            if channel_id not in programmes_by_channel:
                programmes_by_channel[channel_id] = []
            programmes_by_channel[channel_id].append(prog_xml)

        # Upsert EPG entries — only for channels that have a matching tvg-id in our manifests
        channel_count = 0
        with get_session() as session:
            for channel_id, (display_name, icon_url) in xmltv_channels.items():
                # Check if this channel_id matches a tvg-id in our M3U source
                if channel_id not in tvg_to_title:
                    continue

                # Use the manifest title as channel_name for matching
                channel_name = tvg_to_title[channel_id]

                # Combine programme fragments
                progs = programmes_by_channel.get(channel_id, [])
                epg_data = "\n".join(progs) if progs else None

                # Upsert
                existing = session.query(Epg).filter_by(
                    channel_id=channel_id, epg_source_id=source_id
                ).first()

                if existing:
                    existing.channel_name = channel_name
                    existing.icon_url = icon_url
                    existing.epg_data = epg_data
                    existing.last_updated = datetime.utcnow()
                else:
                    session.add(Epg(
                        id=str(uuid.uuid4()),
                        channel_id=channel_id,
                        channel_name=channel_name,
                        epg_source_id=source_id,
                        icon_url=icon_url,
                        epg_data=epg_data,
                        last_updated=datetime.utcnow(),
                    ))

                channel_count += 1

        # Update source metadata
        with get_session() as session:
            source = session.query(EpgSource).filter_by(id=source_id).first()
            if source:
                source.channel_count = channel_count
                source.last_ingested_at = datetime.utcnow()

        logger.info("Ingested %d matched EPG channels from %s (of %d total in XMLTV)",
                     channel_count, source_name, len(xmltv_channels))
        return {"ok": True, "channels": channel_count, "total_xmltv": len(xmltv_channels)}
