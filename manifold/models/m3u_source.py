"""Model for M3U playlist sources."""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, DateTime, Text, Integer, Boolean

from manifold.models.base import Base


class M3uSource(Base):
    __tablename__ = "m3u_sources"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, nullable=False)
    url = Column(Text, nullable=False, unique=True)
    stream_mode = Column(String, default="passthrough")
    # When true, newly-ingested channels from this source are created active.
    # Existing channels are untouched so manual deactivations stick.
    auto_activate = Column(Boolean, default=False, nullable=False)
    channel_count = Column(Integer, default=0)
    last_ingested_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
