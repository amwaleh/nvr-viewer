"""System status, network scanning, and SD card access."""
import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Query

from ..state import (db, scanner, active_streams, detection_settings,
                     RECORDINGS_DIR)
from ...network.sdcard import SDCardAccess

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])


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
    results = scanner.scan(subnet)
    return results


# --- SD Card ---

@router.get("/sdcard/{camera_id}")
async def list_sdcard_files(camera_id: int):
    """List files on the camera's SD card."""
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    sd = SDCardAccess(cam["host"],
                      username=cam.get("username", "admin"),
                      password=cam.get("password", ""))
    try:
        files = sd.list_files()
        return {"camera": cam["name"], "host": cam["host"], "files": files}
    except Exception as e:
        raise HTTPException(500, f"SD card access failed: {e}")


@router.post("/sdcard/{camera_id}/download")
async def download_sdcard_file(camera_id: int,
                               remote_path: str = Query(...)):
    """Download a file from the camera's SD card."""
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    sd = SDCardAccess(cam["host"],
                      username=cam.get("username", "admin"),
                      password=cam.get("password", ""))
    try:
        local_name = remote_path.replace("/", "_").replace("\\", "_")
        local_path = str(RECORDINGS_DIR / f"sdcard_{cam['host']}_{local_name}")
        sd.download_file(remote_path, local_path)
        return {"message": "File downloaded", "local_path": local_path}
    except Exception as e:
        raise HTTPException(500, f"Download failed: {e}")
