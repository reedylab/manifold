"""HDHomeRun emulation — makes Manifold discoverable by Plex as a tuner."""

import uuid
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from manifold.config import Config, get_setting
from manifold.database import get_session
from manifold.models.manifest import Manifest
from manifold.models.epg import Epg

logger = logging.getLogger(__name__)

router = APIRouter()

DEVICE_ID = "12345678"
DEVICE_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, "manifold.hdhr"))


def _base_url(request: Request):
    return str(request.base_url).rstrip("/")


@router.get("/discover.json")
def discover(request: Request):
    base = _base_url(request)
    return {
        "FriendlyName": "Manifold",
        "Manufacturer": "Manifold",
        "ModelNumber": "HDTC-2US",
        "FirmwareName": "hdhomerun_atsc",
        "TunerCount": 2,
        "FirmwareVersion": "20250301",
        "DeviceID": DEVICE_ID,
        "DeviceAuth": "manifold",
        "BaseURL": base,
        "LineupURL": f"{base}/lineup.json",
    }


@router.get("/lineup_status.json")
def lineup_status():
    return {
        "ScanInProgress": 0,
        "ScanPossible": 1,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }


@router.get("/lineup.json")
def lineup():
    cfg = Config()
    manifold_host = cfg.MANIFOLD_HOST
    manifold_port = cfg.MANIFOLD_PORT

    with get_session() as session:
        rows = (
            session.query(
                Manifest.id,
                Manifest.title,
                Manifest.tags,
                Manifest.channel_number,
                Manifest.title_override,
                Epg.channel_id,
            )
            .outerjoin(Epg, Manifest.title == Epg.channel_name)
            .filter(Manifest.active == True)
            .filter(
                Manifest.tags.op("@>")('["live"]')
                | Manifest.tags.op("@>")('["event"]')
            )
            .order_by(Manifest.channel_number.asc().nullslast(), Manifest.title)
            .all()
        )

    lineup = []
    for i, (manifest_id, title, tags, channel_number, title_override, channel_id) in enumerate(rows, start=1):
        display_title = title_override or title or f"Channel {i}"
        guide_number = str(channel_number) if channel_number is not None else str(i)
        relay_url = f"http://{manifold_host}:{manifold_port}/stream/{manifest_id}.m3u8"
        entry = {
            "GuideNumber": guide_number,
            "GuideName": display_title,
            "URL": relay_url,
        }
        if channel_id and channel_number is None:
            entry["GuideNumber"] = channel_id
        lineup.append(entry)

    return lineup


@router.get("/device.xml")
def device_xml(request: Request):
    base = _base_url(request)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <specVersion>
    <major>1</major>
    <minor>0</minor>
  </specVersion>
  <URLBase>{base}</URLBase>
  <device>
    <deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType>
    <friendlyName>Manifold</friendlyName>
    <manufacturer>Manifold</manufacturer>
    <modelName>HDTC-2US</modelName>
    <modelNumber>HDTC-2US</modelNumber>
    <serialNumber>{DEVICE_ID}</serialNumber>
    <UDN>uuid:{DEVICE_UUID}</UDN>
  </device>
</root>"""
    return Response(content=xml, media_type="application/xml")
