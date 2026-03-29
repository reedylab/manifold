"""Persistent filler loop — always-running HLS stream from bump clips.

Produces a continuously-updating HLS playlist at:
  /app/streams/_filler_loop/stream.m3u8

Any channel that isn't live yet serves from this shared filler.
Loops through available bump clips with overlays. Falls back to
a generated "Loading..." screen if no bumps are available.
"""

import os
import time
import logging
import subprocess
import threading

from manifold.config import Config

logger = logging.getLogger(__name__)

FILLER_LOOP_DIR = os.path.join(Config.STREAM_DIR, "_filler_loop")
FILLER_FALLBACK = os.path.join(Config.STREAM_DIR, "_filler_fallback.ts")
FALLBACK_DURATION = 30

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_loop_thread = None
_stop_event = threading.Event()


def _build_filler_vf() -> str:
    """Build the video filter chain for the filler loop with branding overlay."""
    f = FONT
    parts = [
        # Base: scale to 1080p with letterboxing
        "scale=1920:1080:force_original_aspect_ratio=decrease",
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2",
        "format=yuv420p",
        # Dark box at bottom center
        "drawbox=x=(w-460)/2:y=h-130:w=460:h=110:color=black@0.55:t=fill",
        # "MANIFOLD" branding
        f"drawtext=fontfile={f}:text='MANIFOLD':fontsize=26:fontcolor=0x5aa9ff:x=(w-text_w)/2:y=h-122",
        # "Starting soon..."
        f"drawtext=fontfile={f}:text='Starting soon...':fontsize=18:fontcolor=white@0.8:x=(w-text_w)/2:y=h-88",
        # Live clock
        f"drawtext=fontfile={f}:text='%{{localtime\\:%I\\:%M\\:%S %p}}':fontsize=22:fontcolor=white@0.9:x=(w-text_w)/2:y=h-55",
    ]
    return ",".join(parts)


def _ensure_fallback():
    """Generate a simple loopable fallback video."""
    if os.path.isfile(FILLER_FALLBACK):
        return
    os.makedirs(os.path.dirname(FILLER_FALLBACK), exist_ok=True)
    logger.info("Generating fallback filler video...")
    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i",
        f"color=c=0x161a21:s=1280x720:d={FALLBACK_DURATION}:r=30,"
        f"drawtext=fontfile={FONT}:"
        "text='Starting soon...':fontcolor=white:fontsize=42:"
        "x=(w-text_w)/2:y=(h-text_h)/2",
        "-f", "lavfi", "-i",
        f"anullsrc=channel_layout=stereo:sample_rate=48000:duration={FALLBACK_DURATION}",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-profile:v", "high", "-force_key_frames", "expr:gte(t,n_forced*6)",
        "-c:a", "aac", "-b:a", "128k",
        "-f", "mpegts", "-t", str(FALLBACK_DURATION),
        FILLER_FALLBACK,
    ]
    try:
        subprocess.run(cmd, capture_output=True, timeout=60)
        logger.info("Fallback filler ready")
    except Exception as e:
        logger.error("Failed to generate fallback filler: %s", e)


def _get_concat_list() -> str:
    """Build an FFmpeg concat file from available bump clips."""
    from manifold.services.bump_manager import BumpManager
    data = BumpManager.get_all()
    clips = []
    for folder_clips in data.get("clips", {}).values():
        for c in folder_clips:
            if os.path.isfile(c["path"]):
                clips.append(c["path"])

    if not clips:
        # No bumps — use fallback
        _ensure_fallback()
        if os.path.isfile(FILLER_FALLBACK):
            clips = [FILLER_FALLBACK]
        else:
            return None

    # Write concat list
    concat_path = os.path.join(FILLER_LOOP_DIR, "_concat.txt")
    with open(concat_path, "w") as f:
        for path in clips:
            safe = path.replace("'", "'\\''")
            f.write(f"file '{safe}'\n")
    return concat_path


def _run_loop():
    """Main filler loop thread — runs FFmpeg continuously."""
    os.makedirs(FILLER_LOOP_DIR, exist_ok=True)

    playlist = os.path.join(FILLER_LOOP_DIR, "stream.m3u8")
    seg_pattern = os.path.join(FILLER_LOOP_DIR, "seg_%05d.ts")

    while not _stop_event.is_set():
        concat_path = _get_concat_list()
        if not concat_path:
            logger.warning("Filler loop: no clips available, retrying in 30s")
            _stop_event.wait(30)
            continue

        logger.info("Filler loop starting (encoding bump clips to HLS)...")

        # Build overlay filter
        vf = _build_filler_vf()

        # Single FFmpeg: read concat → encode with overlays → HLS output
        cmd = [
            "ffmpeg", "-y",
            "-loglevel", "warning",
            "-re",
            "-f", "concat", "-safe", "0",
            "-stream_loop", "-1",  # infinite loop
            "-i", concat_path,
            "-vf", vf,
            "-r", "30",
            "-c:v", "libx264",
            "-preset", "fast",
            "-profile:v", "high",
            "-force_key_frames", "expr:gte(t,n_forced*6)",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
            "-f", "hls",
            "-hls_time", "6",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments+omit_endlist",
            "-hls_segment_filename", seg_pattern,
            playlist,
        ]

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

            # Drain stderr
            def _drain():
                try:
                    for _ in proc.stderr:
                        pass
                except Exception:
                    pass

            threading.Thread(target=_drain, daemon=True).start()

            # Wait until stopped or process exits
            while not _stop_event.is_set():
                if proc.poll() is not None:
                    logger.warning("Filler loop FFmpeg exited (code %d), restarting...", proc.returncode)
                    break
                time.sleep(1)

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        except Exception as e:
            logger.error("Filler loop error: %s", e)

        if not _stop_event.is_set():
            time.sleep(2)  # brief pause before restart


def start_filler_loop():
    """Start the persistent filler loop in a background thread."""
    global _loop_thread
    if _loop_thread and _loop_thread.is_alive():
        return
    _stop_event.clear()
    _loop_thread = threading.Thread(target=_run_loop, daemon=True, name="filler-loop")
    _loop_thread.start()
    logger.info("Filler loop thread started")


def stop_filler_loop():
    """Stop the filler loop."""
    _stop_event.set()


def get_filler_playlist() -> str | None:
    """Return path to the filler loop's HLS playlist, if it exists."""
    p = os.path.join(FILLER_LOOP_DIR, "stream.m3u8")
    return p if os.path.isfile(p) else None


def get_filler_segment(name: str) -> str | None:
    """Return path to a filler loop segment."""
    p = os.path.join(FILLER_LOOP_DIR, name)
    return p if os.path.isfile(p) else None
