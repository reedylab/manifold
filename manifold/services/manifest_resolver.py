"""Resolve m3u8 manifests from page URLs using headless Chrome via Selenium."""

import base64
import hashlib
import json
import logging
import re
import time
from urllib.parse import urlparse, urljoin

from selenium.webdriver.remote.command import Command

from manifold.config import Config
from manifold.database import get_session
from manifold.models.manifest import Capture, Manifest, Variant, HeaderProfile

logger = logging.getLogger(__name__)

# Status tracking for async resolve jobs
_status = {"running": False, "last_url": None, "last_error": None, "last_manifest_id": None}

# Batch state
_batch = {"running": False, "total": 0, "completed": 0, "current_url": None, "results": []}

MATCH_PATTERNS = ("m3u8", "application/x-mpegurl", "application/vnd.apple.mpegurl")
INCLUDE_TYPES = ("Media", "Fetch", "XHR", "Document", "Other")
# Patterns in JSON API responses that indicate a stream URL field
JSON_STREAM_PATTERNS = ("ngtv.io", "/api/", "/media/", "/stream")


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


def _find_m3u8_in_json(obj) -> str | None:
    """Recursively search a parsed JSON object for an m3u8 URL."""
    if isinstance(obj, str):
        if ".m3u8" in obj and obj.startswith("http"):
            return obj
        return None
    if isinstance(obj, dict):
        for v in obj.values():
            result = _find_m3u8_in_json(v)
            if result:
                return result
    if isinstance(obj, list):
        for item in obj:
            result = _find_m3u8_in_json(item)
            if result:
                return result
    return None


_STEALTH_JS = """
// Mask webdriver property
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// Mask chrome automation indicators
window.navigator.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};

// Mask permissions query
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (params) =>
  params.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(params);

// Mask plugins (headless has 0 plugins)
Object.defineProperty(navigator, 'plugins', {
  get: () => [1, 2, 3, 4, 5].map(() => ({
    description: '', filename: '', length: 0, name: ''
  }))
});

// Mask languages
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

// Mask platform
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});

// Override getParameter to hide RENDERER/VENDOR
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(param) {
  if (param === 37445) return 'Intel Inc.';
  if (param === 37446) return 'Intel Iris OpenGL Engine';
  return getParameter.call(this, param);
};
"""


def _get_browser():
    """Create a Remote WebDriver connected to the Selenium sidecar."""
    from selenium.webdriver import Remote, ChromeOptions

    options = ChromeOptions()
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    # Anti-bot flags (ported from selenium_resolver MyDriver)
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-software-rasterizer")
    options.add_argument("--disable-webgl")
    options.add_argument("--disable-webrtc")
    options.add_argument("--mute-audio")
    options.add_argument("--no-sandbox")
    options.add_argument("--no-zygote")
    options.add_argument("--incognito")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--autoplay-policy=no-user-gesture-required")
    options.add_argument("--disable-site-isolation-trials")
    options.add_argument("--disable-features=IsolateOrigins,site-per-process")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # Suppress automation indicators in navigator
    options.add_argument("--disable-component-extensions-with-background-pages")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    url = f"{Config.SELENIUM_URL}/wd/hub"
    logger.info("Connecting to Selenium at %s", url)
    browser = Remote(command_executor=url, options=options)

    # Inject stealth JS to mask webdriver fingerprint
    browser.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": _STEALTH_JS})

    return browser


def _wait_for_manifest(browser, *, timeout=60):
    """Poll Chrome DevTools performance logs for an m3u8 manifest.

    Returns a dict with url, body, headers, mime, heartbeat info, or None.
    """
    start = time.time()
    req_meta = {}
    resp_meta = {}
    want_body = set()
    captured_heartbeat = None

    while (time.time() - start) < timeout:
        try:
            for entry in browser.execute(Command.GET_LOG, {"type": "performance"})["value"]:
                try:
                    msg = json.loads(entry["message"])["message"]
                except Exception:
                    continue

                method = msg.get("method", "")
                p = msg.get("params", {})
                rid = p.get("requestId")
                if not rid:
                    continue

                rtype = p.get("type") or (p.get("initiator", {}) or {}).get("type")

                if rtype and rtype not in INCLUDE_TYPES:
                    continue

                if method == "Network.requestWillBeSent":
                    req = p.get("request", {}) or {}
                    url = req.get("url", "")
                    accept = req.get("headers", {}).get("Accept", "")
                    headers = req.get("headers", {}) or {}

                    req_meta[rid] = {"url": url, "method": req.get("method"), "headers": headers}

                    url_match = any(pat.lower() in url.lower() for pat in MATCH_PATTERNS)
                    accept_match = any(pat.lower() in accept.lower() for pat in MATCH_PATTERNS) if accept else False

                    # Also match JSON API endpoints that may contain stream URLs
                    api_match = any(pat.lower() in url.lower() for pat in JSON_STREAM_PATTERNS)

                    if url_match or accept_match or api_match:
                        want_body.add(rid)
                        logger.debug("Want body for %s (pattern match)", url)

                    # Capture heartbeat auth headers
                    if "heartbeat" in url.lower():
                        captured_heartbeat = {
                            "heartbeat_url": url,
                            "Authorization": headers.get("authorization") or headers.get("Authorization"),
                            "x-channel-key": headers.get("x-channel-key") or headers.get("X-Channel-Key"),
                            "x-client-token": headers.get("x-client-token") or headers.get("X-Client-Token"),
                            "x-user-agent": headers.get("x-user-agent") or headers.get("X-User-Agent"),
                            "Referer": headers.get("referer") or headers.get("Referer"),
                            "User-Agent": headers.get("user-agent") or headers.get("User-Agent"),
                        }
                        logger.info("Captured heartbeat auth from %s", url)

                if method == "Network.responseReceived":
                    resp = p.get("response", {}) or {}
                    url = resp.get("url", "")
                    mime = resp.get("mimeType", "") or ""

                    resp_meta[rid] = {
                        "status": resp.get("status"),
                        "mime": mime,
                        "headers": resp.get("headers", {}) or {},
                        "url": url,
                    }

                    if any(pat.lower() in (url + " " + mime).lower() for pat in MATCH_PATTERNS):
                        want_body.add(rid)

                if method == "Network.loadingFinished" and rid in want_body:
                    body = ""
                    is_b64 = False
                    try:
                        body_res = browser.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                        body = body_res.get("body", "")
                        is_b64 = body_res.get("base64Encoded", False)
                    except Exception as e:
                        logger.warning("Failed to get body for rid=%s: %s", rid, e)
                        continue

                    req_url = req_meta.get(rid, {}).get("url") or resp_meta.get(rid, {}).get("url", "")

                    # Check if this is a direct m3u8 response
                    if not is_b64 and body and ("#EXTM3U" in body or ".m3u8" in (req_url or "")):
                        return {
                            "url": req_url,
                            "status": resp_meta.get(rid, {}).get("status"),
                            "mime": resp_meta.get(rid, {}).get("mime"),
                            "req_headers": req_meta.get(rid, {}).get("headers", {}),
                            "resp_headers": resp_meta.get(rid, {}).get("headers", {}),
                            "body": body,
                            "base64Encoded": is_b64,
                            "heartbeat": captured_heartbeat,
                        }

                    # Check if this is a JSON API response containing an m3u8 URL
                    if not is_b64 and body:
                        try:
                            json_data = json.loads(body)
                            stream_url = _find_m3u8_in_json(json_data)
                            if stream_url:
                                logger.info("Found m3u8 URL in JSON response from %s: %s", req_url, stream_url[:200])
                                # Fetch the actual m3u8 manifest
                                import requests as http_requests
                                ua = browser.execute_script("return navigator.userAgent")
                                resp = http_requests.get(stream_url, timeout=15,
                                                         headers={"User-Agent": ua, "Referer": req_url})
                                if resp.status_code == 200 and "#EXTM3U" in resp.text:
                                    return {
                                        "url": stream_url,
                                        "status": resp.status_code,
                                        "mime": resp.headers.get("Content-Type", "application/vnd.apple.mpegurl"),
                                        "req_headers": {"User-Agent": ua, "Referer": req_url},
                                        "resp_headers": dict(resp.headers),
                                        "body": resp.text,
                                        "base64Encoded": False,
                                        "heartbeat": captured_heartbeat,
                                        "source_api_url": req_url,
                                    }
                        except (json.JSONDecodeError, ValueError):
                            pass

        except Exception as e:
            logger.error("Error processing performance logs: %s", e)

        time.sleep(0.08)

    logger.warning("No manifest found within %ds timeout", timeout)
    return None


def _decode_body(result: dict) -> str | None:
    """Decode body from capture result, validate HLS signature."""
    if result.get("base64Encoded"):
        raw = result["body"]
        if not isinstance(raw, str):
            raw = str(raw)
        try:
            binary = base64.b64decode("".join(raw.split()), validate=True)
            try:
                text = binary.decode("utf-8")
            except UnicodeDecodeError:
                text = binary.decode("latin-1", errors="replace")
        except base64.binascii.Error:
            return None
    else:
        text = result.get("body")

    if not text or "#EXTM3U" not in text:
        return None

    return _sanitize_body(text)


class ManifestResolverService:

    @staticmethod
    def get_status():
        return dict(_status)

    @staticmethod
    def resolve(url: str, title: str | None = None, timeout: int = 60) -> dict:
        """Navigate to a URL, capture the m3u8 manifest, store in DB.

        Returns {"ok": bool, "manifest_id": str|None, "manifest_url": str|None, "error": str|None}
        """
        _status["running"] = True
        _status["last_url"] = url
        _status["last_error"] = None

        browser = None
        try:
            browser = _get_browser()

            logger.info("Navigating to %s", url)
            browser.get(url)

            # Try switching into player iframe
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC

            try:
                WebDriverWait(browser, 10).until(
                    EC.frame_to_be_available_and_switch_to_it((By.TAG_NAME, "iframe"))
                )
                logger.info("Switched to player iframe")
            except Exception:
                logger.debug("No iframe found, continuing in main frame")

            result = _wait_for_manifest(browser, timeout=timeout)
            if not result:
                _status["last_error"] = "No manifest captured within timeout"
                return {"ok": False, "manifest_id": None, "manifest_url": None,
                        "error": _status["last_error"]}

            body_text = _decode_body(result)
            if not body_text:
                _status["last_error"] = f"Captured resource is not HLS: {result.get('url')} (MIME: {result.get('mime')})"
                return {"ok": False, "manifest_id": None, "manifest_url": None,
                        "error": _status["last_error"]}

            # Get user agent from browser
            ua = browser.execute_script("return navigator.userAgent")

            # Build context with heartbeat info
            context = {}
            heartbeat = result.get("heartbeat")
            if heartbeat:
                context["heartbeat_url"] = heartbeat["heartbeat_url"]
                context["heartbeat_interval"] = 30
                context["auth_headers"] = {
                    k: v for k, v in heartbeat.items()
                    if k != "heartbeat_url" and v is not None
                }
                key_match = re.search(r'#EXT-X-KEY:.*?URI="([^"]+)"', body_text)
                if key_match:
                    context["drm_key_url"] = key_match.group(1)

            # Store to DB
            manifest_url = result["url"]
            manifest_id = _store_manifest(
                page_url=url,
                user_agent=ua,
                manifest_url=manifest_url,
                mime=result.get("mime"),
                resp_headers=result.get("resp_headers"),
                body_text=body_text,
                title=title,
                context=context,
                heartbeat=heartbeat,
            )

            _status["last_manifest_id"] = manifest_id
            logger.info("Manifest resolved and stored: %s -> %s", url, manifest_id)
            return {"ok": True, "manifest_id": manifest_id, "manifest_url": manifest_url, "error": None}

        except Exception as e:
            logger.exception("Resolve failed for %s", url)
            _status["last_error"] = str(e)
            return {"ok": False, "manifest_id": None, "manifest_url": None, "error": str(e)}

        finally:
            if browser:
                try:
                    browser.quit()
                except Exception:
                    pass
            _status["running"] = False

    @staticmethod
    def check_selenium() -> bool:
        """Check if the Selenium sidecar is reachable."""
        import requests as http_requests
        try:
            r = http_requests.get(f"{Config.SELENIUM_URL}/status", timeout=5)
            return r.status_code == 200 and r.json().get("value", {}).get("ready", False)
        except Exception:
            return False

    @staticmethod
    def get_batch_status():
        return dict(_batch)

    @staticmethod
    def resolve_batch(urls: list[dict], timeout: int = 60):
        """Resolve a list of URLs sequentially. Each entry: {"url": str, "title": str|None}."""
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
        # Create capture record
        cap = Capture(page_url=page_url, user_agent=user_agent, context=context)
        session.add(cap)
        session.flush()

        # Create or find header profile from heartbeat auth
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

        # Check for existing manifest by URL hash + body hash
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

            # Parse and store variants if master playlist
            if kind == "master":
                for v in _parse_master_variants(body_text, manifest_url):
                    session.add(Variant(manifest_id=manifest.id, **v))

        return manifest.id
