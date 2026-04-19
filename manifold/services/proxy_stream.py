"""Proxy streaming — poll upstream HLS, download segments, serve locally.

Pulls each new .ts byte-for-byte to /app/streams/{manifest_id}/ and writes a
local playlist pointing at those filenames. No re-encode, no remux — pure
segment copy. Ideal for upstream CDNs that already deliver HLS-ready MPEG-TS
in the codec the client wants. Zero loss and lower CPU than ffmpeg -c copy.

Headers (Referer/Origin/User-Agent) are injected at download time, which lets
proxy mode work against CDNs that reject requests missing those headers —
even when the client (Jellyfin etc.) couldn't emit them directly.

No resolver = no full-manifest-URL refresh. If the playlist URL itself 401s,
we bail after a few retries and let the caller notice (the HLS output dir
just stops getting new segments; clients move on).
"""

import logging
import os
import re
import threading
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests

from manifold.config import Config
from manifold.database import get_session
from manifold.models.manifest import Manifest

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2
MAX_SEGMENTS_ON_DISK = 10
MAX_AUTH_ERRORS = 3

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class ProxyStream:
    """One proxy-mode session. Duck-types StreamSession's public surface
    (is_running / is_live / stop / last_accessed / started_at / hls_dir)
    so stream_manager can track proxy and ffmpeg sessions in the same dict."""

    def __init__(self, manifest_id, source_url, headers=None, channel_title=""):
        self.manifest_id = manifest_id
        self.source_url = source_url
        self.explicit_headers = dict(headers) if headers else {}
        self.channel_title = channel_title
        self.hls_dir = os.path.join(Config.STREAM_DIR, manifest_id)
        self.started_at = time.time()
        self.last_accessed = time.time()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._live = False

        self._source_domain = ""
        try:
            with get_session() as s:
                row = s.query(Manifest.source_domain).filter_by(id=manifest_id).first()
                self._source_domain = (row[0] if row and row[0] else "") or ""
        except Exception:
            pass
        if not self._source_domain:
            try:
                self._source_domain = urlparse(source_url).netloc
            except Exception:
                pass

    def _upstream_headers(self) -> dict:
        h = {"User-Agent": _UA}
        if self._source_domain:
            h["Referer"] = f"https://{self._source_domain}/"
            h["Origin"] = f"https://{self._source_domain}"
        h.update(self.explicit_headers)
        return h

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def start(self):
        os.makedirs(self.hls_dir, exist_ok=True)
        for f in os.listdir(self.hls_dir):
            if f.endswith(".ts") or f.endswith(".m3u8"):
                try:
                    os.remove(os.path.join(self.hls_dir, f))
                except OSError:
                    pass
        self._stop.clear()
        self.started_at = time.time()
        self.last_accessed = time.time()
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"proxy-poller-{self.manifest_id}",
        )
        self._thread.start()
        logger.info("[PROXY] Started %s (%s)", self.manifest_id, self.source_url[:80])

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        try:
            if os.path.isdir(self.hls_dir):
                for f in os.listdir(self.hls_dir):
                    if f.endswith(".ts") or f.endswith(".m3u8"):
                        try:
                            os.remove(os.path.join(self.hls_dir, f))
                        except OSError:
                            pass
        except Exception:
            pass
        logger.info("[PROXY] Stopped %s", self.manifest_id)

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_live(self) -> bool:
        return self._live

    # ── Variant resolution (HLS master → highest-bandwidth variant) ──────

    def _resolve_variant_url(self, url: str) -> str:
        try:
            r = requests.get(url, headers=self._upstream_headers(), timeout=10)
            text = r.text
        except Exception:
            return url
        if "#EXT-X-STREAM-INF" not in text:
            return url
        best_bw = -1
        best_uri = None
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", line)
                if m and i + 1 < len(lines):
                    uri = lines[i + 1].strip()
                    bw = int(m.group(1))
                    if bw > best_bw and uri and not uri.startswith("#"):
                        best_bw = bw
                        best_uri = uri
        return urljoin(url, best_uri) if best_uri else url

    # ── Poll loop ─────────────────────────────────────────────────────────

    def _poll_loop(self):
        seen_seqs: set = set()
        auth_errors = 0
        local_seq = 0
        segment_files: list[tuple[int, str, float]] = []

        variant_url = self._resolve_variant_url(self.source_url)
        logger.info("[PROXY] %s polling %s", self.manifest_id, variant_url[:120])

        while not self._stop.is_set():
            try:
                r = requests.get(variant_url, headers=self._upstream_headers(), timeout=10)
                if r.status_code in (401, 403, 404):
                    auth_errors += 1
                    logger.warning("[PROXY] %s upstream HTTP %d (#%d)",
                                   self.manifest_id, r.status_code, auth_errors)
                    if auth_errors >= MAX_AUTH_ERRORS:
                        logger.error("[PROXY] %s bailing after %d auth errors",
                                     self.manifest_id, auth_errors)
                        return
                    self._stop.wait(POLL_INTERVAL)
                    continue
                if r.status_code != 200:
                    self._stop.wait(POLL_INTERVAL)
                    continue
                auth_errors = 0
            except Exception as e:
                logger.warning("[PROXY] %s fetch failed: %s", self.manifest_id, e)
                self._stop.wait(POLL_INTERVAL)
                continue

            # Parse MEDIA-SEQUENCE once per playlist — each segment's true
            # identity is (media_sequence + position_in_playlist). Filename
            # digits are not reliable (smartcdn encodes duration-ms in the
            # filename, which repeats across different segments).
            media_seq = 0
            for line in r.text.splitlines():
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    try:
                        media_seq = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        media_seq = 0
                    break

            segments = []
            current_duration = 6.0
            seg_pos = 0
            for line in r.text.splitlines():
                line = line.strip()
                if line.startswith("#EXTINF:"):
                    try:
                        current_duration = float(line.split(":")[1].split(",")[0])
                    except (ValueError, IndexError):
                        pass
                elif line and not line.startswith("#"):
                    uri = urljoin(r.url, line)
                    seq = media_seq + seg_pos
                    segments.append((uri, seq, current_duration))
                    seg_pos += 1

            new_count = 0
            for uri, seq, duration in segments:
                if self._stop.is_set():
                    return
                if seq in seen_seqs:
                    continue
                seen_seqs.add(seq)
                local_filename = f"seg_{local_seq:05d}.ts"
                local_path = os.path.join(self.hls_dir, local_filename)
                try:
                    self._download_segment(uri, local_path)
                    segment_files.append((local_seq, local_filename, duration))
                    local_seq += 1
                    new_count += 1
                    self._live = True
                except Exception as e:
                    logger.warning("[PROXY] %s segment %s failed: %s",
                                   self.manifest_id, seq, e)

            # Rolling window of segments on disk
            while len(segment_files) > MAX_SEGMENTS_ON_DISK:
                _, old, _ = segment_files.pop(0)
                try:
                    os.remove(os.path.join(self.hls_dir, old))
                except OSError:
                    pass

            if segment_files:
                self._write_playlist(segment_files)

            if new_count:
                logger.debug("[PROXY] %s +%d segments (disk=%d)",
                             self.manifest_id, new_count, len(segment_files))

            # Prune seen_seqs so memory stays bounded
            if len(seen_seqs) > 1000 and segments:
                min_keep = min(s[1] for s in segments)
                seen_seqs = {s for s in seen_seqs if s >= min_keep}

            self._stop.wait(POLL_INTERVAL)

    def _download_segment(self, uri: str, local_path: str):
        r = requests.get(uri, headers=self._upstream_headers(),
                         timeout=15, stream=True)
        r.raise_for_status()
        with open(local_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=65536):
                if self._stop.is_set():
                    return
                f.write(chunk)

    def _write_playlist(self, segment_files):
        playlist = os.path.join(self.hls_dir, "stream.m3u8")
        first_seq = segment_files[0][0]
        max_dur = max(d for _, _, d in segment_files)
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{int(max_dur) + 1}",
            f"#EXT-X-MEDIA-SEQUENCE:{first_seq}",
        ]
        for _, filename, duration in segment_files:
            lines.append(f"#EXTINF:{duration:.3f},")
            lines.append(filename)
        with open(playlist, "w") as f:
            f.write("\n".join(lines) + "\n")
