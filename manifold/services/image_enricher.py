"""Programme image enrichment — find and cache poster images for EPG titles.

Uses TMDB as primary source, Google Images as fallback.
Images are stored as JPEG files in PROGRAM_IMAGE_DIR.
"""

import io
import logging
import os
import re
import time
import threading
import urllib.parse

import requests
from lxml import etree

from manifold.config import Config, get_setting
from manifold.database import get_session
from manifold.models.program import Program
from manifold.models.epg import Epg

logger = logging.getLogger(__name__)

PROGRAM_IMAGE_DIR = os.path.join(Config.DATA_DIR, "program_images")

# Noise patterns stripped from EPG programme titles before searching
NOISE_PATTERNS = [
    r"\s+Live\b",
    r"\s+New\b",
    r"\s+Repeat\b",
    r"\s+Rerun\b",
    r"\s*\(R\)\b",
    r"\s+S\d+E\d+\b.*",
]

# Google image scoring — preferred sources get a boost
_PREFERRED_SOURCES = {
    "imdb.com": 1.4,
    "themoviedb.org": 1.4,
    "rottentomatoes.com": 1.4,
    "tvmaze.com": 1.0,
    "thetvdb.com": 1.0,
}
_STREAMING_SOURCES = {
    "netflix.com": 0.7,
    "hulu.com": 0.7,
    "amazon.com": 0.7,
    "primevideo.com": 0.7,
    "disneyplus.com": 0.7,
    "hbomax.com": 0.7,
    "peacocktv.com": 0.7,
    "paramountplus.com": 0.7,
    "appletv.com": 0.7,
}
_PENALIZED_SOURCES = {
    "pinterest.com": -0.8,
    "facebook.com": -0.8,
    "twitter.com": -0.8,
    "instagram.com": -0.8,
    "tiktok.com": -0.8,
}
_MERCH_KEYWORDS = ["shop", "store", "buy", "merch", "poster-print", "canvas", "wallpaper", "etsy"]

_GOOGLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


class ImageEnricherService:
    """Find and cache poster images for EPG programme titles."""

    _state = {
        "running": False,
        "total": 0,
        "processed": 0,
        "cached": 0,
        "downloaded": 0,
        "failed": 0,
    }
    _lock = threading.Lock()

    @staticmethod
    def enrich_all():
        """Scan all EPG programmes and enrich uncached titles with images."""
        with ImageEnricherService._lock:
            if ImageEnricherService._state["running"]:
                logger.warning("Image enrichment already running, skipping")
                return
            ImageEnricherService._state.update({
                "running": True,
                "total": 0,
                "processed": 0,
                "cached": 0,
                "downloaded": 0,
                "failed": 0,
            })

        os.makedirs(PROGRAM_IMAGE_DIR, exist_ok=True)

        try:
            # Collect all unique programme titles from EPG data
            titles_channels = ImageEnricherService._collect_epg_titles()
            ImageEnricherService._state["total"] = len(titles_channels)
            logger.info("Image enrichment started: %d unique titles to process", len(titles_channels))

            for title, channel_name in titles_channels.items():
                if not ImageEnricherService._state["running"]:
                    logger.info("Image enrichment stopped by user")
                    break

                result = ImageEnricherService.enrich_programme(title, channel_name)
                ImageEnricherService._state["processed"] += 1

                if result.get("cached"):
                    ImageEnricherService._state["cached"] += 1
                elif result.get("downloaded"):
                    ImageEnricherService._state["downloaded"] += 1
                    time.sleep(0.3)  # rate limit after successful download
                else:
                    ImageEnricherService._state["failed"] += 1

            logger.info(
                "Image enrichment finished: %d processed, %d cached, %d downloaded, %d failed",
                ImageEnricherService._state["processed"],
                ImageEnricherService._state["cached"],
                ImageEnricherService._state["downloaded"],
                ImageEnricherService._state["failed"],
            )
        except Exception as e:
            logger.error("Image enrichment error: %s", e, exc_info=True)
        finally:
            ImageEnricherService._state["running"] = False

    @staticmethod
    def enrich_programme(title: str, channel_name: str = "") -> dict:
        """Find/download image for a single programme title.

        Returns {"cached": bool, "downloaded": bool, "filename": str|None}
        """
        cleaned = ImageEnricherService._clean_title(title)
        if not cleaned:
            return {"cached": False, "downloaded": False, "filename": None}

        with get_session() as session:
            prog = session.query(Program).filter_by(title=cleaned).first()

            if prog:
                filename = f"{prog.id:06d}.jpg"
                filepath = os.path.join(PROGRAM_IMAGE_DIR, filename)

                # Update channels list
                channels = list(prog.channels or [])
                if channel_name and channel_name not in channels:
                    channels.append(channel_name)
                    prog.channels = channels

                # Cache hit — image exists and not flagged for refresh
                if not prog.is_refresh and os.path.isfile(filepath):
                    return {"cached": True, "downloaded": False, "filename": filename}

                # Refresh requested — re-search
                image_url = ImageEnricherService._search_image(cleaned, channel_name)
                if image_url and ImageEnricherService._download_and_save(image_url, filename):
                    prog.is_refresh = False
                    return {"cached": False, "downloaded": True, "filename": filename}

                prog.is_refresh = False
                return {"cached": False, "downloaded": False, "filename": None}

            # New title — create Program row
            prog = Program(
                title=cleaned,
                channels=[channel_name] if channel_name else [],
            )
            session.add(prog)
            session.flush()  # get the id

            filename = f"{prog.id:06d}.jpg"
            image_url = ImageEnricherService._search_image(cleaned, channel_name)
            if image_url and ImageEnricherService._download_and_save(image_url, filename):
                return {"cached": False, "downloaded": True, "filename": filename}

            return {"cached": False, "downloaded": False, "filename": None}

    @staticmethod
    def get_status() -> dict:
        """Return current enrichment progress."""
        return dict(ImageEnricherService._state)

    @staticmethod
    def get_stats() -> dict:
        """Return total cached count, missing count, etc."""
        os.makedirs(PROGRAM_IMAGE_DIR, exist_ok=True)
        with get_session() as session:
            total_programs = session.query(Program).count()

        # Count actual image files on disk
        cached = 0
        if os.path.isdir(PROGRAM_IMAGE_DIR):
            cached = len([f for f in os.listdir(PROGRAM_IMAGE_DIR) if f.endswith(".jpg")])

        return {
            "total_programs": total_programs,
            "cached_images": cached,
            "missing_images": max(0, total_programs - cached),
        }

    @staticmethod
    def stop():
        """Stop a running enrichment."""
        ImageEnricherService._state["running"] = False

    @staticmethod
    def _clean_title(title: str) -> str:
        """Strip noise from EPG programme titles."""
        if not title:
            return ""
        cleaned = title.strip()
        for pattern in NOISE_PATTERNS:
            cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
        return cleaned.strip()

    @staticmethod
    def _search_image(query: str, channel_name: str = "") -> str | None:
        """Search chain: TMDB -> TVMaze -> Fanart.tv -> Google fallback."""
        # Tier 1: TMDB (best for movies + major TV)
        url = ImageEnricherService._tmdb_search(query)
        if url:
            return url

        # Tier 2: TVMaze (free, no auth, great for daytime/cable/talk shows)
        url = ImageEnricherService._tvmaze_search(query)
        if url:
            return url

        # Tier 3: Wikipedia (free, no auth, quality-gated)
        url = ImageEnricherService._wikipedia_search(query)
        if url:
            return url

        # Tier 4: Fanart.tv (optional — only if API key is configured)
        fanart_key = get_setting("fanart_api_key", "")
        if fanart_key:
            url = ImageEnricherService._fanart_search(query)
            if url:
                return url

        return None

    @staticmethod
    def _tmdb_search(query: str) -> str | None:
        """Search TMDB, return poster URL or None. Also caches IDs for Fanart.tv."""
        api_key = get_setting("tmdb_api_key", "")
        if not api_key:
            return None

        try:
            resp = requests.get(
                "https://api.themoviedb.org/3/search/multi",
                params={"query": query, "api_key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            for result in results:
                media_type = result.get("media_type", "")
                if media_type not in ("tv", "movie"):
                    continue

                # Cache IDs for Fanart.tv fallback even if no poster
                tmdb_id = result.get("id")
                if tmdb_id:
                    ImageEnricherService._last_tmdb_id = tmdb_id
                    ImageEnricherService._last_tmdb_type = media_type

                poster_path = result.get("poster_path")
                if poster_path:
                    return f"https://image.tmdb.org/t/p/w342{poster_path}"

        except Exception as e:
            logger.warning("TMDB search failed for '%s': %s", query, e)

        return None

    # Class-level hints for cross-tier ID sharing
    _last_tmdb_id = None
    _last_tmdb_type = None

    @staticmethod
    def _tvmaze_search(query: str) -> str | None:
        """Search TVMaze (free, no auth). Returns image URL or None."""
        try:
            resp = requests.get(
                "https://api.tvmaze.com/singlesearch/shows",
                params={"q": query},
                timeout=10,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            image = data.get("image") or {}
            # Prefer 'original' over 'medium'
            url = image.get("original") or image.get("medium")
            if url:
                logger.debug("TVMaze hit for '%s': %s", query, url)
                return url

        except Exception as e:
            logger.debug("TVMaze search failed for '%s': %s", query, e)

        return None

    _WIKI_HEADERS = {
        "User-Agent": "ManifoldEPG/1.0 (https://github.com/manifold; jake@localhost) requests/2.31",
        "Accept": "application/json",
    }

    @staticmethod
    def _wikipedia_search(query: str) -> str | None:
        """Search Wikipedia for a programme image. Quality-gated: skips small/logo images.

        Uses the Wikipedia API to find the page, then gets the original image file.
        Only returns images that are large enough to be real posters/photos (>300px).
        """
        headers = ImageEnricherService._WIKI_HEADERS
        try:
            # Step 1: Search for the page
            resp = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "format": "json",
                    "list": "search",
                    "srsearch": f"{query} tv OR film OR series",
                    "srlimit": 3,
                },
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            results = resp.json().get("query", {}).get("search", [])
            if not results:
                return None

            # Step 2: Get the page image (high-res original) for each result
            for result in results:
                title = result.get("title", "")
                resp2 = requests.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "titles": title,
                        "prop": "pageimages",
                        "piprop": "original",
                        "pilicense": "any",
                    },
                    headers=headers,
                    timeout=10,
                )
                resp2.raise_for_status()
                pages = resp2.json().get("query", {}).get("pages", {})

                for page in pages.values():
                    original = page.get("original", {})
                    source = original.get("source", "")
                    width = original.get("width", 0)
                    height = original.get("height", 0)

                    if not source:
                        continue

                    # Quality gates:
                    # 1. Must be at least 300px in both dimensions (skip tiny logos/icons)
                    if width < 300 or height < 300:
                        logger.debug("Wikipedia image too small for '%s': %dx%d", query, width, height)
                        continue

                    # 2. Skip SVG and PNG files (usually logos/diagrams/wordmarks)
                    src_lower = source.lower()
                    if src_lower.endswith(".svg") or src_lower.endswith(".png"):
                        logger.debug("Wikipedia image is SVG/PNG (likely logo) for '%s'", query)
                        continue

                    # 3. Skip filenames that look like logos/wordmarks
                    fname_lower = source.rsplit("/", 1)[-1].lower()
                    if any(w in fname_lower for w in ("logo", "wordmark", "icon", "emblem", "seal", "badge")):
                        logger.debug("Wikipedia image filename looks like logo for '%s': %s", query, fname_lower[:40])
                        continue

                    # 4. Prefer portrait-ish or square images (skip extreme banners)
                    aspect = width / height if height > 0 else 1
                    if aspect > 2.5:  # super-wide banner
                        logger.debug("Wikipedia image too wide for '%s': %dx%d", query, width, height)
                        continue

                    logger.debug("Wikipedia hit for '%s': %dx%d %s", query, width, height, source)
                    return source

        except Exception as e:
            logger.debug("Wikipedia search failed for '%s': %s", query, e)

        return None

    @staticmethod
    def _fanart_search(query: str) -> str | None:
        """Search Fanart.tv using TMDB ID from previous tier. Returns image URL or None."""
        fanart_key = get_setting("fanart_api_key", "")
        if not fanart_key:
            return None

        tmdb_id = ImageEnricherService._last_tmdb_id
        media_type = ImageEnricherService._last_tmdb_type
        if not tmdb_id:
            return None

        try:
            if media_type == "tv":
                # Fanart.tv TV endpoint uses TVDB IDs, but supports TMDB lookup
                resp = requests.get(
                    f"https://webservice.fanart.tv/v3/tv/{tmdb_id}",
                    params={"api_key": fanart_key},
                    headers={"api-key": fanart_key},
                    timeout=10,
                )
            else:
                resp = requests.get(
                    f"https://webservice.fanart.tv/v3/movies/{tmdb_id}",
                    params={"api_key": fanart_key},
                    headers={"api-key": fanart_key},
                    timeout=10,
                )

            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()

            # Try tvposter/movieposter first, then hdtvlogo/hdmovielogo, then tvthumb/moviethumb
            poster_keys = (
                ["tvposter", "hdtvlogo", "tvthumb", "showbackground"]
                if media_type == "tv"
                else ["movieposter", "hdmovielogo", "moviethumb", "moviebackground"]
            )
            for key in poster_keys:
                arts = data.get(key, [])
                if arts:
                    # Pick the first English one, or just the first
                    for art in arts:
                        if art.get("lang", "") in ("en", "00", ""):
                            url = art.get("url")
                            if url:
                                logger.debug("Fanart.tv hit for '%s' (%s): %s", query, key, url)
                                return url
                    # Fallback to first regardless of language
                    url = arts[0].get("url")
                    if url:
                        return url

        except Exception as e:
            logger.debug("Fanart.tv search failed for '%s': %s", query, e)

        return None

    @staticmethod
    def _google_fallback(query: str, channel_name: str = "") -> str | None:
        """HTTP-based Google Images search, return best image URL or None."""
        search_query = f"{query} tv show poster"
        encoded = urllib.parse.quote_plus(search_query)
        url = f"https://www.google.com/search?q={encoded}&tbm=isch&tbs=iar:t"

        try:
            resp = requests.get(url, headers=_GOOGLE_HEADERS, timeout=15)
            resp.raise_for_status()
            html = resp.text
        except Exception as e:
            logger.warning("Google image search failed for '%s': %s", query, e)
            return None

        # Extract image URLs from Google's HTML
        # Google embeds image source URLs in script tags and data attributes
        candidates = []

        # Pattern 1: URLs in script tags (Google embeds full-res URLs in JS)
        script_urls = re.findall(
            r'\["(https?://[^"]+\.(?:jpg|jpeg|png|webp)(?:\?[^"]*)?)",[^,]*,[^,]*,"[^"]*"',
            html,
            re.IGNORECASE,
        )
        for img_url in script_urls:
            img_url = img_url.replace("\\u003d", "=").replace("\\u0026", "&")
            candidates.append(img_url)

        # Pattern 2: Image URLs in data attributes or img tags
        attr_urls = re.findall(
            r'(?:src|data-src|data-iurl)="(https?://[^"]+\.(?:jpg|jpeg|png|webp)(?:\?[^"]*)?)"',
            html,
            re.IGNORECASE,
        )
        for img_url in attr_urls:
            if "gstatic.com" not in img_url and "google.com" not in img_url:
                candidates.append(img_url)

        # Pattern 3: URLs in JSON-like structures within script tags
        json_urls = re.findall(
            r'"(https?://[^"]+)"[^}]*"(?:oh|ow)":\s*\d+',
            html,
            re.IGNORECASE,
        )
        for img_url in json_urls:
            img_url = img_url.replace("\\u003d", "=").replace("\\u0026", "&")
            if re.search(r'\.(jpg|jpeg|png|webp)', img_url, re.IGNORECASE):
                candidates.append(img_url)

        if not candidates:
            logger.debug("No Google image candidates found for '%s'", query)
            return None

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for c in candidates:
            if c not in seen:
                seen.add(c)
                unique.append(c)

        # Score candidates
        scored = []
        for img_url in unique:
            score = 1.0
            url_lower = img_url.lower()

            # Preferred sources
            for domain, boost in _PREFERRED_SOURCES.items():
                if domain in url_lower:
                    score += boost
                    break

            # Streaming sources
            for domain, boost in _STREAMING_SOURCES.items():
                if domain in url_lower:
                    score += boost
                    break

            # Penalized sources
            for domain, penalty in _PENALIZED_SOURCES.items():
                if domain in url_lower:
                    score += penalty
                    break

            # Merch penalty
            for keyword in _MERCH_KEYWORDS:
                if keyword in url_lower:
                    score -= 0.6
                    break

            scored.append((score, img_url))

        scored.sort(key=lambda x: x[0], reverse=True)

        # Return the best scoring URL
        best_url = scored[0][1]
        time.sleep(3)  # rate limit Google fallback
        return best_url

    @staticmethod
    def _download_and_save(image_url: str, filename: str) -> bool:
        """Download image, convert to JPEG, save to PROGRAM_IMAGE_DIR."""
        os.makedirs(PROGRAM_IMAGE_DIR, exist_ok=True)
        filepath = os.path.join(PROGRAM_IMAGE_DIR, filename)

        try:
            resp = requests.get(image_url, headers=_GOOGLE_HEADERS, timeout=15)
            resp.raise_for_status()

            if len(resp.content) < 1000:
                logger.debug("Image too small (%d bytes), skipping: %s", len(resp.content), image_url)
                return False

            # Convert to JPEG using Pillow
            from PIL import Image

            img = Image.open(io.BytesIO(resp.content))

            # Handle RGBA (PNG with transparency) and other modes
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(img, mask=img.split()[-1] if "A" in img.mode else None)
                img = background
            elif img.mode != "RGB":
                img = img.convert("RGB")

            img.save(filepath, "JPEG", quality=85, optimize=True)
            logger.info("Saved programme image: %s (%dx%d)", filename, img.width, img.height)
            return True

        except Exception as e:
            logger.warning("Failed to download/save image from %s: %s", image_url, e)
            return False

    @staticmethod
    def _collect_epg_titles() -> dict:
        """Collect all unique programme titles from EPG data.

        Returns dict of {cleaned_title: channel_name}.
        """
        titles = {}

        with get_session() as session:
            rows = session.query(Epg.channel_name, Epg.epg_data).all()

        for channel_name, epg_data in rows:
            if not epg_data:
                continue
            try:
                wrapped = f"<root>{epg_data}</root>"
                root = etree.fromstring(wrapped.encode("utf-8"))
                for prog in root.findall(".//programme"):
                    title_el = prog.find("title")
                    if title_el is not None and title_el.text:
                        cleaned = ImageEnricherService._clean_title(title_el.text)
                        if cleaned and cleaned not in titles:
                            titles[cleaned] = channel_name or ""
            except Exception as e:
                logger.warning("Failed to parse EPG data for channel %s: %s", channel_name, e)

        return titles
