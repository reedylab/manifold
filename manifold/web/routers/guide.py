"""Guide router — TV guide grid data parsed from manifold.xml."""

import os

from fastapi import APIRouter, Query

from manifold.config import Config

router = APIRouter()


@router.get("/guide")
def guide(hours: int = Query(default=12)):
    from lxml import etree
    from datetime import datetime, timedelta, timezone

    cfg = Config()
    xmltv_path = os.path.join(cfg.OUTPUT_DIR, "manifold.xml")

    if not os.path.isfile(xmltv_path):
        return {"channels": [], "start": "", "end": ""}

    try:
        tree = etree.parse(xmltv_path)
        root = tree.getroot()
    except Exception:
        return {"channels": [], "start": "", "end": ""}

    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=1)
    window_end = now + timedelta(hours=hours)

    channel_map = {}
    for chan in root.findall(".//channel"):
        cid = chan.get("id", "")
        name_el = chan.find("display-name")
        icon_el = chan.find("icon")
        channel_map[cid] = {
            "id": cid,
            "name": name_el.text if name_el is not None else cid,
            "logo": icon_el.get("src", "") if icon_el is not None else "",
            "programmes": [],
        }

    def _parse_ts(ts):
        try:
            return datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    for prog in root.findall(".//programme"):
        cid = prog.get("channel", "")
        start = _parse_ts(prog.get("start", ""))
        stop = _parse_ts(prog.get("stop", ""))
        if not start or not stop:
            continue
        if stop <= window_start or start >= window_end:
            continue
        if cid not in channel_map:
            continue

        title_el = prog.find("title")
        desc_el = prog.find("desc")
        cat_el = prog.find("category")
        icon_el = prog.find("icon")

        channel_map[cid]["programmes"].append({
            "title": title_el.text if title_el is not None else "",
            "start": start.isoformat(),
            "stop": stop.isoformat(),
            "desc": desc_el.text if desc_el is not None else "",
            "category": cat_el.text if cat_el is not None else "",
            "icon": icon_el.get("src", "") if icon_el is not None else "",
        })

    channels = [ch for ch in channel_map.values() if ch["programmes"]]
    channels.sort(key=lambda c: c["name"])

    return {
        "channels": channels,
        "start": window_start.isoformat(),
        "end": window_end.isoformat(),
    }
