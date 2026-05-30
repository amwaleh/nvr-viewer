"""Camera CRUD, streaming, snapshots, and recording control."""
import asyncio
import cv2
import logging
import numpy as np
from io import BytesIO
from typing import Optional
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel

from ..state import db, creds, active_streams, RECORDINGS_DIR
from ..streaming import start_stream, stop_stream
from ...core.rtsp_client import CameraConfig
from ...core.recorder import Recorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["cameras"])


# --- Models ---

class CameraAdd(BaseModel):
    name: str
    host: str
    port: int = 554
    path: str = "/onvif1"
    username: str = "admin"
    password: str = ""
    type: str = "rtsp"
    stream_url: str = ""


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    path: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    type: Optional[str] = None
    stream_url: Optional[str] = None


# --- CRUD ---

@router.get("/cameras")
async def list_cameras():
    cameras = db.get_cameras()
    for cam in cameras:
        key = str(cam["id"])
        info = active_streams.get(key, {})
        cam["status"] = info.get("status", "disconnected")
        cam["frame_count"] = info.get("frame_count", 0)
    return cameras


@router.post("/cameras")
async def add_camera(cam: CameraAdd):
    cam_id = db.add_camera(
        name=cam.name, host=cam.host, port=cam.port,
        path=cam.path, camera_type=cam.type, stream_url=cam.stream_url)
    if cam.password:
        creds.set(cam.host, cam.username, cam.password)
    return {"id": cam_id, "message": f"Camera '{cam.name}' added"}


@router.delete("/cameras/{camera_id}")
async def remove_camera(camera_id: int):
    key = str(camera_id)
    stop_stream(key)
    db.delete_camera(camera_id)
    return {"message": "Camera deleted", "id": camera_id}


@router.put("/cameras/{camera_id}")
async def update_camera(camera_id: int, cam: CameraUpdate):
    existing = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            existing = c
            break
    if not existing:
        raise HTTPException(404, "Camera not found")

    # Stop stream if connection details changed
    if cam.host is not None or cam.port is not None or cam.path is not None:
        stop_stream(str(camera_id))

    # Only pass DB-safe fields (no credentials)
    db_fields = {}
    for field in ["name", "host", "port", "path"]:
        val = getattr(cam, field, None)
        if val is not None:
            db_fields[field] = val

    if db_fields:
        db.update_camera(camera_id, **db_fields)

    # Update credentials via encrypted store (never in DB)
    if cam.password:
        host = cam.host or existing["host"]
        creds.set(host, cam.username or "admin", cam.password)

    return {"message": "Camera updated", "id": camera_id}


# --- Streaming ---

@router.get("/stream/{camera_id}")
async def stream_camera(camera_id: int):
    key = str(camera_id)
    info = active_streams.get(key)
    if not info or info["status"] != "streaming":
        raise HTTPException(400, "Camera not streaming")

    cam_type = info.get("config")
    is_mjpeg = info.get("_host", "") and not cam_type

    async def mjpeg_generator():
        while True:
            jpeg_bytes = info.get("latest_jpeg")
            if jpeg_bytes:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n"
                       + jpeg_bytes + b"\r\n")
            await asyncio.sleep(0.033)

    async def rtsp_generator():
        while True:
            frame = info.get("latest_frame")
            if frame is not None:
                _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n"
                       + jpeg.tobytes() + b"\r\n")
            await asyncio.sleep(0.033)

    if info.get("latest_jpeg") is not None:
        gen = mjpeg_generator()
    else:
        gen = rtsp_generator()

    return StreamingResponse(gen, media_type="multipart/x-mixed-replace; boundary=frame")


@router.post("/stream/{camera_id}/start")
async def start_camera_stream(camera_id: int):
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    key = str(camera_id)
    cam_type = cam.get("type", "rtsp")

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

    return {"message": f"Stream started for camera {camera_id}"}


@router.post("/stream/{camera_id}/stop")
async def stop_camera_stream(camera_id: int):
    key = str(camera_id)
    stop_stream(key)
    return {"message": f"Stream stopped for camera {camera_id}"}


@router.get("/snapshot/{camera_id}")
async def take_snapshot(camera_id: int):
    key = str(camera_id)
    info = active_streams.get(key)
    if not info or info["status"] != "streaming":
        raise HTTPException(400, "Camera not streaming")

    frame = info.get("latest_frame")
    if frame is None:
        raise HTTPException(400, "No frame available yet")

    _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return Response(content=jpeg.tobytes(), media_type="image/jpeg",
                    headers={"Content-Disposition": f"attachment; filename=snapshot_{camera_id}.jpg"})


# --- Recording control ---

@router.post("/record/{camera_id}/start")
async def start_recording(camera_id: int):
    key = str(camera_id)
    stream = active_streams.get(key)
    if not stream or stream["status"] != "streaming":
        raise HTTPException(400, "Camera not streaming")

    if stream.get("recorder") and stream["recorder"].recording:
        return {"message": "Already recording", "path": stream["recorder"].file_path}

    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break

    recorder = Recorder(cam["name"] if cam else f"cam_{camera_id}",
                        output_dir=RECORDINGS_DIR)
    path = recorder.start()
    stream["recorder"] = recorder

    if cam:
        db.log_recording(cam["id"], path, "manual")

    return {"message": "Recording started", "path": path}


@router.post("/record/{camera_id}/stop")
async def stop_recording(camera_id: int):
    key = str(camera_id)
    stream = active_streams.get(key)
    if not stream:
        raise HTTPException(404, "No active stream")

    rec = stream.get("recorder")
    if not rec or not rec.recording:
        return {"message": "Not recording"}

    info = rec.stop()
    stream["recorder"] = None
    return {"message": "Recording stopped", **info}
