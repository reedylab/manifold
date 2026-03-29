"""Programs model — maps to existing programs table."""

from datetime import datetime

from sqlalchemy import Column, String, Integer, Boolean, Text, DateTime
from sqlalchemy.dialects.postgresql import JSONB

from manifold.models.base import Base


class Program(Base):
    __tablename__ = "programs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String, nullable=False, unique=True, index=True)
    tvdb_id = Column(Integer, unique=True, nullable=True)
    imdb_id = Column(String(20), unique=True, nullable=True)
    tmdb_id = Column(Integer, unique=True, nullable=True)
    is_refresh = Column(Boolean, default=False, nullable=False)
    channels = Column(JSONB, default=list, nullable=False)
    extra_keywords = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
