"""FastAPI backend for NVR Viewer web interface."""
import asyncio
import cv2
import json
import numpy as np
import logging
import time
import threading
import urllib.request
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
event_processor = None  # Initialized after settings load

# App settings — persisted to disk
CONFIG_DIR = Path.home() / ".nvr-viewer"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

# Default storage is the current working directory
DEFAULT_STORAGE_DIR = str(Path.cwd())


def _load_settings() -> dict:
    """Load all app settings from disk."""
    defaults = {
        "detection": {"motion": True, "objects": True, "faces": True},
        "storage_dir": DEFAULT_STORAGE_DIR,
    }
    # Try new unified settings file first
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            defaults["detection"].update(saved.get("detection", {}))
            if "storage_dir" in saved:
                defaults["storage_dir"] = saved["storage_dir"]
        except Exception:
            pass
    else:
        # Migrate from old detection_settings.json
        old_file = CONFIG_DIR / "detection_settings.json"
        if old_file.exists():
            try:
                with open(old_file, "r") as f:
                    defaults["detection"].update(json.load(f))
            except Exception:
                pass
    return defaults


def _save_settings():
    """Persist all settings to disk."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump({
                "detection": detection_settings,
                "storage_dir": str(STORAGE_DIR),
            }, f, indent=2)
    except Exception as e:
        logger.warning(f"Failed to save settings: {e}")


_settings = _load_settings()
detection_settings = _settings["detection"]
STORAGE_DIR = Path(_settings["storage_dir"])

# Derived storage paths
RECORDINGS_DIR = STORAGE_DIR / "recordings"
SNAPSHOTS_DIR = STORAGE_DIR / "snapshots"
CLIPS_DIR = STORAGE_DIR / "clips"

# Ensure dirs exist
for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# Initialize event processor with configured storage paths
event_processor = EventProcessor(db=db, snapshot_dir=SNAPSHOTS_DIR, clips_dir=CLIPS_DIR)

# Active camera streams: {camera_id: {client, decoder, latest_frame, thread, stop_event}}
active_streams: dict[str, dict] = {}
stream_lock = threading.Lock()


# --- Pydantic Models ---

class CameraAdd(BaseModel):
    name: str
    host: str
    port: int = 554
    path: str = "/onvif1"
    username: str = "admin"
    password: str = ""
    type: str = "rtsp"  # "rtsp" or "mjpeg"
    stream_url: str = ""  # Full MJPEG URL for mjpeg type


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

            frame_skip += 1

            # Feed pre-event buffer (EventProcessor handles adaptive skip)
            event_processor.buffer_frame(camera_id, frame)

            # Run detection every 5th frame to reduce CPU load
            if frame_skip % 5 != 0:
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
                stream_info["active_detections"] = detections
                try:
                    new_events = event_processor.process(
                        camera_id, config.name, frame, detections)
                    if new_events:
                        stream_info["last_detections"] = new_events
                except Exception as e:
                    logger.debug(f"Event processing error: {e}")
            else:
                stream_info["active_detections"] = []

    client.read_frames(on_frame, stop_event.is_set)
    decoder.close()
    stream_info["status"] = "disconnected"
    logger.info(f"Stream ended: {camera_key}")


def _mjpeg_stream_worker(camera_key: str, stream_url: str):
    """Background thread that reads MJPEG over HTTP using raw parsing for low latency."""
    global object_detector, face_detector

    stream_info = active_streams.get(camera_key)
    if not stream_info:
        return

    stop_event = stream_info["stop_event"]

    # Per-camera motion detector
    if camera_key not in motion_detector_cache:
        motion_detector_cache[camera_key] = MotionDetector()
    motion_det = motion_detector_cache[camera_key]

    cam_db = db.get_camera_by_host(stream_info.get("_host", ""))
    camera_id = cam_db["id"] if cam_db else 0
    camera_name = stream_info.get("_name", f"MJPEG-{camera_key}")
    frame_skip = 0

    try:
        # Raw HTTP connection — avoids OpenCV's buffering overhead
        req = urllib.request.Request(stream_url)
        resp = urllib.request.urlopen(req, timeout=10)

        # Read MJPEG boundary from content-type header
        content_type = resp.headers.get("Content-Type", "")
        boundary = b"--"
        if "boundary=" in content_type:
            boundary = b"--" + content_type.split("boundary=")[1].strip().encode()

        stream_info["status"] = "streaming"
        logger.info(f"MJPEG stream connected (raw): {stream_url}")

        buf = b""
        while not stop_event.is_set():
            chunk = resp.read(4096)
            if not chunk:
                time.sleep(0.1)
                continue
            buf += chunk

            # Find JPEG frame boundaries (SOI=FFD8, EOI=FFD9)
            while True:
                soi = buf.find(b"\xff\xd8")
                if soi == -1:
                    buf = buf[-2:]  # keep potential partial marker
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi == -1:
                    break  # incomplete frame, read more

                # Extract complete JPEG frame
                jpeg_bytes = buf[soi:eoi + 2]
                buf = buf[eoi + 2:]

                frame_skip += 1
                stream_info["frame_count"] = stream_info.get("frame_count", 0) + 1

                # Store raw JPEG bytes for passthrough to browser (no re-encode!)
                stream_info["latest_jpeg"] = jpeg_bytes

                # Decode every 3rd frame for clip buffer, every 5th for detection
                run_detection = (frame_skip % 5 == 0)
                if frame_skip % 3 == 0 or run_detection:
                    frame = cv2.imdecode(
                        np.frombuffer(jpeg_bytes, dtype=np.uint8),
                        cv2.IMREAD_COLOR
                    )
                    if frame is None:
                        continue

                    stream_info["latest_frame"] = frame

                    # Feed pre-event buffer
                    event_processor.buffer_frame(camera_id, frame)

                    if not run_detection:
                        continue

                    # Run detection pipeline
                    detections = []

                    if detection_settings.get("motion"):
                        try:
                            detections.extend(motion_det.detect(frame))
                        except Exception as e:
                            logger.debug(f"Motion detect error: {e}")

                    if detection_settings.get("objects") and (detections or frame_skip % 15 == 0):
                        try:
                            if object_detector is None:
                                object_detector = ObjectDetector()
                            detections.extend(object_detector.detect(frame))
                        except Exception as e:
                            logger.debug(f"Object detect error: {e}")

                    if detection_settings.get("faces") and (detections or frame_skip % 15 == 0):
                        try:
                            if face_detector is None:
                                face_detector = FaceDetector()
                            detections.extend(face_detector.detect(frame))
                        except Exception as e:
                            logger.debug(f"Face detect error: {e}")

                    if detections:
                        stream_info["active_detections"] = detections
                        try:
                            new_events = event_processor.process(
                                camera_id, camera_name, frame, detections)
                            if new_events:
                                stream_info["last_detections"] = new_events
                        except Exception as e:
                            logger.debug(f"Event processing error: {e}")
                    else:
                        stream_info["active_detections"] = []

        resp.close()
    except Exception as e:
        logger.error(f"MJPEG stream error: {e}")

    stream_info["status"] = "disconnected"
    logger.info(f"MJPEG stream ended: {camera_key}")


def start_stream(camera_key: str, config: CameraConfig = None,
                 camera_type: str = "rtsp", stream_url: str = "",
                 camera_name: str = "", camera_host: str = ""):
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
            "_host": camera_host or (config.host if config else ""),
            "_name": camera_name or (config.name if config else ""),
        }

        if camera_type == "mjpeg" and stream_url:
            t = threading.Thread(target=_mjpeg_stream_worker,
                                 args=(camera_key, stream_url), daemon=True)
        else:
            t = threading.Thread(target=_stream_worker,
                                 args=(camera_key, config), daemon=True)
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


@app.get("/events")
async def events_page():
    """Serve the events gallery page."""
    events_path = TEMPLATES_DIR / "events.html"
    if events_path.exists():
        return FileResponse(events_path, media_type="text/html")
    raise HTTPException(404, "Events page not found")


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
    camera_id = db.add_camera(cam.name, cam.host, cam.port, cam.path,
                              camera_type=cam.type, stream_url=cam.stream_url)
    if cam.password:
        creds.set(cam.host, cam.username, cam.password)
    return {"id": camera_id, "message": f"Camera '{cam.name}' added"}


@app.delete("/api/cameras/{camera_id}")
async def remove_camera(camera_id: int):
    """Remove a camera and stop its stream."""
    stop_stream(str(camera_id))
    if not db.delete_camera(camera_id):
        raise HTTPException(404, "Camera not found")
    return {"message": f"Camera {camera_id} deleted"}


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    path: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None


@app.put("/api/cameras/{camera_id}")
async def update_camera(camera_id: int, cam: CameraUpdate):
    """Update camera settings."""
    # Stop stream if connection details changed
    if cam.host is not None or cam.port is not None or cam.path is not None:
        stop_stream(str(camera_id))

    db.update_camera(camera_id, name=cam.name, host=cam.host,
                     port=cam.port, path=cam.path)

    # Update credentials if provided
    if cam.password:
        # Get current or new host
        cameras = db.get_cameras()
        host = cam.host
        if not host:
            for c in cameras:
                if c["id"] == camera_id:
                    host = c["host"]
                    break
        if host:
            creds.set(host, cam.username or "admin", cam.password)

    return {"message": f"Camera {camera_id} updated"}


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
        cam_type = cam.get("type", "rtsp") or "rtsp"
        cam_stream_url = cam.get("stream_url", "") or ""

        if cam_type == "mjpeg" and cam_stream_url:
            start_stream(key, camera_type="mjpeg", stream_url=cam_stream_url,
                         camera_name=cam["name"], camera_host=cam["host"])
        else:
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
        last_frame_id = -1
        is_mjpeg = cam.get("type") == "mjpeg"
        while True:
            stream = active_streams.get(key)
            if not stream or stream["status"] == "disconnected":
                break

            current_id = stream.get("frame_count", 0)
            if current_id != last_frame_id:
                last_frame_id = current_id

                # MJPEG cameras: passthrough raw JPEG bytes (no decode/re-encode)
                if is_mjpeg:
                    jpeg_raw = stream.get("latest_jpeg")
                    if jpeg_raw:
                        yield (b"--frame\r\n"
                               b"Content-Type: image/jpeg\r\n\r\n" + jpeg_raw + b"\r\n")
                else:
                    # RTSP cameras: encode decoded frame to JPEG
                    frame = stream.get("latest_frame")
                    if frame is not None:
                        h, w = frame.shape[:2]
                        if w > 800:
                            scale = 800 / w
                            small = cv2.resize(frame, (800, int(h * scale)), interpolation=cv2.INTER_NEAREST)
                        else:
                            small = frame
                        _, jpeg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 55])
                        yield (b"--frame\r\n"
                               b"Content-Type: image/jpeg\r\n\r\n" + jpeg.tobytes() + b"\r\n")

            time.sleep(0.033)  # ~30 fps cap

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

    key = str(camera_id)
    cam_type = cam.get("type", "rtsp") or "rtsp"
    cam_stream_url = cam.get("stream_url", "") or ""

    if cam_type == "mjpeg" and cam_stream_url:
        start_stream(key, camera_type="mjpeg", stream_url=cam_stream_url,
                     camera_name=cam["name"], camera_host=cam["host"])
    else:
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
    offset: int = 0,
):
    """Query detection events with pagination."""
    events = db.get_events(camera_id=camera_id, detection_type=detection_type,
                           since=since, limit=limit, offset=offset)
    total = db.count_events(camera_id=camera_id, detection_type=detection_type,
                            since=since)
    # Add web-accessible snapshot and clip URLs using stored full paths
    for ev in events:
        sp = ev.get("snapshot_path")
        if sp and Path(sp).exists():
            # Use relative path from snapshots root for URL
            try:
                rel = Path(sp).relative_to(SNAPSHOTS_DIR)
                ev["snapshot_url"] = f"/api/snapshots/{rel.as_posix()}"
            except ValueError:
                ev["snapshot_url"] = f"/api/snapshots/{Path(sp).name}"
        meta = ev.get("metadata", "")
        if meta and meta.endswith(".mp4"):
            try:
                rel = Path(meta).relative_to(CLIPS_DIR)
                ev["clip_url"] = f"/api/clips/{rel.as_posix()}"
            except ValueError:
                ev["clip_url"] = f"/api/clips/{Path(meta).name}"
    return {"events": events, "total": total, "limit": limit, "offset": offset}


@app.get("/api/snapshots/{filepath:path}")
async def get_snapshot(filepath: str):
    """Serve a detection snapshot image."""
    file_path = SNAPSHOTS_DIR / filepath
    if not file_path.exists() or not file_path.is_file():
       raise HTTPException(404, "Snapshot not found")
    return FileResponse(file_path, media_type="image/jpeg", filename=file_path.name)


@app.get("/api/clips/{filepath:path}")
async def get_clip(filepath: str):
    """Serve a detection video clip."""
    file_path = CLIPS_DIR / filepath
    if not file_path.exists() or not file_path.is_file():
       raise HTTPException(404, "Clip not found")
    return FileResponse(file_path, media_type="video/mp4", filename=file_path.name)


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
    _save_settings()
    logger.info(f"Detection settings updated: {detection_settings}")
    return {"message": "Detection settings updated", "settings": detection_settings}


# Storage settings
class StorageSettings(BaseModel):
    storage_dir: str


@app.get("/api/settings/storage")
async def get_storage_settings():
    """Get current storage directory."""
    return {"storage_dir": str(STORAGE_DIR)}


@app.post("/api/settings/storage")
async def set_storage_settings(settings: StorageSettings):
    """Update storage directory. Requires server restart to take full effect."""
    global STORAGE_DIR, RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR, event_processor
    new_dir = Path(settings.storage_dir)
    if not new_dir.is_absolute():
        raise HTTPException(400, "Storage directory must be an absolute path")
    STORAGE_DIR = new_dir
    RECORDINGS_DIR = STORAGE_DIR / "recordings"
    SNAPSHOTS_DIR = STORAGE_DIR / "snapshots"
    CLIPS_DIR = STORAGE_DIR / "clips"
    for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    event_processor = EventProcessor(db=db, snapshot_dir=SNAPSHOTS_DIR, clips_dir=CLIPS_DIR)
    _save_settings()
    logger.info(f"Storage directory updated: {STORAGE_DIR}")
    return {"message": "Storage directory updated", "storage_dir": str(STORAGE_DIR)}
