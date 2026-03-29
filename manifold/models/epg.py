"""EPG model — maps to existing epg table."""

import uuid
from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime, UniqueConstraint, Index, ForeignKey

from manifold.models.base import Base


class Epg(Base):
    __tablename__ = "epg"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    channel_id = Column(String, nullable=False, index=True)
    channel_name = Column(String, nullable=False)
    epg_source_id = Column(String, ForeignKey("epg_sources.id", ondelete="CASCADE"))
    icon_url = Column(Text)
    epg_data = Column(Text)
    last_updated = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("channel_id", "epg_source_id", name="uq_epg_channel_source"),
        Index("ix_epg_channel_name", "channel_name"),
    )
