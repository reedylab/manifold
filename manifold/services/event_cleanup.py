"""Event cleanup — delete expired events, parse end times from titles."""

import logging
import re
from datetime import datetime, timedelta, timezone

from manifold.database import get_session
from manifold.models.manifest import Manifest

logger = logging.getLogger(__name__)


class EventCleanupService:

    @staticmethod
    def cleanup_expired():
        """Delete manifests tagged 'event' whose event_end_at is in the past."""
        with get_session() as session:
            expired = (
                session.query(Manifest)
                .filter(
                    Manifest.tags.op("@>")('["event"]'),
                    Manifest.event_end_at.isnot(None),
                    Manifest.event_end_at < datetime.now(timezone.utc),
                )
                .all()
            )
            count = len(expired)
            for m in expired:
                logger.info("Deleting expired event: %s (ended %s)", m.title, m.event_end_at)
                session.delete(m)

        logger.info("Cleaned up %d expired events", count)
        return count

    @staticmethod
    def update_event_end_times():
        """Parse datetime from title for events with NULL event_end_at."""
        with get_session() as session:
            rows = (
                session.query(Manifest)
                .filter(
                    Manifest.tags.op("@>")('["event"]'),
                    Manifest.event_end_at.is_(None),
                )
                .all()
            )

            updated = 0
            for m in rows:
                end_at = _parse_datetime_from_title(m.title)
                if not end_at:
                    # Fallback: 6 hours after creation
                    end_at = m.created_at.replace(tzinfo=timezone.utc) + timedelta(hours=6)
                m.event_end_at = end_at
                updated += 1

        logger.info("Updated event_end_at for %d events", updated)
        return updated


def _parse_datetime_from_title(title: str):
    """Extract MM/DD HH:MM AM/PM ET from title, add 4 hours buffer."""
    if not title:
        return None

    m = re.search(r"\((\d{1,2}/\d{1,2}) (\d{1,2}:\d{2}) (AM|PM) ET\)", title)
    if not m:
        return None

    month, day = map(int, m.group(1).split("/"))
    hour, minute = map(int, m.group(2).split(":"))
    am_pm = m.group(3)

    year = datetime.now().year
    if am_pm == "PM" and hour != 12:
        hour += 12
    if am_pm == "AM" and hour == 12:
        hour = 0

    # ET = UTC-5
    dt = datetime(year, month, day, hour, minute, tzinfo=timezone(timedelta(hours=-5)))

    # Add 4 hours buffer (same as original threadfin_cleanup)
    dt = dt + timedelta(hours=4)

    return dt
