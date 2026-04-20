"""Activation rules — decides which channels are exposed to downstream.

Rules are stored as a JSON blob under the `activation_rules` key in settings.
Channels with `activation_mode == 'auto'` have their `active` flag rewritten
on each ingest based on whether their `primary_tag` is in `tags_auto_on`.
Channels in `force_on` / `force_off` mode are left alone.
"""

import json
import logging

from manifold.config import get_setting, set_setting

logger = logging.getLogger(__name__)

ACTIVATION_RULES_KEY = "activation_rules"

DEFAULT_ACTIVATION_RULES = {
    "tags_auto_on": ["event", "sports", "news", "live"],
}

MODES = ("auto", "force_on", "force_off")


def get_activation_rules() -> dict:
    raw = get_setting(ACTIVATION_RULES_KEY)
    if not raw:
        set_setting(ACTIVATION_RULES_KEY, json.dumps(DEFAULT_ACTIVATION_RULES))
        return json.loads(json.dumps(DEFAULT_ACTIVATION_RULES))
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("Invalid activation_rules JSON in settings; using defaults: %s", e)
        return json.loads(json.dumps(DEFAULT_ACTIVATION_RULES))


def set_activation_rules(rules: dict) -> None:
    set_setting(ACTIVATION_RULES_KEY, json.dumps(rules))


def should_be_active(activation_mode: str, tags: list[str] | None, rules: dict) -> bool | None:
    """Return the desired `active` value for a channel, or None if the mode
    says to leave it alone (force_on/force_off decisions belong to the user).

    Matches against ANY tag in the channel's tags list — so a channel tagged
    ["sports","espn","live"] activates if any of those tags is in tags_auto_on.
    """
    if activation_mode != "auto":
        return None
    tags_auto_on = set(rules.get("tags_auto_on") or [])
    return any(t in tags_auto_on for t in (tags or []))
