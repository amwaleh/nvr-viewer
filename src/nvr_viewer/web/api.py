"""FastAPI backend for NVR Viewer web interface.

Thin orchestrator: sets up the app, CORS, static files, frontend pages,
and includes all API routers. Business logic lives in routers/ and streaming.py.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .routers import cameras, recordings, events, detection, settings, system, notifications, service

logger = logging.getLogger(__name__)

app = FastAPI(title="NVR Viewer API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Frontend pages ---

STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"


@app.get("/")
async def index():
    """Serve the main frontend page."""
    index_path = TEMPLATES_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path, media_type="text/html")
    return {"message": "NVR Viewer API", "docs": "/docs"}


@app.get("/events")
async def events_page():
    """Serve the events gallery page."""
    events_path = TEMPLATES_DIR / "events.html"
    if events_path.exists():
        return FileResponse(events_path, media_type="text/html")
    raise HTTPException(404, "Events page not found")


@app.get("/settings")
async def settings_page():
    """Serve the settings configuration page."""
    settings_path = TEMPLATES_DIR / "settings.html"
    if settings_path.exists():
        return FileResponse(settings_path, media_type="text/html")
    raise HTTPException(404, "Settings page not found")


# --- Static files ---
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Include API routers ---
app.include_router(cameras.router)
app.include_router(recordings.router)
app.include_router(events.router)
app.include_router(detection.router)
app.include_router(settings.router)
app.include_router(system.router)
app.include_router(notifications.router)
app.include_router(service.router)
