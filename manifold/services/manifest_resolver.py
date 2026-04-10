"""Resolve m3u8 manifests via the selenium-uc sidecar.

The actual browser work happens in the sidecar (Chrome + undetected_chromedriver).
This service is a thin HTTP client that calls the sidecar and stores results
in Manifold's database.
"""

import hashlib
import logging
import re
from urllib.parse import urlparse, urljoin

import requests as http_requests

from manifold.config import Config
from manifold.database import get_session
from manifold.models.manifest import Capture, Manifest, Variant, HeaderProfile

logger = logging.getLogger(__name__)

# Status tracking for async resolve jobs
_status = {"running": False, "last_url": None, "last_error": None, "last_manifest_id": None}

# Batch state
_batch = {"running": False, "total": 0, "completed": 0, "current_url": None, "results": []}


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _sha256(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _sanitize_body(text: str | None) -> str | None:
    """Strip control characters, keep #EXT lines intact."""
    if not text:
        return None
    sanitized = re.sub(r'[\x00-\x1F\x7F]', '', text)
    lines = sanitized.splitlines()
    clean = []
    for line in lines:
        if line.startswith('#EXT'):
            clean.append(line)
        else:
            clean.append(re.sub(r'[\x00-\x1F\x7F\x80-\xFF]', '', line))
    return '\n'.join(clean) or None


def _parse_master_variants(body_text: str, manifest_url: str) -> list[dict]:
    """Parse EXT-X-STREAM-INF entries from a master playlist."""
    if not body_text:
        return []
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#EXT-X-STREAM-INF"):
            attrs = {}
            for kv in re.split(r',(?=[A-Z0-9\-]+=)', ln.split(":", 1)[1]):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    attrs[k] = v.strip('"')
            uri = lines[i + 1] if i + 1 < len(lines) else ""
            abs_url = urljoin(manifest_url, uri)
            res = attrs.get("RESOLUTION")
            w = h = None
            if res and "x" in res:
                try:
                    w, h = map(int, res.split("x"))
                except Exception:
                    pass
            out.append({
                "uri": uri,
                "abs_url": abs_url,
                "bandwidth": int(attrs.get("BANDWIDTH", "0") or 0),
                "resolution": res,
                "frame_rate": float(attrs.get("FRAME-RATE", "0") or 0),
                "codecs": attrs.get("CODECS"),
                "audio_group": attrs.get("AUDIO"),
                "width": w,
                "height": h,
            })
            i += 2
        else:
            i += 1
    return out


def _call_sidecar(url: str, timeout: int) -> dict:
    """POST to the selenium-uc sidecar /capture endpoint."""
    sidecar_url = f"{Config.SELENIUM_URL}/capture"
    # HTTP timeout = browser timeout + 30s buffer for startup/teardown
    http_timeout = timeout + 30
    logger.info("Calling sidecar %s for %s", sidecar_url, url)
    resp = http_requests.post(
        sidecar_url,
        json={"url": url, "timeout": timeout, "switch_iframe": True},
        timeout=http_timeout,
    )
    resp.raise_for_status()
    return resp.json()


class ManifestResolverService:

    @staticmethod
    def get_status():
        return dict(_status)

    @staticmethod
    def check_selenium() -> bool:
        """Check if the selenium-uc sidecar is reachable."""
        try:
            r = http_requests.get(f"{Config.SELENIUM_URL}/health", timeout=5)
            return r.status_code == 200 and r.json().get("ready", False)
        except Exception:
            return False

    @staticmethod
    def resolve(url: str, title: str | None = None, timeout: int = 60) -> dict:
        """Capture an m3u8 manifest via the sidecar and store it in DB."""
        _status["running"] = True
        _status["last_url"] = url
        _status["last_error"] = None

        try:
            capture = _call_sidecar(url, timeout)

            if not capture.get("ok"):
                err = capture.get("error", "Unknown error from sidecar")
                _status["last_error"] = err
                return {"ok": False, "manifest_id": None, "manifest_url": None, "error": err}

            body_text = _sanitize_body(capture.get("body"))
            if not body_text or "#EXTM3U" not in body_text:
                err = "Captured body is not valid HLS"
                _status["last_error"] = err
                return {"ok": False, "manifest_id": None, "manifest_url": None, "error": err}

            # Build context with heartbeat info
            context = {}
            heartbeat = capture.get("heartbeat")
            if heartbeat:
                context["heartbeat_url"] = heartbeat.get("heartbeat_url")
                context["heartbeat_interval"] = 30
                context["auth_headers"] = {
                    k: v for k, v in heartbeat.items()
                    if k != "heartbeat_url" and v is not None
                }
                key_match = re.search(r'#EXT-X-KEY:.*?URI="([^"]+)"', body_text)
                if key_match:
                    context["drm_key_url"] = key_match.group(1)

            manifest_url = capture["manifest_url"]
            manifest_id = _store_manifest(
                page_url=url,
                user_agent=capture.get("user_agent", ""),
                manifest_url=manifest_url,
                mime=capture.get("mime"),
                resp_headers=capture.get("headers"),
                body_text=body_text,
                title=title,
                context=context,
                heartbeat=heartbeat,
            )

            _status["last_manifest_id"] = manifest_id
            logger.info("Manifest resolved and stored: %s -> %s", url, manifest_id)
            return {"ok": True, "manifest_id": manifest_id, "manifest_url": manifest_url, "error": None}

        except http_requests.exceptions.RequestException as e:
            err = f"Sidecar communication failed: {e}"
            logger.exception("Sidecar call failed for %s", url)
            _status["last_error"] = err
            return {"ok": False, "manifest_id": None, "manifest_url": None, "error": err}

        except Exception as e:
            logger.exception("Resolve failed for %s", url)
            _status["last_error"] = str(e)
            return {"ok": False, "manifest_id": None, "manifest_url": None, "error": str(e)}

        finally:
            _status["running"] = False

    @staticmethod
    def get_batch_status():
        return dict(_batch)

    @staticmethod
    def resolve_batch(urls: list[dict], timeout: int = 60):
        """Resolve a list of URLs sequentially."""
        _batch["running"] = True
        _batch["total"] = len(urls)
        _batch["completed"] = 0
        _batch["results"] = [
            {"url": u["url"], "title": u.get("title"), "status": "pending",
             "manifest_id": None, "manifest_url": None, "error": None}
            for u in urls
        ]

        for i, entry in enumerate(urls):
            _batch["current_url"] = entry["url"]
            _batch["results"][i]["status"] = "resolving"

            result = ManifestResolverService.resolve(
                url=entry["url"], title=entry.get("title"), timeout=timeout
            )

            if result["ok"]:
                _batch["results"][i]["status"] = "done"
                _batch["results"][i]["manifest_id"] = result["manifest_id"]
                _batch["results"][i]["manifest_url"] = result["manifest_url"]
            else:
                _batch["results"][i]["status"] = "failed"
                _batch["results"][i]["error"] = result["error"]

            _batch["completed"] = i + 1

        _batch["running"] = False
        _batch["current_url"] = None
        logger.info("Batch resolve complete: %d/%d succeeded",
                     sum(1 for r in _batch["results"] if r["status"] == "done"),
                     _batch["total"])

    @staticmethod
    def retry_batch_item(index: int, timeout: int = 60):
        """Retry a single failed item in the batch."""
        if index < 0 or index >= len(_batch["results"]):
            return {"ok": False, "error": "Invalid index"}

        item = _batch["results"][index]
        if item["status"] != "failed":
            return {"ok": False, "error": "Item is not in failed state"}

        _batch["running"] = True
        _batch["current_url"] = item["url"]
        item["status"] = "resolving"
        item["error"] = None

        result = ManifestResolverService.resolve(
            url=item["url"], title=item.get("title"), timeout=timeout
        )

        if result["ok"]:
            item["status"] = "done"
            item["manifest_id"] = result["manifest_id"]
            item["manifest_url"] = result["manifest_url"]
        else:
            item["status"] = "failed"
            item["error"] = result["error"]

        _batch["running"] = False
        _batch["current_url"] = None
        return result


def _store_manifest(
    *,
    page_url: str,
    user_agent: str,
    manifest_url: str,
    mime: str | None,
    resp_headers: dict | None,
    body_text: str,
    title: str | None,
    context: dict,
    heartbeat: dict | None,
) -> str:
    """Insert capture + manifest into Manifold's DB. Returns manifest ID."""
    source_domain = urlparse(manifest_url).netloc
    kind = "master" if "#EXT-X-STREAM-INF" in body_text else "media"
    url_hash = _md5(manifest_url)
    body_hash = _sha256(body_text)

    # DRM detection
    drm_method = None
    is_drm = False
    if "#EXT-X-KEY" in body_text:
        if "METHOD=SAMPLE-AES" in body_text:
            drm_method, is_drm = "SAMPLE-AES", True
        elif "METHOD=AES-128" in body_text:
            drm_method, is_drm = "AES-128", False

    with get_session() as session:
        cap = Capture(page_url=page_url, user_agent=user_agent, context=context)
        session.add(cap)
        session.flush()

        header_profile_id = None
        if heartbeat:
            profile_name = f"resolved-{source_domain}"
            hp = session.query(HeaderProfile).filter_by(name=profile_name).first()
            auth_headers = {k: v for k, v in heartbeat.items()
                           if k != "heartbeat_url" and v is not None}
            if hp:
                hp.headers = auth_headers
            else:
                hp = HeaderProfile(name=profile_name, headers=auth_headers)
                session.add(hp)
                session.flush()
            header_profile_id = hp.id

        manifest = session.query(Manifest).filter(
            Manifest.url_hash == url_hash,
            Manifest.sha256 == body_hash,
        ).first()

        if manifest:
            manifest.capture_id = cap.id
            if header_profile_id:
                manifest.header_profile_id = header_profile_id
                manifest.requires_headers = True
            manifest.body = body_text
            manifest.sha256 = body_hash
        else:
            manifest = Manifest(
                capture_id=cap.id,
                header_profile_id=header_profile_id,
                url=manifest_url,
                url_hash=url_hash,
                source_domain=source_domain,
                mime=mime,
                kind=kind,
                headers=resp_headers or {},
                requires_headers=bool(header_profile_id),
                body=body_text,
                sha256=body_hash,
                drm_method=drm_method,
                is_drm=is_drm,
                title=title,
                tags=["live", "captured", "resolved"],
                active=True,
            )
            session.add(manifest)
            session.flush()

            if kind == "master":
                for v in _parse_master_variants(body_text, manifest_url):
                    session.add(Variant(manifest_id=manifest.id, **v))

        return manifest.id
