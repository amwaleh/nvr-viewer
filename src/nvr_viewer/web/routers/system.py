"""System status, network scanning, and SD card access."""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..state import (db, creds, scanner, active_streams, detection_settings,
                     RECORDINGS_DIR)
from ...network.sdcard import SDCardAccess

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])


def _get_sd_access(cam: dict) -> SDCardAccess:
    """Create SDCardAccess with credentials from the encrypted store."""
    stored_cred = creds.get(cam["host"])
    return SDCardAccess(
        cam["host"],
        username=stored_cred["username"] if stored_cred else "admin",
        password=stored_cred["password"] if stored_cred else "",
    )


def _find_camera(camera_id: int) -> dict:
    """Lookup camera by ID or raise 404."""
    for c in db.get_cameras():
        if c["id"] == camera_id:
            return c
    raise HTTPException(404, "Camera not found")


@router.get("/status")
async def system_status():
    """Get system status overview."""
    cameras = db.get_cameras()
    streams = {}
    for key, info in active_streams.items():
        streams[key] = {
            "status": info["status"],
            "frame_count": info.get("frame_count", 0),
            "recording": (info.get("recorder") is not None and
                          info["recorder"].recording
                          if info.get("recorder") else False),
            "last_detections": info.get("last_detections", []),
        }

    rec_count = (len(list(RECORDINGS_DIR.glob("*.mp4")))
                 if RECORDINGS_DIR.exists() else 0)

    return {
        "cameras_registered": len(cameras),
        "streams_active": sum(1 for s in streams.values()
                              if s["status"] == "streaming"),
        "recordings_count": rec_count,
        "streams": streams,
        "detection": detection_settings,
    }


@router.get("/scan")
async def scan_network(subnet: Optional[str] = None):
    """Scan the network for cameras."""
    cameras = scanner.discover_cameras(subnet)
    for cam in cameras:
        cam["has_credentials"] = creds.get(cam["host"]) is not None
    return cameras


# --- SD Card ---

@router.get("/sdcard/{camera_id}")
async def list_sdcard_files(camera_id: int):
    """List files on the camera's SD card."""
    cam = _find_camera(camera_id)
    sd = _get_sd_access(cam)
    try:
        files = sd.list_files()
        if not files:
            return {
                "camera": cam["name"], "host": cam["host"], "files": [],
                "message": "SD card listing not available for this camera. "
                           "Yoosee cameras use a proprietary protocol for SD card access. "
                           "Use the Record feature to save streams locally instead.",
                "supported": False,
            }
        return {"camera": cam["name"], "host": cam["host"], "files": files, "supported": True}
    except Exception:
        logger.exception("SD card list failed for camera %s", camera_id)
        raise HTTPException(500, "SD card access failed")


@router.post("/sdcard/{camera_id}/download")
async def download_sdcard_file(camera_id: int,
                               remote_path: str = Query(...)):
    """Download a file from the camera's SD card."""
    cam = _find_camera(camera_id)
    sd = _get_sd_access(cam)
    try:
        local_name = remote_path.replace("/", "_").replace("\\", "_")
        local_path = str(RECORDINGS_DIR / f"sdcard_{cam['host']}_{local_name}")
        sd.download_file(remote_path, local_path)
        return {"message": "File downloaded", "local_path": local_path}
    except Exception:
        logger.exception("SD card download failed for camera %s", camera_id)
        raise HTTPException(500, "Download failed")
