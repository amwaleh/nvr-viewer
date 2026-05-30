"""FastAPI backend for NVR Viewer web interface.

Thin orchestrator: sets up the app, CORS, static files, frontend pages,
and includes all API routers. Business logic lives in routers/ and streaming.py.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

from .routers import cameras, recordings, events, detection, settings, system, notifications, service, timeline
from .state import db, creds, continuous_recording_settings, active_streams
from .streaming import start_stream
from ..core.rtsp_client import CameraConfig

logger = logging.getLogger(__name__)


def _auto_start_continuous_cameras():
    """Start streams for all cameras with continuous recording enabled."""
    enabled_ids = [k for k, v in continuous_recording_settings.items() if v]
    if not enabled_ids:
        return

    all_cameras = db.get_cameras()
    cam_map = {str(c["id"]): c for c in all_cameras}

    for cam_id_str in enabled_ids:
        cam = cam_map.get(cam_id_str)
        if not cam:
            logger.warning("Continuous recording enabled for unknown camera %s", cam_id_str)
            continue

        key = cam_id_str
        if key in active_streams and active_streams[key].get("status") == "streaming":
            continue  # Already running

        cam_type = cam.get("type", "rtsp")
        try:
            if cam_type == "mjpeg":
                stream_url = cam.get("stream_url", "")
                if not stream_url:
                    stream_url = f"http://{cam['host']}:{cam['port']}/0/stream"
                start_stream(key, camera_type="mjpeg", stream_url=stream_url,
                             camera_name=cam["name"], camera_host=cam["host"])
            else:
                stored_cred = creds.get(cam["host"])
                config = CameraConfig(
                    host=cam["host"], port=cam["port"],
                    path=cam.get("path", "/onvif1"),
                    username=stored_cred["username"] if stored_cred else "admin",
                    password=stored_cred["password"] if stored_cred else "",
                    name=cam["name"])
                start_stream(key, config=config)

            logger.info("Auto-started continuous recording stream: %s (%s)",
                        cam["name"], cam["host"])
        except Exception as e:
            logger.error("Failed to auto-start camera %s: %s", cam["name"], e)


@asynccontextmanager
async def lifespan(app):
    """Startup/shutdown lifecycle for the app."""
    _auto_start_continuous_cameras()
    yield


app = FastAPI(title="NVR Viewer API", version="0.2.0", lifespan=lifespan)

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


@app.get("/timeline")
async def timeline_page():
    """Serve the timeline playback page."""
    timeline_path = TEMPLATES_DIR / "timeline.html"
    if timeline_path.exists():
        return FileResponse(timeline_path, media_type="text/html")
    raise HTTPException(404, "Timeline page not found")


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
app.include_router(timeline.router)
