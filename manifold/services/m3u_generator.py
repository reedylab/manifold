"""Generate M3U playlist from database."""

import logging
import os
import tempfile

from manifold.config import Config, get_setting
from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.m3u_source import M3uSource
from manifold.models.epg import Epg

logger = logging.getLogger(__name__)

# Tag → group-title mapping
GROUP_MAP = {
    "event": "Live Events",
    "sports": "Sports",
    "news": "News",
    "movies": "Movies",
    "kids": "Kids",
    "live": "Live TV",
}


class M3UGeneratorService:

    @staticmethod
    def generate():
        cfg = Config()
        manifold_host = cfg.MANIFOLD_HOST
        manifold_port = cfg.MANIFOLD_PORT
        output_path = os.path.join(cfg.OUTPUT_DIR, "manifold.m3u")
        os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)

        with get_session() as session:
            rows = (
                session.query(
                    Manifest.id,
                    Manifest.url,
                    Manifest.title,
                    Manifest.tvg_id,
                    Manifest.tags,
                    Manifest.logo_cached,
                    Manifest.channel_number,
                    Manifest.title_override,
                    M3uSource.stream_mode,
                )
                .outerjoin(M3uSource, Manifest.m3u_source_id == M3uSource.id)
                .filter(Manifest.active == True)
                .filter(
                    Manifest.tags.op("@>")('["live"]')
                    | Manifest.tags.op("@>")('["event"]')
                )
                .order_by(Manifest.channel_number.asc().nullslast(), Manifest.title)
                .all()
            )

        if not rows:
            logger.warning("No active live/event manifests found for M3U generation")
            _atomic_write(output_path, "#EXTM3U\n")
            return 0

        seen = set()
        lines = ["#EXTM3U"]
        for row in rows:
            manifest_id, url, title, tvg_id, tags, logo_cached, channel_number, title_override, stream_mode = row
            if manifest_id in seen:
                continue
            seen.add(manifest_id)
            display_title = title_override or title or f"Manifest {manifest_id}"

            # Passthrough: emit the original source URL directly so downstream
            # apps (e.g. channelarr) hit the source stream without manifold
            # proxying or encoding anything. Only ffmpeg mode needs to route
            # through manifold's stream router for HLS segmentation + filler.
            if (stream_mode or "passthrough") == "passthrough":
                stream_url = url
            else:
                stream_url = f"http://{manifold_host}:{manifold_port}/stream/{manifest_id}.m3u8"

            # tvg-chno from channel_number
            tvg_chno = f' tvg-chno="{channel_number}"' if channel_number is not None else ""

            # tvg-id from manifest (originally from M3U EXTINF)
            tvg_id_str = f' tvg-id="{tvg_id}"' if tvg_id else ""

            # tvg-logo
            if logo_cached:
                tvg_logo = f' tvg-logo="http://{manifold_host}:{manifold_port}/logo/{manifest_id}"'
            else:
                tvg_logo = ""

            # group-title from tags
            group_title = _resolve_group(tags or [])

            lines.append(
                f'#EXTINF:-1{tvg_chno}{tvg_id_str}{tvg_logo} tvg-name="{display_title}" '
                f'group-title="{group_title}",{display_title}'
            )
            lines.append(stream_url)

        content = "\n".join(lines) + "\n"
        _atomic_write(output_path, content)
        count = (len(lines) - 1) // 2
        logger.info("Generated M3U: %d channels → %s", count, output_path)
        return count


def _resolve_group(tags: list) -> str:
    for tag in tags:
        if tag in GROUP_MAP:
            return GROUP_MAP[tag]
    return "Uncategorized"


def _atomic_write(path: str, content: str):
    """Write to temp file then rename for atomic update.

    tempfile.mkstemp creates 0o600 files; chmod to 0o644 so downstream readers
    running as a different user (e.g. Jellyfin reading from a shared mount)
    can actually open them.
    """
    dir_name = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
