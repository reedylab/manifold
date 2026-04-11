"""Configuration from environment variables and DB settings table."""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    PG_HOST = os.getenv("PG_HOST", "localhost")
    PG_PORT = os.getenv("PG_PORT", "5432")
    PG_USER = os.getenv("PG_USER", "user")
    PG_PASS = os.getenv("PG_PASS", "pass")
    PG_DB = os.getenv("PG_DB", "db")
    BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
    BRIDGE_PORT = os.getenv("BRIDGE_PORT", "8080")
    OUTPUT_DIR = os.getenv("OUTPUT_DIR", "/app/output")
    LOG_DIR = os.getenv("LOG_DIR", "/app/logs")
    DATA_DIR = os.getenv("DATA_DIR", "/app/data")
    LOGO_DIR = os.getenv("LOGO_DIR", "/app/logos")
    STREAM_DIR = os.getenv("STREAM_DIR", "/app/streams")
    MANIFOLD_HOST = os.getenv("MANIFOLD_HOST", "127.0.0.1")
    MANIFOLD_PORT = os.getenv("MANIFOLD_PORT", "40000")
    VPN_HTTP_PROXY = os.getenv("VPN_HTTP_PROXY", "")
    GLUETUN_CONTROL_URL = os.getenv("GLUETUN_CONTROL_URL", "")
    GLUETUN_CONTROL_USER = os.getenv("GLUETUN_CONTROL_USER", "")
    GLUETUN_CONTROL_PASS = os.getenv("GLUETUN_CONTROL_PASS", "")

    PROGRAM_IMAGE_DIR = os.path.join(os.getenv("DATA_DIR", "/app/data"), "program_images")

    @property
    def database_url(self):
        return f"postgresql+psycopg2://{self.PG_USER}:{self.PG_PASS}@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DB}"

    @property
    def bridge_base(self):
        return f"http://{self.BRIDGE_HOST}:{self.BRIDGE_PORT}"


def get_setting(key, default=None):
    """Read a setting from the DB settings table."""
    from manifold.database import get_session
    from manifold.models.settings import Settings

    with get_session() as session:
        row = session.query(Settings).filter_by(key=key).first()
        return row.value if row else default


def set_setting(key, value):
    """Write a setting to the DB settings table."""
    from manifold.database import get_session
    from manifold.models.settings import Settings

    with get_session() as session:
        row = session.query(Settings).filter_by(key=key).first()
        if row:
            row.value = value
        else:
            session.add(Settings(key=key, value=value))
