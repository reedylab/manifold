"""Logo management — download, cache, and serve channel logos."""

import os
import logging
import tempfile

import requests

from manifold.config import Config
from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.epg import Epg

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class LogoManagerService:

    @staticmethod
    def get_logo_path(manifest_id: str) -> str | None:
        cfg = Config()
        path = os.path.join(cfg.LOGO_DIR, f"{manifest_id}.png")
        return path if os.path.isfile(path) else None

    @staticmethod
    def save_logo(manifest_id: str, data: bytes) -> bool:
        cfg = Config()
        os.makedirs(cfg.LOGO_DIR, exist_ok=True)
        target = os.path.join(cfg.LOGO_DIR, f"{manifest_id}.png")
        try:
            fd, tmp = tempfile.mkstemp(dir=cfg.LOGO_DIR, suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, target)
            return True
        except Exception as e:
            logger.error("Failed to save logo %s: %s", manifest_id, e)
            return False

    @staticmethod
    def sync_logos():
        """Download logos for manifests that don't have a cached logo yet.
        Sources (in priority order):
          1. tvg_logo from M3U EXTINF
          2. icon_url from EPG channel data (matched via tvg_id)
        """
        cfg = Config()
        os.makedirs(cfg.LOGO_DIR, exist_ok=True)

        # Get manifests missing logos
        with get_session() as session:
            from sqlalchemy import or_
            rows = (
                session.query(
                    Manifest.id,
                    Manifest.tvg_logo,
                    Manifest.tvg_id,
                )
                .filter(Manifest.logo_cached == False)
                .limit(500)
                .all()
            )

        if not rows:
            return 0

        # Build EPG icon lookup: tvg_id -> icon_url
        tvg_ids = [r.tvg_id for r in rows if r.tvg_id]
        epg_icons = {}
        if tvg_ids:
            with get_session() as session:
                epg_rows = (
                    session.query(Epg.channel_id, Epg.icon_url)
                    .filter(
                        Epg.channel_id.in_(tvg_ids),
                        Epg.icon_url.isnot(None),
                        Epg.icon_url != "",
                    )
                    .all()
                )
                epg_icons = {r.channel_id: r.icon_url for r in epg_rows}

        downloaded = 0
        for manifest_id, tvg_logo, tvg_id in rows:
            # Pick the best logo URL: tvg_logo first, then EPG icon
            logo_url = None
            if tvg_logo:
                logo_url = tvg_logo
            elif tvg_id and tvg_id in epg_icons:
                logo_url = epg_icons[tvg_id]

            if not logo_url:
                continue

            try:
                resp = requests.get(logo_url, headers={"User-Agent": USER_AGENT}, timeout=10)
                if resp.status_code != 200:
                    continue
                data = resp.content
                if len(data) < 100:
                    continue
                if LogoManagerService.save_logo(manifest_id, data):
                    with get_session() as session:
                        m = session.query(Manifest).filter_by(id=manifest_id).first()
                        if m:
                            m.logo_cached = True
                    downloaded += 1
            except Exception:
                continue

        logger.info("Logo sync: downloaded %d of %d pending", downloaded, len(rows))
        return downloaded

    @staticmethod
    def logo_url(manifest_id: str) -> str:
        cfg = Config()
        return f"http://{cfg.MANIFOLD_HOST}:{cfg.MANIFOLD_PORT}/logo/{manifest_id}"
