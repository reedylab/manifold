"""Generate XMLTV EPG from database.

Every active channel gets EPG data:
  - Real EPG from linked sources (matched via tvg_id)
  - Dummy EPG auto-generated for channels without real data

Dummy EPG settings (from DB settings):
  - dummy_epg_days: how many days of dummy data to generate (default 7)
  - dummy_epg_block_minutes: programme block length in minutes (default 30)
"""

import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from xml.etree.ElementTree import Element, SubElement, ElementTree, indent

from lxml import etree

from manifold.config import Config, get_setting
from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.epg import Epg
from manifold.models.program import Program

logger = logging.getLogger(__name__)

DTD_ORDER = [
    "title", "sub-title", "desc", "credits", "date", "category",
    "keyword", "language", "orig-language", "length", "icon", "url",
    "country", "episode-num", "video", "audio", "previously-shown",
    "premiere", "last-chance", "new", "subtitles", "rating",
    "star-rating", "review", "image",
]


def _fmt_xmltv_time(dt):
    offset = dt.strftime("%z")
    if not offset:
        offset = "+0000"
    return dt.strftime("%Y%m%d%H%M%S") + " " + offset


def _reorder_programme_children(prog_el):
    children = list(prog_el)
    for c in children:
        prog_el.remove(c)
    order_map = {tag: i for i, tag in enumerate(DTD_ORDER)}
    children.sort(key=lambda c: order_map.get(c.tag, len(DTD_ORDER)))
    for c in children:
        prog_el.append(c)


class XMLTVGeneratorService:

    @staticmethod
    def generate():
        cfg = Config()
        output_path = os.path.join(cfg.OUTPUT_DIR, "manifold.xml")
        os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)

        manifold_host = cfg.MANIFOLD_HOST
        manifold_port = cfg.MANIFOLD_PORT

        # Dummy EPG settings
        dummy_days = int(get_setting("dummy_epg_days", "7") or "7")
        dummy_block = int(get_setting("dummy_epg_block_minutes", "30") or "30")

        # Get all active channels with their EPG data (if any)
        from sqlalchemy import or_
        with get_session() as session:
            rows = (
                session.query(
                    Manifest.id,
                    Manifest.title,
                    Manifest.tvg_id,
                    Manifest.logo_cached,
                    Manifest.channel_number,
                    Manifest.title_override,
                    Epg.channel_id,
                    Epg.channel_name,
                    Epg.icon_url,
                    Epg.epg_data,
                    Manifest.tags,
                )
                .outerjoin(
                    Epg,
                    or_(
                        (Manifest.tvg_id == Epg.channel_id) & (Manifest.tvg_id.isnot(None)) & (Manifest.tvg_id != ""),
                        Manifest.title == Epg.channel_name,
                    )
                )
                .filter(Manifest.active == True)
                .filter(
                    Manifest.tags.op("@>")('["live"]')
                    | Manifest.tags.op("@>")('["event"]')
                )
                .order_by(Manifest.channel_number.asc().nullslast(), Manifest.title)
                .all()
            )

        if not rows:
            logger.warning("No active channels for XMLTV generation")
            _write_empty_xmltv(output_path)
            return {"xmltv_channels": 0, "real_epg": 0, "dummy_epg": 0}

        tv = Element("tv", attrib={
            "generator-info-name": "Manifold",
            "source-info-name": "Manifold IPTV",
        })

        # Deduplicate channels. A manifest may match multiple Epg rows (via
        # tvg_id OR title), so prefer the row where Epg.channel_id == tvg_id
        # to avoid picking a stale row that still matches by title. Without
        # this, rename of a source's channel ids leaves orphan rows that
        # masquerade as valid matches and inject old programmes.
        channels = {}
        for manifest_id, title, tvg_id, logo_cached, channel_number, title_override, epg_ch_id, epg_ch_name, icon_url, epg_data, tags in rows:
            is_tvg_match = bool(tvg_id and epg_ch_id == tvg_id)
            existing = channels.get(manifest_id)
            if existing and existing["tvg_match"] and not is_tvg_match:
                # Already have a tvg_id-matched row for this manifest; don't
                # let a weaker title-matched row overwrite it.
                continue
            if existing and not is_tvg_match:
                # Both are non-tvg matches; keep the first.
                continue
            # Must match the M3U's tvg-id (which is Manifest.tvg_id) so Jellyfin
            # can link programmes to channels. Prefer tvg_id, then the EPG row's
            # id if no tvg_id is set, then the manifest id as last resort.
            channel_id = tvg_id or epg_ch_id or manifest_id
            display_title = title_override or title or f"Channel {channel_id}"
            channels[manifest_id] = {
                "channel_id": channel_id,
                "title": display_title,
                "logo_cached": logo_cached,
                "icon_url": icon_url,
                "epg_data": epg_data,
                "tvg_match": is_tvg_match,
                "tags": list(tags or []),
            }

        # Write <channel> elements
        for manifest_id, ch in channels.items():
            chan_el = SubElement(tv, "channel", id=ch["channel_id"])
            dn = SubElement(chan_el, "display-name", lang="en")
            dn.text = ch["title"]
            if ch["logo_cached"]:
                SubElement(chan_el, "icon", src=f"http://{manifold_host}:{manifold_port}/logo/{manifest_id}")
            elif ch["icon_url"]:
                SubElement(chan_el, "icon", src=ch["icon_url"])

        # Build programme image lookup: cleaned_title -> icon URL
        programme_images = {}
        try:
            program_image_dir = os.path.join(Config.DATA_DIR, "program_images")
            with get_session() as session:
                all_progs = session.query(Program.id, Program.title).all()
            for prog_id, prog_title in all_progs:
                filename = f"{prog_id:06d}.jpg"
                if os.path.isfile(os.path.join(program_image_dir, filename)):
                    icon_url = f"http://{manifold_host}:{manifold_port}/output/program-image/{filename}"
                    programme_images[prog_title] = icon_url
        except Exception as e:
            logger.warning("Failed to load programme images: %s", e)

        # Write <programme> elements
        real_count = 0
        dummy_count = 0
        programme_count = 0

        for manifest_id, ch in channels.items():
            if ch["epg_data"]:
                n = _parse_and_append_programmes(tv, ch["channel_id"], ch["epg_data"],
                                                 programme_images, ch["tags"])
                programme_count += n
                if n > 0:
                    real_count += 1
                else:
                    # Parse failed — fall back to dummy
                    programme_count += _generate_dummy_programmes(
                        tv, ch["channel_id"], ch["title"], dummy_days, dummy_block,
                        ch["tags"]
                    )
                    dummy_count += 1
            else:
                programme_count += _generate_dummy_programmes(
                    tv, ch["channel_id"], ch["title"], dummy_days, dummy_block,
                    ch["tags"]
                )
                dummy_count += 1

        indent(tv, space="  ")

        tree = ElementTree(tv)
        dir_name = os.path.dirname(output_path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
                f.write(b'<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
                tree.write(f, encoding="UTF-8", xml_declaration=False)
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, output_path)
        except Exception:
            os.unlink(tmp_path)
            raise

        logger.info("Generated XMLTV: %d channels (%d real, %d dummy), %d programmes -> %s",
                     len(channels), real_count, dummy_count, programme_count, output_path)

        return {
            "xmltv_channels": len(channels),
            "real_epg": real_count,
            "dummy_epg": dummy_count,
            "programmes": programme_count,
        }


def _inject_channel_categories(prog_el, channel_tags):
    """Add <category> subelements for each channel tag that isn't already
    present on the programme. Jellyfin classifies programmes into its fixed
    Movies/Kids/News/Sports buckets by matching these category strings
    against its listing provider's category arrays."""
    if not channel_tags:
        return
    existing = set()
    for c in prog_el.findall("category"):
        if c.text:
            existing.add(c.text.strip().lower())
    for tag in channel_tags:
        t = str(tag).strip().lower()
        if not t or t in existing:
            continue
        cat = SubElement(prog_el, "category", lang="en")
        cat.text = t
        existing.add(t)


def _parse_and_append_programmes(tv_element, channel_id, epg_data, programme_images=None, channel_tags=None):
    count = 0
    if programme_images is None:
        programme_images = {}
    try:
        wrapped = f"<root>{epg_data}</root>"
        root = etree.fromstring(wrapped.encode("utf-8"))
        for prog in root.findall(".//programme"):
            prog.set("channel", channel_id)
            xml_str = etree.tostring(prog, encoding="unicode")
            from xml.etree.ElementTree import fromstring
            stdlib_prog = fromstring(xml_str)

            # Inject programme image icon if available and not already present
            if programme_images:
                title_el = stdlib_prog.find("title")
                existing_icon = stdlib_prog.find("icon")
                if title_el is not None and title_el.text and existing_icon is None:
                    icon_url = programme_images.get(title_el.text)
                    if icon_url:
                        SubElement(stdlib_prog, "icon", src=icon_url)

            _inject_channel_categories(stdlib_prog, channel_tags)
            _reorder_programme_children(stdlib_prog)
            tv_element.append(stdlib_prog)
            count += 1
    except Exception as e:
        logger.warning("Failed to parse EPG data for channel %s: %s", channel_id, e)
    return count


def _generate_dummy_programmes(tv_element, channel_id, channel_title, days=7, block_minutes=30, channel_tags=None):
    """Generate dummy programme blocks for channels without real EPG."""
    now = datetime.now(timezone.utc)
    # Start from beginning of today
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)
    count = 0

    current = start
    while current < end:
        prog_stop = current + timedelta(minutes=block_minutes)

        prog = SubElement(tv_element, "programme",
                          start=_fmt_xmltv_time(current),
                          stop=_fmt_xmltv_time(prog_stop),
                          channel=channel_id)

        title_el = SubElement(prog, "title", lang="en")
        title_el.text = channel_title

        desc_el = SubElement(prog, "desc", lang="en")
        # Vary description slightly by time of day
        hour = current.hour
        if 6 <= hour < 12:
            period = "Morning"
        elif 12 <= hour < 17:
            period = "Afternoon"
        elif 17 <= hour < 22:
            period = "Evening"
        else:
            period = "Late Night"
        desc_el.text = f"{channel_title} — {period} Programming"

        date_el = SubElement(prog, "date")
        date_el.text = current.strftime("%Y%m%d")

        cat_el = SubElement(prog, "category", lang="en")
        cat_el.text = "General"

        _inject_channel_categories(prog, channel_tags)

        current = prog_stop
        count += 1

    return count


def _write_empty_xmltv(path):
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE tv SYSTEM "xmltv.dtd">\n'
        '<tv generator-info-name="Manifold" />\n'
    )
    dir_name = os.path.dirname(path)
    os.makedirs(dir_name, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise
