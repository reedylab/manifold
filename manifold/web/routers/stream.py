"""Stream router — passthrough proxy or FFmpeg HLS with filler overlays."""

import os
import re
import logging
from urllib.parse import urljoin, quote

import requests as http_requests
from fastapi import APIRouter, Query, HTTPException
from starlette.responses import Response, StreamingResponse, FileResponse

from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.m3u_source import M3uSource
from manifold.services.stream_manager import StreamManagerService

logger = logging.getLogger(__name__)
router = APIRouter()

MANIFEST_ID_RE = re.compile(r"^[a-f0-9-]+$")
CHUNK = 16384
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


@router.get("/{manifest_id}.m3u8")
def stream_playlist(manifest_id: str, src: str = Query(default=None)):
    if not MANIFEST_ID_RE.match(manifest_id):
        raise HTTPException(status_code=400)
    if src:
        return _proxy_m3u8(manifest_id, src)

    with get_session() as session:
        m = (
            session.query(
                Manifest.url, Manifest.headers, Manifest.title,
                M3uSource.stream_mode,
            )
            .outerjoin(M3uSource, Manifest.m3u_source_id == M3uSource.id)
            .filter(Manifest.id == manifest_id)
            .first()
        )
    if not m:
        raise HTTPException(status_code=404)
    url, headers, title, stream_mode = m
    stream_mode = stream_mode or "passthrough"

    if stream_mode == "ffmpeg":
        StreamManagerService.touch(manifest_id)
        if not StreamManagerService.is_running(manifest_id):
            StreamManagerService.start_stream(manifest_id, url, headers, channel_title=title or "")
        playlist = StreamManagerService.playlist_path(manifest_id)
        if os.path.isfile(playlist):
            return _serve_local_playlist(manifest_id, playlist)
        return _proxy_m3u8(manifest_id, url)
    elif stream_mode == "proxy":
        StreamManagerService.touch(manifest_id)
        if not StreamManagerService.is_running(manifest_id):
            StreamManagerService.start_proxy(manifest_id, url, headers, channel_title=title or "")
        playlist = StreamManagerService.playlist_path(manifest_id)
        if os.path.isfile(playlist):
            return _serve_local_playlist(manifest_id, playlist)
        # No filler in proxy mode — if the first segment hasn't landed yet,
        # fall through to the request-time proxy for this single playlist hit.
        return _proxy_m3u8(manifest_id, url)
    else:
        return _proxy_m3u8(manifest_id, url)


@router.get("/{manifest_id}/{segment:path}")
def stream_segment(manifest_id: str, segment: str):
    if not MANIFEST_ID_RE.match(manifest_id):
        raise HTTPException(status_code=400)
    StreamManagerService.touch(manifest_id)
    p = os.path.join(StreamManagerService.stream_dir(manifest_id), segment)
    if os.path.isfile(p):
        return FileResponse(p, media_type="video/mp2t")
    raise HTTPException(status_code=404)


@router.get("/proxy")
def proxy_segment(url: str = Query(default=None)):
    if not url:
        raise HTTPException(status_code=400)
    return _proxy_bytes(url)


def _serve_local_playlist(mid, path):
    with open(path) as f:
        body = f.read()
    lines = []
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.endswith(".ts"):
            s = f"/stream/{mid}/{s}"
        lines.append(s)
    return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl")


def _proxy_m3u8(mid, url):
    try:
        r = http_requests.get(url, headers={"User-Agent": UA}, timeout=15, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        logger.error("Proxy m3u8 failed: %s", e)
        raise HTTPException(status_code=502)
    lines = []
    for line in r.text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            a = urljoin(r.url, s)
            s = f"/stream/{mid}.m3u8?src={quote(a, safe='')}" if any(s.endswith(x) for x in (".m3u8", ".m3u")) else f"/stream/proxy?url={quote(a, safe='')}"
        lines.append(s)
    return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl")


def _proxy_bytes(url):
    try:
        r = http_requests.get(url, headers={"User-Agent": UA}, stream=True, timeout=15, allow_redirects=True)
        r.raise_for_status()
    except Exception as e:
        logger.error("Proxy failed: %s", e)
        raise HTTPException(status_code=502)

    def gen():
        try:
            for c in r.iter_content(chunk_size=CHUNK):
                yield c
        except Exception:
            pass
        finally:
            r.close()

    return StreamingResponse(gen(), media_type=r.headers.get("Content-Type", "video/mp2t"))
