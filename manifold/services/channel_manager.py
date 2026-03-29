"""Channel management — activate/deactivate, list, EPG matching."""

import logging

from sqlalchemy import or_

from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.epg import Epg
from manifold.models.m3u_source import M3uSource

logger = logging.getLogger(__name__)


class ChannelManagerService:

    @staticmethod
    def get_all_channels():
        """List all live/event manifests with EPG mapping status."""
        with get_session() as session:
            rows = (
                session.query(
                    Manifest.id,
                    Manifest.title,
                    Manifest.tvg_id,
                    Manifest.tags,
                    Manifest.active,
                    Manifest.event_end_at,
                    Manifest.created_at,
                    Manifest.updated_at,
                    Manifest.logo_cached,
                    Manifest.channel_number,
                    Manifest.title_override,
                    M3uSource.stream_mode,
                    Epg.channel_id,
                    Epg.channel_name,
                )
                .outerjoin(M3uSource, Manifest.m3u_source_id == M3uSource.id)
                .outerjoin(
                    Epg,
                    or_(
                        (Manifest.tvg_id == Epg.channel_id) & (Manifest.tvg_id.isnot(None)) & (Manifest.tvg_id != ""),
                        Manifest.title == Epg.channel_name,
                    )
                )
                .filter(
                    Manifest.tags.op("@>")('["live"]')
                    | Manifest.tags.op("@>")('["event"]')
                )
                .order_by(Manifest.channel_number.asc().nullslast(), Manifest.title)
                .all()
            )

        channels = []
        seen = set()
        for row in rows:
            mid = row[0]
            if mid in seen:
                continue
            seen.add(mid)
            channels.append({
                "id": mid,
                "title": row[1],
                "tvg_id": row[2],
                "tags": row[3] or [],
                "active": row[4],
                "event_end_at": row[5].isoformat() if row[5] else None,
                "created_at": row[6].isoformat() if row[6] else None,
                "updated_at": row[7].isoformat() if row[7] else None,
                "logo_cached": bool(row[8]),
                "channel_number": row[9],
                "title_override": row[10],
                "stream_mode": row[11] or "passthrough",
                "epg_channel_id": row[12],
                "epg_channel_name": row[13],
                "epg_mapped": row[12] is not None,
            })
        return channels

    @staticmethod
    def toggle_channel(manifest_id, active):
        with get_session() as session:
            m = session.query(Manifest).filter_by(id=manifest_id).first()
            if not m:
                return False
            m.active = active
        logger.info("Channel %s set active=%s", manifest_id, active)
        return True

    @staticmethod
    def update_channel(manifest_id, data):
        with get_session() as session:
            m = session.query(Manifest).filter_by(id=manifest_id).first()
            if not m:
                return False
            if "title" in data:
                m.title = data["title"]
            if "tags" in data:
                m.tags = data["tags"]
            if "active" in data:
                m.active = data["active"]
            if "channel_number" in data:
                val = data["channel_number"]
                m.channel_number = int(val) if val is not None else None
            if "title_override" in data:
                val = data["title_override"]
                m.title_override = val if val else None
        return True

    @staticmethod
    def delete_channel(manifest_id):
        with get_session() as session:
            m = session.query(Manifest).filter_by(id=manifest_id).first()
            if not m:
                return False
            session.delete(m)
        logger.info("Deleted manifest %s", manifest_id)
        return True
