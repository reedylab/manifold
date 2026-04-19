"""Stream manager — per-channel pipe with shared filler + per-channel overlays.

The filler loop (filler_loop.py) always runs, producing HLS at _filler_loop/.
Each stream has its own segmenter pipe. The per-stream encoder reads from the
filler loop's HLS (with per-channel overlays), probes the live source, then
cuts over to live when ready. Same pipe throughout = clean cutover.
"""

import os
import time
import shutil
import logging
import subprocess
import threading

from manifold.config import Config
from manifold.services import filler_loop

logger = logging.getLogger(__name__)

_streams = {}
_lock = threading.Lock()

MAX_STREAMS = int(os.getenv("MAX_STREAMS", "6"))
STALE_TIMEOUT = int(os.getenv("STREAM_STALE_TIMEOUT", "300"))

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _escape_drawtext(text):
    return text.replace("\\", "\\\\").replace("'", "'\\''").replace(":", "\\:").replace("%", "%%")


def _wrap_title(text, max_chars=40):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}" if cur else w
    if cur:
        lines.append(cur)
    return lines[:3]


def _filler_overlay_vf(channel_title):
    """Build drawtext overlay for filler: UP NEXT + channel title + clock."""
    safe = _escape_drawtext(channel_title or "Live TV")
    text_lines = _wrap_title(safe)
    box_h = 60 + len(text_lines) * 30

    title_draws = ""
    for i, line in enumerate(text_lines):
        y = 80 + i * 30
        sl = _escape_drawtext(line)
        title_draws += (
            f",drawtext=fontfile={FONT}:text='{sl}':"
            f"fontsize=22:fontcolor=white@0.95:x=(w-text_w)/2:y={y}"
        )

    return (
        f"drawbox=x=(w-600)/2:y=40:w=600:h={box_h}:color=black@0.65:t=fill,"
        f"drawtext=fontfile={FONT_BOLD}:text='UP NEXT':"
        f"fontsize=18:fontcolor=0x5aa9ff:x=(w-text_w)/2:y=50"
        f"{title_draws},"
        f"drawtext=fontfile={FONT}:text='%{{localtime\\:%I\\:%M\\:%S %p}}':"
        f"fontsize=20:fontcolor=white@0.8:x=(w-text_w)/2:y=h-40"
    )


class StreamSession:
    def __init__(self, manifest_id, channel_title=""):
        self.manifest_id = manifest_id
        self.channel_title = channel_title
        self.hls_dir = os.path.join(Config.STREAM_DIR, manifest_id)
        self.segmenter = None
        self.encoder = None
        self.pipe_r = None
        self.pipe_w = None
        self.started_at = time.time()
        self.last_accessed = time.time()
        self._stop = threading.Event()
        self._source_ready = threading.Event()
        self._thread = None
        self._live = False

    def start(self, source_url, headers=None):
        os.makedirs(self.hls_dir, exist_ok=True)
        for f in os.listdir(self.hls_dir):
            if f.endswith(".ts") or f.endswith(".m3u8"):
                os.remove(os.path.join(self.hls_dir, f))

        self.pipe_r, self.pipe_w = os.pipe()

        playlist = os.path.join(self.hls_dir, "stream.m3u8")
        seg_pattern = os.path.join(self.hls_dir, "seg_%05d.ts")

        self.segmenter = subprocess.Popen(
            [
                "ffmpeg", "-y", "-loglevel", "warning",
                "-f", "mpegts", "-i", "pipe:0",
                "-c", "copy",
                "-f", "hls", "-hls_time", "6", "-hls_list_size", "10",
                "-hls_flags", "delete_segments+omit_endlist",
                "-hls_segment_filename", seg_pattern,
                playlist,
            ],
            stdin=self.pipe_r,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(self.pipe_r)
        self.pipe_r = None

        self._thread = threading.Thread(
            target=self._run, args=(source_url, headers), daemon=True
        )
        self._thread.start()

    def _run_filler_encoder(self):
        """Read from filler loop HLS, add per-channel overlays, output mpegts to pipe."""
        filler_playlist = filler_loop.get_filler_playlist()
        if not filler_playlist:
            # Filler not ready yet — wait briefly
            for _ in range(20):
                if self._stop.is_set():
                    return
                filler_playlist = filler_loop.get_filler_playlist()
                if filler_playlist:
                    break
                time.sleep(0.5)
        if not filler_playlist:
            return

        overlay = _filler_overlay_vf(self.channel_title)

        cmd = [
            "ffmpeg", "-y", "-loglevel", "warning",
            "-re",
            "-live_start_index", "-1",
            "-i", filler_playlist,
            "-vf", overlay,
            "-r", "30",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-profile:v", "high",
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-f", "mpegts",
            "pipe:1",
        ]

        try:
            self.encoder = subprocess.Popen(
                cmd, stdout=self.pipe_w, stderr=subprocess.PIPE, close_fds=False,
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            while not self._stop.is_set() and not self._source_ready.is_set():
                if self.encoder.poll() is not None:
                    break
                time.sleep(0.3)

            if self.encoder and self.encoder.poll() is None:
                self.encoder.terminate()
                try:
                    self.encoder.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.encoder.kill()
        except Exception as e:
            logger.debug("[%s] Filler encoder error: %s", self.manifest_id, e)
        finally:
            self.encoder = None

    def _probe_source(self, source_url):
        import requests as http_requests
        while not self._stop.is_set() and not self._source_ready.is_set():
            try:
                r = http_requests.head(
                    source_url, headers={"User-Agent": "Mozilla/5.0"},
                    allow_redirects=True, timeout=5,
                )
                if r.status_code < 400:
                    self._source_ready.set()
                    logger.info("[%s] Live source reachable — cutting over",
                                self.manifest_id)
                    return
            except Exception:
                pass
            self._stop.wait(2)

    def _run(self, source_url, headers):
        MAX_RETRIES = 50
        retry_count = 0

        try:
            while not self._stop.is_set() and retry_count < MAX_RETRIES:
                self._live = False
                self._source_ready = threading.Event()

                # Probe live source in background
                threading.Thread(
                    target=self._probe_source, args=(source_url,), daemon=True
                ).start()

                # Play filler with per-channel overlays until live is ready
                if retry_count == 0:
                    logger.info("[%s] Playing filler with overlays...", self.manifest_id)
                else:
                    logger.info("[%s] Stream lost — filler with overlays (retry %d)",
                                self.manifest_id, retry_count)

                self._run_filler_encoder()

                if self._stop.is_set():
                    return

                # Switch to live
                logger.info("[%s] Switching to live: %s",
                            self.manifest_id, source_url[:80])
                self._live = True

                cmd = [
                    "ffmpeg", "-y", "-loglevel", "warning",
                    "-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "10",
                    "-fflags", "+genpts+discardcorrupt+igndts",
                    "-rw_timeout", "15000000",
                ]
                if headers:
                    hs = "\r\n".join(f"{k}: {v}" for k, v in headers.items())
                    cmd += ["-headers", hs + "\r\n"]
                cmd += ["-i", source_url, "-c", "copy", "-f", "mpegts", "pipe:1"]

                self._run_encoder(cmd)

                if self._stop.is_set():
                    return
                self._live = False
                retry_count += 1
                self._stop.wait(2)

        except Exception as e:
            logger.error("[%s] Stream error: %s", self.manifest_id, e)
        finally:
            self._close_pipe()

    def _run_encoder(self, cmd):
        try:
            self.encoder = subprocess.Popen(
                cmd, stdout=self.pipe_w, stderr=subprocess.PIPE, close_fds=False,
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()

            while not self._stop.is_set():
                if self.encoder.poll() is not None:
                    logger.warning("[%s] Encoder exited (code %d)",
                                   self.manifest_id, self.encoder.returncode)
                    break
                time.sleep(0.5)
        except Exception as e:
            logger.error("[%s] Encoder error: %s", self.manifest_id, e)
        finally:
            if self.encoder and self.encoder.poll() is None:
                try:
                    self.encoder.terminate()
                    self.encoder.wait(timeout=3)
                except Exception:
                    try:
                        self.encoder.kill()
                    except Exception:
                        pass
            self.encoder = None

    def _drain_stderr(self):
        try:
            if self.encoder and self.encoder.stderr:
                for _ in self.encoder.stderr:
                    pass
        except Exception:
            pass

    def _close_pipe(self):
        if self.pipe_w is not None:
            try:
                os.close(self.pipe_w)
            except OSError:
                pass
            self.pipe_w = None

    def stop(self):
        self._stop.set()
        self._source_ready.set()
        if self.encoder and self.encoder.poll() is None:
            try:
                self.encoder.terminate()
                self.encoder.wait(timeout=3)
            except Exception:
                try:
                    self.encoder.kill()
                except Exception:
                    pass
        self._close_pipe()
        if self.segmenter and self.segmenter.poll() is None:
            try:
                self.segmenter.terminate()
                self.segmenter.wait(timeout=3)
            except Exception:
                try:
                    self.segmenter.kill()
                except Exception:
                    pass
        try:
            if os.path.isdir(self.hls_dir):
                shutil.rmtree(self.hls_dir)
        except Exception:
            pass

    @property
    def is_running(self):
        return self.segmenter is not None and self.segmenter.poll() is None

    @property
    def is_live(self):
        return self._live


class StreamManagerService:
    @staticmethod
    def stream_dir(manifest_id):
        return os.path.join(Config.STREAM_DIR, manifest_id)

    @staticmethod
    def playlist_path(manifest_id):
        return os.path.join(Config.STREAM_DIR, manifest_id, "stream.m3u8")

    @staticmethod
    def is_running(manifest_id):
        with _lock:
            s = _streams.get(manifest_id)
            if not s:
                return False
            if not s.is_running:
                del _streams[manifest_id]
                return False
            return True

    @staticmethod
    def is_live(manifest_id):
        with _lock:
            s = _streams.get(manifest_id)
            return s.is_live if s else False

    @staticmethod
    def touch(manifest_id):
        with _lock:
            s = _streams.get(manifest_id)
            if s:
                s.last_accessed = time.time()

    @staticmethod
    def start_stream(manifest_id, source_url, headers=None, channel_title=""):
        if StreamManagerService.is_running(manifest_id):
            StreamManagerService.touch(manifest_id)
            return True
        with _lock:
            active = sum(1 for s in _streams.values() if s.is_running)
            if active >= MAX_STREAMS:
                logger.warning("Max streams (%d)", MAX_STREAMS)
                return False

        session = StreamSession(manifest_id, channel_title)
        session.start(source_url, headers)
        with _lock:
            _streams[manifest_id] = session

        playlist = StreamManagerService.playlist_path(manifest_id)
        for _ in range(20):
            if os.path.isfile(playlist) and os.path.getsize(playlist) > 0:
                logger.info("Stream ready: %s", manifest_id)
                return True
            if not session.is_running:
                with _lock:
                    _streams.pop(manifest_id, None)
                return False
            time.sleep(0.5)
        logger.warning("Playlist not ready after 10s: %s", manifest_id)
        return True

    @staticmethod
    def start_proxy(manifest_id, source_url, headers=None, channel_title=""):
        """Proxy-mode variant of start_stream: spins up a segment poller
        instead of an ffmpeg HLS pipeline. Same _streams dict, same cleanup
        paths — ProxyStream duck-types StreamSession's public surface."""
        from manifold.services.proxy_stream import ProxyStream
        if StreamManagerService.is_running(manifest_id):
            StreamManagerService.touch(manifest_id)
            return True
        with _lock:
            active = sum(1 for s in _streams.values() if s.is_running)
            if active >= MAX_STREAMS:
                logger.warning("Max streams (%d)", MAX_STREAMS)
                return False

        session = ProxyStream(manifest_id, source_url, headers, channel_title)
        session.start()
        with _lock:
            _streams[manifest_id] = session

        playlist = StreamManagerService.playlist_path(manifest_id)
        for _ in range(30):  # proxy waits for the first segment download
            if os.path.isfile(playlist) and os.path.getsize(playlist) > 0:
                logger.info("Proxy stream ready: %s", manifest_id)
                return True
            if not session.is_running:
                with _lock:
                    _streams.pop(manifest_id, None)
                return False
            time.sleep(0.5)
        logger.warning("Proxy playlist not ready after 15s: %s", manifest_id)
        return True

    @staticmethod
    def stop_stream(manifest_id):
        with _lock:
            s = _streams.pop(manifest_id, None)
        if s:
            s.stop()
            logger.info("Stopped stream %s", manifest_id)
        return True

    @staticmethod
    def get_status(manifest_id):
        with _lock:
            s = _streams.get(manifest_id)
            if not s or not s.is_running:
                return None
            return {
                "manifest_id": manifest_id, "running": True,
                "live": s.is_live,
                "uptime": round(time.time() - s.started_at),
                "idle": round(time.time() - s.last_accessed),
            }

    @staticmethod
    def list_active():
        result = []
        with _lock:
            dead = []
            for mid, s in _streams.items():
                if not s.is_running:
                    dead.append(mid)
                    continue
                result.append({
                    "manifest_id": mid, "live": s.is_live,
                    "uptime": round(time.time() - s.started_at),
                    "idle": round(time.time() - s.last_accessed),
                })
            for mid in dead:
                del _streams[mid]
        return result

    @staticmethod
    def cleanup_stale():
        now = time.time()
        to_stop = []
        with _lock:
            for mid, s in _streams.items():
                if not s.is_running:
                    to_stop.append(mid)
                elif (now - s.last_accessed) > STALE_TIMEOUT:
                    to_stop.append(mid)
        for mid in to_stop:
            StreamManagerService.stop_stream(mid)
