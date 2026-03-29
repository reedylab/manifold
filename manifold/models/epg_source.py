"""Model for EPG (XMLTV) sources, linked to an M3U source."""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Text, Integer, ForeignKey

from manifold.models.base import Base


class EpgSource(Base):
    __tablename__ = "epg_sources"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    url = Column(Text, nullable=False)
    m3u_source_id = Column(String, ForeignKey("m3u_sources.id", ondelete="CASCADE"), nullable=False)
    channel_count = Column(Integer, default=0)
    last_ingested_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
