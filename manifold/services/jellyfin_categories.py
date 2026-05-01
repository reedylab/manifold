"""Maps manifold's tag taxonomy to Jellyfin's four Live-TV display buckets.

Jellyfin's Live TV has hardcoded Movies / Kids / News / Sports tabs that
classify programmes by <category> elements in the XMLTV feed. The listing
provider holds four arrays (MovieCategories / KidsCategories / ... ) that
say which category strings belong in each bucket.

This service:
  * Stores the mapping in manifold_settings as a single JSON blob so it's
    editable via the UI / API.
  * Seeds sensible defaults drawn from the tag system we already built
    (sports → braves/falcons/event/etc., news → news+local, and so on).
  * The rebind step reads this map and pushes it onto the Jellyfin listing
    provider so the two sides stay in sync without manual clicks.
"""

import json
import logging

from manifold.config import get_setting, set_setting

logger = logging.getLogger(__name__)

CATEGORY_MAP_KEY = "jellyfin_category_map"

# Defaults reference tags that exist in the auto-tagging system today.
# Users can add/remove via the UI without touching code.
DEFAULT_CATEGORY_MAP = {
    "movies": ["movies", "movie"],
    "kids":   ["kids", "disney", "nickelodeon", "nick jr", "nicktoons",
               "teennick", "cartoon", "boomerang", "family", "children"],
    "news":   ["news", "local"],
    "sports": ["sports", "event", "streameast", "local_sports",
               "braves", "falcons", "bulldogs", "hawks",
               "mlb", "nba", "nfl", "nhl", "ncaaf", "ncaab", "ufc"],
}

# Jellyfin field name for each bucket on the ListingProvider payload.
JF_FIELD_FOR_BUCKET = {
    "movies": "MovieCategories",
    "kids":   "KidsCategories",
    "news":   "NewsCategories",
    "sports": "SportsCategories",
}


def get_category_map() -> dict:
    raw = get_setting(CATEGORY_MAP_KEY)
    if not raw:
        set_setting(CATEGORY_MAP_KEY, json.dumps(DEFAULT_CATEGORY_MAP))
        return json.loads(json.dumps(DEFAULT_CATEGORY_MAP))
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("Invalid jellyfin_category_map JSON; using defaults: %s", e)
        return json.loads(json.dumps(DEFAULT_CATEGORY_MAP))


def set_category_map(m: dict) -> None:
    set_setting(CATEGORY_MAP_KEY, json.dumps(m))


def apply_to_provider(provider: dict) -> dict:
    """Return a shallow copy of a Jellyfin ListingProvider dict with the four
    category arrays overwritten from the current settings map."""
    out = dict(provider)
    mapping = get_category_map()
    for bucket, field in JF_FIELD_FOR_BUCKET.items():
        values = [str(v).strip().lower() for v in (mapping.get(bucket) or []) if str(v).strip()]
        if values:
            out[field] = values
    return out
