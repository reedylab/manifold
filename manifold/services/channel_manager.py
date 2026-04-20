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
                    Manifest.primary_tag,
                    Manifest.activation_mode,
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
                "primary_tag": row[14],
                "activation_mode": row[15],
            })
        return channels

    @staticmethod
    def toggle_channel(manifest_id, active):
        """Flip active state. Records user intent via activation_mode so the
        additive-activation pass doesn't overwrite it on next ingest."""
        with get_session() as session:
            m = session.query(Manifest).filter_by(id=manifest_id).first()
            if not m:
                return False
            m.active = active
            m.activation_mode = "force_on" if active else "force_off"
        logger.info("Channel %s set active=%s (mode=force_%s)",
                    manifest_id, active, "on" if active else "off")
        return True

    @staticmethod
    def reset_activation(manifest_id):
        """Return a channel to auto mode so ingest rules decide its active state."""
        with get_session() as session:
            m = session.query(Manifest).filter_by(id=manifest_id).first()
            if not m:
                return False
            m.activation_mode = "auto"
        logger.info("Channel %s reset to activation_mode=auto", manifest_id)
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
                # Sync activation_mode so the change survives next ingest.
                m.activation_mode = "force_on" if data["active"] else "force_off"
            if "activation_mode" in data and data["activation_mode"] in ("auto", "force_on", "force_off"):
                new_mode = data["activation_mode"]
                m.activation_mode = new_mode
                # Sync active when the caller only provided a mode (no explicit active).
                # force_on/force_off imply the active state; auto leaves active alone
                # so the next ingest's additive pass can decide.
                if "active" not in data:
                    if new_mode == "force_on":
                        m.active = True
                    elif new_mode == "force_off":
                        m.active = False
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
