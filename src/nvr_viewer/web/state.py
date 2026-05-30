"""Shared application state for the NVR Viewer web backend.

Centralizes all mutable state (streams, detectors, settings, storage paths)
so routers and stream workers can access them without circular imports.
"""
import json
import logging
import threading
from pathlib import Path
from typing import Optional

from ..detection.motion import MotionDetector
from ..detection.detector import ObjectDetector, FaceDetector
from ..detection.events import EventProcessor
from ..storage.database import Database
from ..storage.credentials import CredentialStore
from ..storage.manager import StorageManager
from ..network.scanner import NetworkScanner
from ..notifications import NotificationManager

logger = logging.getLogger(__name__)

# --- Singletons ---
db = Database()
creds = CredentialStore()
scanner = NetworkScanner()
notifier = NotificationManager()

# --- Config paths ---
CONFIG_DIR = Path.home() / ".nvr-viewer"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
DEFAULT_STORAGE_DIR = str(Path.cwd())

# --- Detection engines (shared across streams) ---
motion_detector_cache: dict[str, MotionDetector] = {}
object_detector: Optional[ObjectDetector] = None
face_detector: Optional[FaceDetector] = None

# --- Stream management ---
active_streams: dict[str, dict] = {}
stream_lock = threading.Lock()


# --- Settings load / save ---

def _load_settings() -> dict:
    """Load all app settings from disk."""
    defaults = {
        "detection": {"motion": True, "objects": True, "faces": True},
        "camera_detection": {},
        "continuous_recording": {},
        "storage_dir": DEFAULT_STORAGE_DIR,
        "min_free_percent": 30,
    }
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r") as f:
                saved = json.load(f)
            defaults["detection"].update(saved.get("detection", {}))
            if "camera_detection" in saved:
                defaults["camera_detection"] = saved["camera_detection"]
            if "continuous_recording" in saved:
                defaults["continuous_recording"] = saved["continuous_recording"]
            if "storage_dir" in saved:
                defaults["storage_dir"] = saved["storage_dir"]
            if "min_free_percent" in saved:
                defaults["min_free_percent"] = saved["min_free_percent"]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load settings: %s", e)
    else:
        old_file = CONFIG_DIR / "detection_settings.json"
        if old_file.exists():
            try:
                with open(old_file, "r") as f:
                    defaults["detection"].update(json.load(f))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load legacy settings: %s", e)
    return defaults


_settings = _load_settings()
detection_settings: dict = _settings["detection"]
camera_detection_settings: dict = _settings.get("camera_detection", {})
continuous_recording_settings: dict = _settings.get("continuous_recording", {})

# --- Storage paths (mutable — can be updated at runtime) ---
STORAGE_DIR = Path(_settings["storage_dir"])
RECORDINGS_DIR = STORAGE_DIR / "recordings"
SNAPSHOTS_DIR = STORAGE_DIR / "snapshots"
CLIPS_DIR = STORAGE_DIR / "clips"

for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# --- Event processor ---
event_processor = EventProcessor(db=db, snapshot_dir=SNAPSHOTS_DIR, clips_dir=CLIPS_DIR)

# --- Storage manager (disk space guardian) ---
min_free_percent: int = _settings.get("min_free_percent", 30)
storage_manager = StorageManager(
    storage_dir=STORAGE_DIR,
    min_free_percent=min_free_percent,
    notifier=notifier,
)
storage_manager.start()


def save_settings():
    """Persist all settings to disk."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(SETTINGS_FILE, "w") as f:
            json.dump({
                "detection": detection_settings,
                "camera_detection": camera_detection_settings,
                "continuous_recording": continuous_recording_settings,
                "storage_dir": str(STORAGE_DIR),
                "min_free_percent": min_free_percent,
            }, f, indent=2)
    except OSError as e:
        logger.warning("Failed to save settings: %s", e)


def cam_detection_enabled(camera_id: int, det_type: str) -> bool:
    """Check if a detection type is enabled for a specific camera."""
    cam_key = str(camera_id)
    if cam_key in camera_detection_settings:
        return camera_detection_settings[cam_key].get(
            det_type, detection_settings.get(det_type, True))
    return detection_settings.get(det_type, True)


def update_storage_dir(new_dir: Path):
    """Update storage directory and re-initialize paths."""
    global STORAGE_DIR, RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR
    global event_processor, storage_manager
    STORAGE_DIR = new_dir
    RECORDINGS_DIR = STORAGE_DIR / "recordings"
    SNAPSHOTS_DIR = STORAGE_DIR / "snapshots"
    CLIPS_DIR = STORAGE_DIR / "clips"
    for d in [RECORDINGS_DIR, SNAPSHOTS_DIR, CLIPS_DIR]:
        d.mkdir(parents=True, exist_ok=True)
    event_processor = EventProcessor(db=db, snapshot_dir=SNAPSHOTS_DIR, clips_dir=CLIPS_DIR)
    storage_manager.stop()
    storage_manager = StorageManager(
        storage_dir=STORAGE_DIR, min_free_percent=min_free_percent,
        notifier=notifier,
    )
    storage_manager.start()
    save_settings()
    logger.info("Storage directory updated: %s", STORAGE_DIR)
