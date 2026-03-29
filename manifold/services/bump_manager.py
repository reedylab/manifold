"""Bump clip manager — scan folders, download from YouTube, serve thumbnails."""

import os
import logging
import threading
import subprocess

from manifold.config import Config

logger = logging.getLogger(__name__)

BUMPS_DIR = os.getenv("BUMPS_PATH", "/app/bumps")
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".ts", ".webm", ".flv", ".wmv"}

_index = {}  # folder_name -> [{"name": str, "path": str}]
_lock = threading.Lock()


class BumpManager:

    @staticmethod
    def scan():
        """Scan the bumps directory and build an index."""
        global _index
        result = {}
        os.makedirs(BUMPS_DIR, exist_ok=True)

        for entry in sorted(os.listdir(BUMPS_DIR)):
            full = os.path.join(BUMPS_DIR, entry)
            if os.path.isdir(full):
                clips = []
                for f in sorted(os.listdir(full)):
                    if os.path.splitext(f)[1].lower() in VIDEO_EXTS:
                        clips.append({"name": f, "path": os.path.join(full, f)})
                if clips:
                    result[entry] = clips
            elif os.path.isfile(full) and os.path.splitext(entry)[1].lower() in VIDEO_EXTS:
                result.setdefault("_root", []).append({"name": entry, "path": full})

        with _lock:
            _index = result

        total = sum(len(v) for v in result.values())
        logger.info("Bump scan: %d folders, %d clips", len(result), total)
        return BumpManager.summary()

    @staticmethod
    def summary():
        with _lock:
            folders = {k: len(v) for k, v in _index.items()}
            total = sum(folders.values())
        return {"folders": folders, "total": total}

    @staticmethod
    def get_all():
        with _lock:
            folders = {k: len(v) for k, v in _index.items()}
            clips = {k: list(v) for k, v in _index.items()}
            total = sum(folders.values())
        return {"folders": folders, "clips": clips, "total": total}

    @staticmethod
    def get_random_clip(folder_names: list[str]) -> str | None:
        """Get a random clip path from the specified folders."""
        import random
        with _lock:
            pool = []
            for f in folder_names:
                pool.extend(c["path"] for c in _index.get(f, []))
        return random.choice(pool) if pool else None

    @staticmethod
    def delete_clip(path: str) -> bool:
        normalized = os.path.normpath(path)
        if not normalized.startswith(os.path.normpath(BUMPS_DIR)):
            return False
        if not os.path.isfile(normalized):
            return False
        try:
            os.remove(normalized)
        except Exception:
            return False
        # Update index
        with _lock:
            for folder, clips in _index.items():
                _index[folder] = [c for c in clips if c["path"] != normalized]
                if not _index[folder]:
                    del _index[folder]
                    break
        return True

    @staticmethod
    def download_url(url: str, folder: str, resolution: str = "1080"):
        """Download a YouTube video to a bump folder (background thread)."""
        dest_dir = os.path.join(BUMPS_DIR, folder)
        os.makedirs(dest_dir, exist_ok=True)

        def _do_download():
            cmd = [
                "yt-dlp",
                "--no-playlist",
                "-f", f"bestvideo[height<={resolution}][ext=mp4]+bestaudio[ext=m4a]/"
                      f"best[height<={resolution}][ext=mp4]/best[height<={resolution}]/best",
                "--merge-output-format", "mp4",
                "-o", os.path.join(dest_dir, "%(title)s.%(ext)s"),
                "--no-overwrites",
                url,
            ]
            logger.info("Downloading bump: %s → %s/ (%sp)", url, folder, resolution)
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
                if result.returncode == 0:
                    logger.info("Bump download complete: %s", url)
                    BumpManager.scan()  # refresh index
                else:
                    logger.error("yt-dlp failed: %s", result.stderr[:500])
            except subprocess.TimeoutExpired:
                logger.error("Bump download timed out: %s", url)
            except Exception as e:
                logger.error("Bump download error: %s", e)

        t = threading.Thread(target=_do_download, daemon=True)
        t.start()

    @staticmethod
    def get_thumbnail(path: str) -> bytes | None:
        """Extract a thumbnail frame from a video file."""
        normalized = os.path.normpath(path)
        if not normalized.startswith(os.path.normpath(BUMPS_DIR)):
            return None
        if not os.path.isfile(normalized):
            return None
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "quiet",
                    "-ss", "1", "-i", normalized,
                    "-vframes", "1",
                    "-vf", "scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2",
                    "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
                ],
                capture_output=True,
                timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception:
            pass
        return None
