"""Auto-numbering — assigns channel numbers based on per-tag ranges.

Behavior per channel (during an ingest pass):
  * No primary_tag or no range configured for the tag → leave number alone.
  * Existing number is in-range for primary_tag → sticky, keep it.
  * Existing number is out-of-range → relocate to next free in the correct range.
  * No existing number → assign lowest free in the range.
  * Range exhausted → log a warning and leave the channel unnumbered.

Numbers are not reclaimed when channels disappear; gaps are intentional so that
a returning channel keeps its number if its row is still in the DB.
"""

import json
import logging

from manifold.config import get_setting, set_setting

logger = logging.getLogger(__name__)

NUMBER_RANGES_KEY = "number_ranges"

DEFAULT_NUMBER_RANGES = {
    "event":  {"start": 500,  "end": 999},
    "sports": {"start": 1000, "end": 1999},
    "news":   {"start": 2000, "end": 2099},
    "movies": {"start": 3000, "end": 3999},
    "kids":   {"start": 4000, "end": 4099},
    "live":   {"start": 5000, "end": 9999},
}


def get_number_ranges() -> dict:
    raw = get_setting(NUMBER_RANGES_KEY)
    if not raw:
        set_setting(NUMBER_RANGES_KEY, json.dumps(DEFAULT_NUMBER_RANGES))
        return json.loads(json.dumps(DEFAULT_NUMBER_RANGES))
    try:
        return json.loads(raw)
    except Exception as e:
        logger.warning("Invalid number_ranges JSON in settings; using defaults: %s", e)
        return json.loads(json.dumps(DEFAULT_NUMBER_RANGES))


def set_number_ranges(ranges: dict) -> None:
    set_setting(NUMBER_RANGES_KEY, json.dumps(ranges))


class AutoNumberer:
    """Stateful numberer for a single ingest pass.

    Takes a snapshot of currently-taken numbers at construction time and tracks
    assignments within the pass so we never double-assign.
    """

    def __init__(self, ranges: dict, taken: set[int]):
        self.ranges = ranges or {}
        self.taken: set[int] = set(taken)
        self._exhausted_warned: set[str] = set()
        # Tracks how many channels couldn't be assigned, keyed by tag, for
        # UI-visible ingest summaries.
        self.unassigned: dict[str, int] = {}

    def assign(self, current_number: int | None, primary_tag: str | None) -> int | None:
        if not primary_tag:
            return current_number
        spec = self.ranges.get(primary_tag)
        if not isinstance(spec, dict):
            return current_number
        try:
            start = int(spec.get("start", 0))
            end = int(spec.get("end", 0))
        except (TypeError, ValueError):
            return current_number
        if start <= 0 or end < start:
            return current_number

        if current_number is not None and start <= current_number <= end:
            self.taken.add(current_number)
            return current_number

        for n in range(start, end + 1):
            if n not in self.taken:
                self.taken.add(n)
                return n

        if primary_tag not in self._exhausted_warned:
            logger.warning(
                "Number range exhausted for tag '%s' (%d-%d)",
                primary_tag, start, end,
            )
            self._exhausted_warned.add(primary_tag)
        self.unassigned[primary_tag] = self.unassigned.get(primary_tag, 0) + 1
        return current_number

    def warnings(self) -> list[dict]:
        """List of structured warnings suitable for the ingest response."""
        out = []
        for tag, count in self.unassigned.items():
            spec = self.ranges.get(tag) or {}
            out.append({
                "type": "range_exhausted",
                "tag": tag,
                "range": [int(spec.get("start", 0)), int(spec.get("end", 0))],
                "unassigned": count,
            })
        return out
