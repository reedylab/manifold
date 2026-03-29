"""SQLAlchemy engine and session management."""

import logging
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

_engine = None
_SessionFactory = None


def init_engine():
    """Create the global engine from config."""
    global _engine, _SessionFactory
    from manifold.config import Config

    cfg = Config()
    _engine = create_engine(
        cfg.database_url,
        pool_recycle=3600,
        pool_pre_ping=True,
    )
    _SessionFactory = sessionmaker(bind=_engine)
    logger.info("Database engine initialized: %s:%s/%s", cfg.PG_HOST, cfg.PG_PORT, cfg.PG_DB)


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


@contextmanager
def get_session():
    """Yield a SQLAlchemy session with auto commit/rollback."""
    if _SessionFactory is None:
        init_engine()
    session = _SessionFactory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
