"""Tag rule engine — keyword-driven channel categorization backed by settings.

Rules are stored as a single JSON blob under the `tag_rules` key in
manifold_settings. On first access the defaults below are seeded so a fresh
install works without manual setup.
"""

import json
import logging

from manifold.config import get_setting, set_setting

logger = logging.getLogger(__name__)

TAG_RULES_KEY = "tag_rules"

# Defaults mirror the previously hardcoded keyword lists in m3u_ingest._compute_tags
# so first-run behavior matches pre-Phase-1 ingest output.
DEFAULT_TAG_RULES = {
    "priority": ["event", "sports", "news", "movies", "kids", "live"],
    "sports": {
        "keywords": ["espn", "sec network", "nba", "nfl", "mlb", "nhl", "fanduel", "sportsnet"],
        "domain_keywords": ["espn"],
    },
    "news": {
        "keywords": ["cnn", "msnbc", "fox news", "bbc", "newsmax", "c-span"],
        "domain_keywords": ["cnn"],
    },
    "movies": {
        "keywords": ["hbo", "showtime", "cinemax", "tmc", "movie"],
        "domain_keywords": [],
    },
    "kids": {
        "keywords": ["disney", "nick", "cartoon", "boomerang", "universal kids"],
        "domain_keywords": [],
    },
}


def get_tag_rules() -> dict:
    """Return the current tag rules, seeding defaults on first read."""
    raw = get_setting(TAG_RULES_KEY)
    if not raw:
        set_setting(TAG_RULES_KEY, json.dumps(DEFAULT_TAG_RULES))
        return json.loads(json.dumps(DEFAULT_TAG_RULES))
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("Invalid tag_rules JSON in settings; using defaults: %s", e)
        return json.loads(json.dumps(DEFAULT_TAG_RULES))


def set_tag_rules(rules: dict) -> None:
    set_setting(TAG_RULES_KEY, json.dumps(rules))


def apply_keyword_rules(rules: dict, title_lower: str, domain_lower: str) -> set[str]:
    """Match the channel's title and domain against every tag's keyword lists."""
    matched: set[str] = set()
    for tag, spec in rules.items():
        if tag == "priority" or not isinstance(spec, dict):
            continue
        keywords = spec.get("keywords") or []
        if any(k in title_lower for k in keywords):
            matched.add(tag)
            continue
        domain_keywords = spec.get("domain_keywords") or []
        if domain_lower and any(k in domain_lower for k in domain_keywords):
            matched.add(tag)
    return matched


def compute_primary_tag(tags: list[str], priority: list[str]) -> str | None:
    """Return the highest-priority tag present on the channel, or None."""
    tag_set = set(tags)
    for tag in priority:
        if tag in tag_set:
            return tag
    return None
