"""FastAPI backend for NVR Viewer web interface."""
import asyncio
import cv2
import numpy as np
import logging
import time
import threading
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..core.rtsp_client import RTSPClient, CameraConfig
from ..core.decoder import H264Decoder
from ..core.recorder import Recorder
from ..detection.motion import MotionDetector
from ..detection.detector import ObjectDetector, FaceDetector
from ..detection.events import EventProcessor
from ..storage.database import Database
from ..storage.credentials import CredentialStore
from ..network.scanner import NetworkScanner
from ..network.sdcard import SDCardAccess

logger = logging.getLogger(__name__)

app = FastAPI(title="NVR Viewer API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend files
STATIC_DIR = Path(__file__).parent / "static"
TEMPLATES_DIR = Path(__file__).parent / "templates"

# Global state
db = Database()
creds = CredentialStore()
scanner = NetworkScanner()

# Detection engines (shared across streams)
motion_detector_cache: dict[str, MotionDetector] = {}
object_detector: Optional[ObjectDetector] = None
face_detector: Optional[FaceDetector] = None
event_processor = EventProcessor(db=db)

# Detection settings
detection_settings = {
    "motion": True,
    "objects": False,  # Heavy — user must enable explicitly
    "faces": False,
}

# Active camera streams: {camera_id: {client, decoder, latest_frame, thread, stop_event}}
active_streams: dict[str, dict] = {}
stream_lock = threading.Lock()

# Recordings directory
RECORDINGS_DIR = Path.home() / ".nvr-viewer" / "recordings"
SNAPSHOTS_DIR = Path.home() / ".nvr-viewer" / "snapshots"


# --- Pydantic Models ---

class CameraAdd(BaseModel):
    name: str
    host: str
    port: int = 554
    path: str = "/onvif1"
    username: str = "admin"
    password: str = ""


class CredentialSet(BaseModel):
    host: str
    username: str = "admin"
    password: str = ""


# --- Camera Stream Management ---

def _stream_worker(camera_key: str, config: CameraConfig):
    """Background thread that connects to camera, decodes, and runs detection."""
    global object_detector, face_detector

    stream_info = active_streams.get(camera_key)
    if not stream_info:
        return

    client = RTSPClient(config)
    if not client.connect():
        logger.error(f"Stream connect failed: {camera_key}")
        stream_info["status"] = "error"
        return

    decoder = H264Decoder(client.sps_pps)
    stream_info["status"] = "streaming"
    stream_info["client"] = client
    stop_event = stream_info["stop_event"]

    # Per-camera motion detector (needs separate background model)
    if camera_key not in motion_detector_cache:
        motion_detector_cache[camera_key] = MotionDetector()
    motion_det = motion_detector_cache[camera_key]

    # Get camera DB id
    cam_db = db.get_camera_by_host(config.host)
    camera_id = cam_db["id"] if cam_db else 0
    frame_skip = 0  # Run detection every 3rd frame to save CPU

    def on_frame(nal_data: bytes, is_first: bool):
        nonlocal frame_skip
        frames = decoder.decode(nal_data)
        for frame in frames:
            stream_info["latest_frame"] = frame
            stream_info["frame_count"] = stream_info.get("frame_count", 0) + 1

            # Write to recorder if active
            rec = stream_info.get("recorder")
            if rec and rec.recording:
                rec.write_frame(frame)

            # Run detection every 3rd frame
            frame_skip += 1
            if frame_skip % 3 != 0:
                continue

            detections = []

            # Motion detection (lightweight, always on if enabled)
            if detection_settings.get("motion"):
                try:
                    motion_results = motion_det.detect(frame)
                    detections.extend(motion_results)
                except Exception as e:
                    logger.debug(f"Motion detect error: {e}")

            # Object detection (heavy, only if motion detected or always-on)
            if detection_settings.get("objects") and (detections or frame_skip % 15 == 0):
                try:
                    global object_detector
                    if object_detector is None:
                        object_detector = ObjectDetector()
                    obj_results = object_detector.detect(frame)
                    detections.extend(obj_results)
                except Exception as e:
                    logger.debug(f"Object detect error: {e}")

            # Face detection
            if detection_settings.get("faces") and (detections or frame_skip % 15 == 0):
                try:
                    global face_detector
                    if face_detector is None:
                        face_detector = FaceDetector()
                    face_results = face_detector.detect(frame)
                    detections.extend(face_results)
                except Exception as e:
                    logger.debug(f"Face detect error: {e}")

            # Process and log events (handles dedup, snapshots, DB)
            if detections:
                try:
                    new_events = event_processor.process(
                        camera_id, config.name, frame, detections)
                    if new_events:
                        stream_info["last_detections"] = new_events
                except Exception as e:
                    logger.debug(f"Event processing error: {e}")

    client.read_frames(on_frame, stop_event.is_set)
    decoder.close()
    stream_info["status"] = "disconnected"
    logger.info(f"Stream ended: {camera_key}")


def start_stream(camera_key: str, config: CameraConfig):
    """Start a camera stream if not already running."""
    with stream_lock:
        if camera_key in active_streams and active_streams[camera_key]["status"] == "streaming":
            return

        stop_event = threading.Event()
        active_streams[camera_key] = {
            "config": config,
            "client": None,
            "latest_frame": None,
            "frame_count": 0,
            "stop_event": stop_event,
            "status": "connecting",
            "recorder": None,
        }

        t = threading.Thread(target=_stream_worker, args=(camera_key, config), daemon=True)
        t.start()
        active_streams[camera_key]["thread"] = t


def stop_stream(camera_key: str):
    """Stop a camera stream."""
    with stream_lock:
        if camera_key in active_streams:
            active_streams[camera_key]["stop_event"].set()
            rec = active_streams[camera_key].get("recorder")
            if rec and rec.recording:
                rec.stop()
            del active_streams[camera_key]


# --- API Endpoints ---

# Frontend
@app.get("/")
async def index():
    """Serve the main frontend page."""
    index_path = TEMPLATES_DIR / "index.html"
    if index_path.exists():
        return FileResponse(index_path, media_type="text/html")
    return {"message": "NVR Viewer API", "docs": "/docs"}


# Mount static files after defining routes
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# Camera Management
@app.get("/api/cameras")
async def list_cameras():
    """List all registered cameras with stream status."""
    cameras = db.get_cameras()
    for cam in cameras:
        key = str(cam["id"])
        stream = active_streams.get(key, {})
        cam["stream_status"] = stream.get("status", "stopped")
        cam["frame_count"] = stream.get("frame_count", 0)
    return cameras


@app.post("/api/cameras")
async def add_camera(cam: CameraAdd):
    """Add a new camera."""
    camera_id = db.add_camera(cam.name, cam.host, cam.port, cam.path)
    if cam.password:
        creds.set(cam.host, cam.username, cam.password)
    return {"id": camera_id, "message": f"Camera '{cam.name}' added"}


@app.delete("/api/cameras/{camera_id}")
async def remove_camera(camera_id: int):
    """Remove a camera and stop its stream."""
    stop_stream(str(camera_id))
    # Note: actual DB delete not implemented to preserve history
    return {"message": f"Camera {camera_id} stopped"}


# Streaming
@app.get("/api/stream/{camera_id}")
async def stream_camera(camera_id: int):
    """MJPEG stream for a camera. Auto-starts the stream if needed."""
    cam = None
    cameras = db.get_cameras()
    for c in cameras:
        if c["id"] == camera_id:
            cam = c
            break

    if not cam:
        raise HTTPException(404, "Camera not found")

    key = str(camera_id)

    # Auto-start stream if not running
    if key not in active_streams or active_streams[key]["status"] != "streaming":
        stored_cred = creds.get(cam["host"])
        config = CameraConfig(
            host=cam["host"],
            port=cam["port"],
            path=cam["path"],
            username=stored_cred["username"] if stored_cred else "admin",
            password=stored_cred["password"] if stored_cred else "",
            name=cam["name"],
        )
        start_stream(key, config)
        # Wait briefly for connection
        for _ in range(50):  # 5 seconds max
            if active_streams.get(key, {}).get("latest_frame") is not None:
                break
            await asyncio.sleep(0.1)

    def mjpeg_generator():
        while True:
            stream = active_streams.get(key)
            if not stream or stream["status"] == "disconnected":
                break

            frame = stream.get("latest_frame")
            if frame is not None:
                _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n")

            time.sleep(0.066)  # ~15 fps

    return StreamingResponse(
        mjpeg_generator(),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )


@app.post("/api/stream/{camera_id}/start")
async def start_camera_stream(camera_id: int):
    """Start streaming from a camera."""
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    stored_cred = creds.get(cam["host"])
    config = CameraConfig(
        host=cam["host"],
        port=cam["port"],
        path=cam["path"],
        username=stored_cred["username"] if stored_cred else "admin",
        password=stored_cred["password"] if stored_cred else "",
        name=cam["name"],
    )
    start_stream(str(camera_id), config)
    return {"message": f"Stream started for camera {camera_id}"}


@app.post("/api/stream/{camera_id}/stop")
async def stop_camera_stream(camera_id: int):
    """Stop streaming from a camera."""
    stop_stream(str(camera_id))
    return {"message": f"Stream stopped for camera {camera_id}"}


# Snapshot
@app.get("/api/snapshot/{camera_id}")
async def get_snapshot(camera_id: int):
    """Get a single JPEG snapshot from a camera."""
    key = str(camera_id)
    stream = active_streams.get(key)
    if not stream or stream.get("latest_frame") is None:
        raise HTTPException(404, "No active stream or no frames yet")

    frame = stream["latest_frame"]
    _, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return StreamingResponse(
        iter([jpeg.tobytes()]),
        media_type="image/jpeg",
        headers={"Content-Disposition": f"inline; filename=snapshot_{camera_id}.jpg"}
    )


# Recording
@app.post("/api/record/{camera_id}/start")
async def start_recording(camera_id: int):
    """Start recording a camera stream."""
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

    recorder = Recorder(cam["name"] if cam else f"cam_{camera_id}")
    path = recorder.start()
    stream["recorder"] = recorder

    if cam:
        db.log_recording(cam["id"], path, "manual")

    return {"message": "Recording started", "path": path}


@app.post("/api/record/{camera_id}/stop")
async def stop_recording(camera_id: int):
    """Stop recording a camera stream."""
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


# Recordings (backed up files)
@app.get("/api/recordings")
async def list_recordings():
    """List all local recording files."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(RECORDINGS_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        files.append({
            "name": f.name,
            "path": str(f),
            "size": stat.st_size,
            "size_mb": round(stat.st_size / 1048576, 1),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
        })
    return files


@app.get("/api/recordings/{filename}")
async def download_recording(filename: str):
    """Download a recording file."""
    file_path = RECORDINGS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Recording not found")
    return FileResponse(file_path, media_type="video/mp4", filename=filename)


# SD Card files
@app.get("/api/sdcard/{camera_id}")
async def list_sdcard_files(camera_id: int):
    """List files on the camera's SD card."""
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    stored_cred = creds.get(cam["host"])
    sd = SDCardAccess(
        host=cam["host"],
        username=stored_cred["username"] if stored_cred else "admin",
        password=stored_cred["password"] if stored_cred else "",
    )
    files = sd.list_files()

    # If no SD card files found, return helpful message
    if not files:
        return {
            "camera": cam["name"],
            "host": cam["host"],
            "files": [],
            "message": "SD card listing not available for this camera. "
                       "Yoosee cameras use a proprietary protocol for SD card access. "
                       "Use the Record feature to save streams locally instead.",
            "supported": False,
        }

    return {"camera": cam["name"], "host": cam["host"], "files": files, "supported": True}


@app.post("/api/sdcard/{camera_id}/download")
async def download_sdcard_file(camera_id: int, remote_path: str = Query(...)):
    """Download a file from camera SD card to local recordings."""
    cam = None
    for c in db.get_cameras():
        if c["id"] == camera_id:
            cam = c
            break
    if not cam:
        raise HTTPException(404, "Camera not found")

    stored_cred = creds.get(cam["host"])
    sd = SDCardAccess(
        host=cam["host"],
        username=stored_cred["username"] if stored_cred else "admin",
        password=stored_cred["password"] if stored_cred else "",
    )

    local_name = remote_path.replace("/", "_").lstrip("_")
    local_path = str(RECORDINGS_DIR / f"sdcard_{cam['host']}_{local_name}")

    success = sd.download_file(remote_path, local_path)
    if not success:
        raise HTTPException(500, "Download failed")

    return {"message": "Downloaded", "local_path": local_path}


# Network scanning
@app.get("/api/scan")
async def scan_network(subnet: Optional[str] = None):
    """Scan network for cameras."""
    cameras = scanner.discover_cameras(subnet)
    # Annotate with stored credentials
    for cam in cameras:
        cam["has_credentials"] = creds.get(cam["host"]) is not None
    return cameras


# Detection events
@app.get("/api/events")
async def list_events(
    camera_id: Optional[int] = None,
    detection_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = 100,
):
    """Query detection events."""
    events = db.get_events(camera_id=camera_id, detection_type=detection_type,
                           since=since, limit=limit)
    return events


# Credentials
@app.get("/api/credentials")
async def list_credentials():
    """List hosts with stored credentials (passwords hidden)."""
    hosts = creds.list_hosts()
    return [{"host": h, "username": creds.get(h)["username"]} for h in hosts]


@app.post("/api/credentials")
async def set_credentials(cred: CredentialSet):
    """Store credentials for a camera host."""
    creds.set(cred.host, cred.username, cred.password)
    return {"message": f"Credentials saved for {cred.host}"}


@app.delete("/api/credentials/{host}")
async def delete_credentials(host: str):
    """Delete credentials for a camera host."""
    if creds.delete(host):
        return {"message": f"Deleted credentials for {host}"}
    raise HTTPException(404, f"No credentials for {host}")


# Stream status
@app.get("/api/status")
async def system_status():
    """Get system status overview."""
    cameras = db.get_cameras()
    streams = {}
    for key, info in active_streams.items():
        streams[key] = {
            "status": info["status"],
            "frame_count": info.get("frame_count", 0),
            "recording": info.get("recorder") is not None and info["recorder"].recording if info.get("recorder") else False,
            "last_detections": info.get("last_detections", []),
        }

    rec_count = len(list(RECORDINGS_DIR.glob("*.mp4"))) if RECORDINGS_DIR.exists() else 0

    return {
        "cameras_registered": len(cameras),
        "streams_active": sum(1 for s in streams.values() if s["status"] == "streaming"),
        "recordings_count": rec_count,
        "streams": streams,
        "detection": detection_settings,
    }


# Detection settings
class DetectionToggle(BaseModel):
    motion: Optional[bool] = None
    objects: Optional[bool] = None
    faces: Optional[bool] = None


@app.get("/api/detection")
async def get_detection_settings():
    """Get current detection settings."""
    return detection_settings


@app.post("/api/detection")
async def set_detection_settings(toggle: DetectionToggle):
    """Toggle detection features on/off."""
    if toggle.motion is not None:
        detection_settings["motion"] = toggle.motion
    if toggle.objects is not None:
        detection_settings["objects"] = toggle.objects
    if toggle.faces is not None:
        detection_settings["faces"] = toggle.faces
    logger.info(f"Detection settings updated: {detection_settings}")
    return {"message": "Detection settings updated", "settings": detection_settings}
