"""Detection settings — default toggles and per-camera overrides."""
import logging
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from ..state import (detection_settings, camera_detection_settings,
                     continuous_recording_settings, save_settings,
                     active_streams, db, RECORDINGS_DIR)
from ...core.recorder import Recorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["detection"])


class DetectionToggle(BaseModel):
    motion: Optional[bool] = None
    objects: Optional[bool] = None
    faces: Optional[bool] = None


@router.get("/detection")
async def get_detection_settings():
    """Get default detection settings and all per-camera overrides."""
    return {
        "default": detection_settings,
        "cameras": camera_detection_settings,
    }


@router.post("/detection")
async def set_detection_settings(toggle: DetectionToggle):
    """Update default detection settings."""
    if toggle.motion is not None:
        detection_settings["motion"] = toggle.motion
    if toggle.objects is not None:
        detection_settings["objects"] = toggle.objects
    if toggle.faces is not None:
        detection_settings["faces"] = toggle.faces
    save_settings()
    logger.info("Default detection settings updated: %s", detection_settings)
    return {"message": "Detection settings updated",
            "default": detection_settings,
            "cameras": camera_detection_settings}


@router.get("/detection/{camera_id}")
async def get_camera_detection(camera_id: int):
    """Get effective detection settings for a specific camera."""
    cam_key = str(camera_id)
    cam_settings = camera_detection_settings.get(cam_key, {})
    effective = {
        "motion": cam_settings.get("motion", detection_settings.get("motion", True)),
        "objects": cam_settings.get("objects", detection_settings.get("objects", True)),
        "faces": cam_settings.get("faces", detection_settings.get("faces", True)),
    }
    return {"camera_id": camera_id, "settings": effective,
            "is_custom": cam_key in camera_detection_settings}


@router.post("/detection/{camera_id}")
async def set_camera_detection(camera_id: int, toggle: DetectionToggle):
    """Set per-camera detection overrides."""
    cam_key = str(camera_id)
    if cam_key not in camera_detection_settings:
        camera_detection_settings[cam_key] = {}
    if toggle.motion is not None:
        camera_detection_settings[cam_key]["motion"] = toggle.motion
    if toggle.objects is not None:
        camera_detection_settings[cam_key]["objects"] = toggle.objects
    if toggle.faces is not None:
        camera_detection_settings[cam_key]["faces"] = toggle.faces
    save_settings()
    logger.info("Camera %d detection: %s", camera_id, camera_detection_settings[cam_key])
    return {"message": f"Camera {camera_id} detection updated",
            "settings": camera_detection_settings[cam_key]}


@router.delete("/detection/{camera_id}")
async def reset_camera_detection(camera_id: int):
    """Remove per-camera overrides, revert to defaults."""
    cam_key = str(camera_id)
    camera_detection_settings.pop(cam_key, None)
    save_settings()
    return {"message": f"Camera {camera_id} reverted to default detection settings"}


# --- Continuous Recording ---

@router.get("/continuous-recording")
async def get_continuous_recording():
    """Get continuous recording settings for all cameras."""
    return continuous_recording_settings


class ContinuousRecordingToggle(BaseModel):
    enabled: bool


@router.post("/continuous-recording/{camera_id}")
async def set_continuous_recording(camera_id: int, toggle: ContinuousRecordingToggle):
    """Enable/disable continuous recording for a camera."""
    cam_key = str(camera_id)
    continuous_recording_settings[cam_key] = toggle.enabled
    save_settings()
    logger.info("Camera %d continuous recording: %s", camera_id, toggle.enabled)

    # If camera is currently streaming, start/stop recording immediately
    stream = active_streams.get(cam_key)
    if stream and stream.get("status") == "streaming":
        rec = stream.get("recorder")
        if toggle.enabled and (not rec or not rec.recording):
            # Start continuous recording now
            cam_name = stream.get("_name", f"cam_{camera_id}")
            from ..streaming import CONTINUOUS_SEGMENT_SECS
            recorder = Recorder(cam_name, output_dir=RECORDINGS_DIR,
                                max_duration=CONTINUOUS_SEGMENT_SECS)
            path = recorder.start()
            stream["recorder"] = recorder
            stream["_continuous"] = True
            stream["_recording_id"] = db.log_recording(camera_id, path, "continuous")
            logger.info("Continuous recording started on toggle: camera=%s", cam_name)
        elif not toggle.enabled and rec and rec.recording and stream.get("_continuous"):
            # Stop continuous recording
            from pathlib import Path as P
            info = rec.stop()
            rec_id = stream.get("_recording_id")
            if rec_id and info.get("file_path"):
                fsize = P(info["file_path"]).stat().st_size if P(info["file_path"]).exists() else 0
                db.end_recording(rec_id, fsize)
            stream["recorder"] = None
            stream["_continuous"] = False
            logger.info("Continuous recording stopped on toggle: camera=%d", camera_id)

    return {"message": f"Camera {camera_id} continuous recording {'enabled' if toggle.enabled else 'disabled'}",
            "camera_id": camera_id, "enabled": toggle.enabled}
