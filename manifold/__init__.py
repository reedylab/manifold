"""Manifold — M3U + XMLTV pipeline."""

from manifold.config import Config


def create_app():
    """Flask application factory."""
    from flask import Flask
    from manifold.logging_setup import setup_logging
    from manifold.database import init_engine
    from manifold.scheduler import start_scheduler

    setup_logging()

    app = Flask(
        __name__,
        template_folder="web/templates",
        static_folder="web/static",
    )
    app.config["SETTINGS"] = Config()

    init_engine()

    # Migrate / create tables
    from manifold.database import get_engine
    from manifold.models import Base
    from sqlalchemy import text, inspect

    engine = get_engine()
    insp = inspect(engine)

    # Migration: add new columns and tables for EPG source linking
    with engine.connect() as conn:
        tables = insp.get_table_names()

        # Add columns to manifests if missing
        if "manifests" in tables:
            manifest_cols = [c["name"] for c in insp.get_columns("manifests")]
            for col, coltype in [
                ("tvg_id", "VARCHAR"),
                ("m3u_source_id", "VARCHAR"),
                ("tvg_logo", "TEXT"),
                ("logo_cached", "BOOLEAN DEFAULT FALSE"),
                ("stream_mode", "VARCHAR DEFAULT 'passthrough'"),
                ("channel_number", "INTEGER"),
                ("title_override", "VARCHAR"),
                ("stale_since", "TIMESTAMPTZ"),
            ]:
                if col not in manifest_cols:
                    conn.execute(text(f"ALTER TABLE manifests ADD COLUMN {col} {coltype}"))
            conn.commit()

        # Add stream_mode to m3u_sources if missing
        if "m3u_sources" in tables:
            m3u_cols = [c["name"] for c in insp.get_columns("m3u_sources")]
            if "stream_mode" not in m3u_cols:
                conn.execute(text("ALTER TABLE m3u_sources ADD COLUMN stream_mode VARCHAR DEFAULT 'passthrough'"))
                conn.commit()

        # Recreate epg table if it lacks epg_source_id or icon_url
        if "epg" in tables:
            epg_cols = [c["name"] for c in insp.get_columns("epg")]
            if "epg_source_id" not in epg_cols or "icon_url" not in epg_cols:
                conn.execute(text("DROP TABLE epg CASCADE"))
                conn.commit()

    Base.metadata.create_all(engine)

    # Register blueprints
    from manifold.web.blueprints.ui import ui_bp
    from manifold.web.blueprints.api import api_bp
    from manifold.web.blueprints.output import output_bp
    from manifold.web.blueprints.hdhr import hdhr_bp
    from manifold.web.blueprints.logo import logo_bp
    from manifold.web.blueprints.stream import stream_bp

    app.register_blueprint(ui_bp)
    app.register_blueprint(api_bp, url_prefix="/api")
    app.register_blueprint(output_bp, url_prefix="/output")
    app.register_blueprint(hdhr_bp)
    app.register_blueprint(logo_bp, url_prefix="/logo")
    app.register_blueprint(stream_bp, url_prefix="/stream")

    start_scheduler(app)

    # Scan bumps and start filler loop
    from manifold.services.bump_manager import BumpManager
    from manifold.services.filler_loop import start_filler_loop
    BumpManager.scan()
    start_filler_loop()

    return app
