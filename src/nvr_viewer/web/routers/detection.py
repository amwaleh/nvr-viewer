"""Detection settings — default toggles and per-camera overrides."""
import logging
from typing import Optional
from fastapi import APIRouter
from pydantic import BaseModel

from ..state import (detection_settings, camera_detection_settings,
                     save_settings)

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
    logger.info(f"Default detection settings updated: {detection_settings}")
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
    logger.info(f"Camera {camera_id} detection: {camera_detection_settings[cam_key]}")
    return {"message": f"Camera {camera_id} detection updated",
            "settings": camera_detection_settings[cam_key]}


@router.delete("/detection/{camera_id}")
async def reset_camera_detection(camera_id: int):
    """Remove per-camera overrides, revert to defaults."""
    cam_key = str(camera_id)
    camera_detection_settings.pop(cam_key, None)
    save_settings()
    return {"message": f"Camera {camera_id} reverted to default detection settings"}
