"""Models for manifests, captures, header_profiles, variants, segments."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey, Enum,
    UniqueConstraint, Index,
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB

from manifold.models.base import Base


class HeaderProfile(Base):
    __tablename__ = "header_profiles"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String, unique=True, nullable=False)
    headers = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    manifests = relationship("Manifest", back_populates="header_profile")


class Capture(Base):
    __tablename__ = "captures"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    page_url = Column(Text, nullable=False)
    user_agent = Column(Text)
    context = Column(JSONB, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    manifests = relationship("Manifest", back_populates="capture")


class Manifest(Base):
    __tablename__ = "manifests"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    capture_id = Column(String, ForeignKey("captures.id", ondelete="SET NULL"))
    m3u_source_id = Column(String, ForeignKey("m3u_sources.id", ondelete="SET NULL"))
    header_profile_id = Column(String, ForeignKey("header_profiles.id", ondelete="SET NULL"))
    url = Column(Text, nullable=False)
    url_hash = Column(String(32), nullable=False)
    source_domain = Column(String, index=True)
    mime = Column(String)
    kind = Column(Enum("master", "media", name="manifest_kind"), nullable=False)
    headers = Column(JSONB, default=dict)
    requires_headers = Column(Boolean, default=False, nullable=False)
    body = Column(Text)
    sha256 = Column(String(64))
    drm_method = Column(String)
    is_drm = Column(Boolean, default=False, nullable=False)
    title = Column(String)
    tvg_id = Column(String)
    tvg_logo = Column(Text)
    logo_cached = Column(Boolean, default=False)
    stream_mode = Column(String, default="passthrough")
    channel_number = Column(Integer, nullable=True)    # User-assigned channel number
    title_override = Column(String, nullable=True)     # Display name override (original title preserved)
    stale_since = Column(DateTime(timezone=True), nullable=True)  # When channel disappeared from M3U source
    expires_at = Column(DateTime(timezone=True), nullable=True)   # Parsed CDN token expiry for resolved channels
    last_refreshed_at = Column(DateTime(timezone=True), nullable=True)  # Last predictive refresh timestamp
    last_accessed_at = Column(DateTime(timezone=True), nullable=True)   # Last client playlist request — used for demand-driven refresh
    tags = Column(JSONB, default=list)
    event_end_at = Column(DateTime(timezone=True))
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    capture = relationship("Capture", back_populates="manifests")
    header_profile = relationship("HeaderProfile", back_populates="manifests")
    variants = relationship("Variant", back_populates="manifest", cascade="all, delete-orphan")
    segments = relationship("Segment", back_populates="manifest", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint("url_hash", "sha256", name="uq_manifest_urlhash_bodyhash"),
        Index(
            "uq_manifests_title_active",
            "title",
            unique=True,
            postgresql_where=(active == True),
        ),
        Index("ix_manifests_event_end", event_end_at),
        Index("ix_manifests_created_desc", created_at.desc()),
    )


class Variant(Base):
    __tablename__ = "variants"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    manifest_id = Column(String, ForeignKey("manifests.id", ondelete="CASCADE"), index=True)
    uri = Column(Text, nullable=False)
    abs_url = Column(Text, nullable=False)
    bandwidth = Column(Integer)
    resolution = Column(String)
    frame_rate = Column(Float)
    codecs = Column(String)
    audio_group = Column(String)
    width = Column(Integer)
    height = Column(Integer)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    manifest = relationship("Manifest", back_populates="variants")

    __table_args__ = (
        Index("ix_variants_bw_desc", bandwidth.desc()),
        Index("ix_variants_res", width.desc(), height.desc()),
    )


class Segment(Base):
    __tablename__ = "segments"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    manifest_id = Column(String, ForeignKey("manifests.id", ondelete="CASCADE"), index=True)
    seq_no = Column(Integer)
    uri = Column(Text, nullable=False)
    abs_url = Column(Text, nullable=False)
    duration_s = Column(Float)
    key_method = Column(String)
    key_uri = Column(Text)
    byte_range = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    manifest = relationship("Manifest", back_populates="segments")
