"""FastAPI application with lifespan startup."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

_web_dir = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──
    from manifold.logging_setup import setup_logging
    from manifold.database import init_engine, get_engine
    from manifold.models import Base
    from manifold.scheduler import start_scheduler
    from manifold.services.bump_manager import BumpManager
    from manifold.services.filler_loop import start_filler_loop
    from sqlalchemy import text, inspect

    setup_logging()
    init_engine()

    # Schema migrations (from original create_app)
    engine = get_engine()
    insp = inspect(engine)

    with engine.connect() as conn:
        tables = insp.get_table_names()

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

        if "m3u_sources" in tables:
            m3u_cols = [c["name"] for c in insp.get_columns("m3u_sources")]
            if "stream_mode" not in m3u_cols:
                conn.execute(text("ALTER TABLE m3u_sources ADD COLUMN stream_mode VARCHAR DEFAULT 'passthrough'"))
                conn.commit()
            if "auto_activate" not in m3u_cols:
                conn.execute(text("ALTER TABLE m3u_sources ADD COLUMN auto_activate BOOLEAN DEFAULT FALSE NOT NULL"))
                conn.commit()

        if "epg" in tables:
            epg_cols = [c["name"] for c in insp.get_columns("epg")]
            if "epg_source_id" not in epg_cols or "icon_url" not in epg_cols:
                conn.execute(text("DROP TABLE epg CASCADE"))
                conn.commit()

    Base.metadata.create_all(engine)

    start_scheduler(None)
    BumpManager.scan()
    start_filler_loop()

    yield
    # ── Shutdown ──


app = FastAPI(title="Manifold", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_web_dir / "static")), name="static")
templates = Jinja2Templates(directory=str(_web_dir / "templates"))


@app.get("/health")
def health():
    return {"status": "ok"}

# Include routers
from manifold.web.routers import ui, channels, sources, output, hdhr, stream, logo, guide, media, system, integrations

app.include_router(ui.router)
app.include_router(channels.router, prefix="/api")
app.include_router(sources.router, prefix="/api")
app.include_router(output.router, prefix="/output")
app.include_router(hdhr.router)
app.include_router(stream.router, prefix="/stream")
app.include_router(logo.router, prefix="/logo")
app.include_router(guide.router, prefix="/api")
app.include_router(media.router, prefix="/api")
app.include_router(system.router, prefix="/api")
app.include_router(integrations.router, prefix="/api")
