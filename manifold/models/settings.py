"""Settings KV table — new table created by Manifold."""

from datetime import datetime

from sqlalchemy import Column, String, Text, DateTime

from manifold.models.base import Base


class Settings(Base):
    __tablename__ = "manifold_settings"

    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
