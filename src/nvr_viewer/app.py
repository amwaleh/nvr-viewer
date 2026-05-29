"""Main NVR application — ties together all modules."""
import logging
import threading
import time
import signal
import sys
from pathlib import Path
from typing import Optional

from .core.rtsp_client import RTSPClient, CameraConfig
from .core.decoder import H264Decoder
from .core.recorder import Recorder
from .detection.motion import MotionDetector
from .detection.detector import ObjectDetector, FaceDetector
from .detection.events import EventProcessor
from .storage.database import Database
from .storage.credentials import CredentialStore
from .network.scanner import NetworkScanner
from .ui.viewer import Viewer

logger = logging.getLogger(__name__)


class NVRApp:
    """Main NVR application controller."""

    def __init__(self, enable_detection: bool = True, enable_recording: bool = False):
        self.db = Database()
        self.creds = CredentialStore()
        self.viewer = Viewer()
        self.scanner = NetworkScanner()
        self.enable_detection = enable_detection
        self.enable_recording = enable_recording

        # Detection engines
        self._motion_detector = MotionDetector() if enable_detection else None
        self._object_detector = None  # Lazy-loaded
        self._face_detector = None  # Lazy-loaded
        self._event_processor = EventProcessor(db=self.db) if enable_detection else None

        # Camera threads
        self._threads: dict[str, threading.Thread] = {}
        self._recorders: dict[str, Recorder] = {}
        self._stop_event = threading.Event()
        self._cameras: dict[str, CameraConfig] = {}

    def add_camera(self, config: CameraConfig):
        """Add a camera to monitor."""
        self._cameras[config.name] = config
        # Store credentials
        self.creds.set(config.host, config.username, config.password)
        # Register in DB
        self.db.add_camera(config.name, config.host, config.port, config.path)
        logger.info(f"Added camera: {config.name} ({config.host})")

    def auto_discover(self, subnet: str = None) -> list[dict]:
        """Auto-discover cameras on the network."""
        logger.info("Scanning network for cameras...")
        discovered = self.scanner.discover_cameras(subnet)
        logger.info(f"Found {len(discovered)} cameras")

        for cam_info in discovered:
            host = cam_info["host"]
            port = cam_info.get("port", 554)
            paths = cam_info.get("paths", ["/onvif1"])
            path = paths[0] if paths else "/onvif1"

            # Check if we have stored credentials
            stored_cred = self.creds.get(host)
            username = stored_cred["username"] if stored_cred else "admin"
            password = stored_cred["password"] if stored_cred else ""

            name = f"Camera_{host.split('.')[-1]}"
            cam_info["name"] = name
            cam_info["path"] = path
            cam_info["has_credentials"] = stored_cred is not None

        return discovered

    def _camera_stream(self, config: CameraConfig):
        """Camera streaming thread — connects, decodes, detects."""
        name = config.name
        logger.info(f"[{name}] Starting stream from {config.host}")

        client = RTSPClient(config)
        if not client.connect():
            logger.error(f"[{name}] Connection failed")
            return

        decoder = H264Decoder(client.sps_pps)
        recorder = self._recorders.get(name)
        camera_db = self.db.get_camera_by_host(config.host)
        camera_id = camera_db["id"] if camera_db else 0

        def on_frame(nal_data: bytes, is_first: bool):
            frames = decoder.decode(nal_data)
            for frame in frames:
                # Detection
                detections = []
                if self.enable_detection and self._motion_detector:
                    motion = self._motion_detector.detect(frame)
                    detections.extend(motion)

                    # Only run heavy detection if motion detected
                    if motion and self._object_detector:
                        obj_dets = self._object_detector.detect(frame)
                        detections.extend(obj_dets)

                    if motion and self._face_detector:
                        face_dets = self._face_detector.detect(frame)
                        detections.extend(face_dets)

                # Process events (dedup, snapshot, DB log)
                if detections and self._event_processor:
                    self._event_processor.process(camera_id, name, frame, detections)

                # Recording
                if recorder and recorder.recording:
                    recorder.write_frame(frame)

                # Update viewer
                recording = recorder.recording if recorder else False
                self.viewer.update_frame(name, frame, detections, recording)

        client.read_frames(on_frame, self._stop_event.is_set)
        decoder.close()
        logger.info(f"[{name}] Stream ended")

    def start(self):
        """Start streaming from all configured cameras."""
        if not self._cameras:
            logger.warning("No cameras configured. Use add_camera() or auto_discover().")
            return

        # Start camera threads
        for name, config in self._cameras.items():
            if self.enable_recording:
                self._recorders[name] = Recorder(name)

            t = threading.Thread(target=self._camera_stream, args=(config,), daemon=True)
            self._threads[name] = t
            t.start()

        logger.info(f"Started {len(self._threads)} camera streams")

        # Handle Ctrl+C
        def signal_handler(sig, frame):
            self.stop()
            sys.exit(0)
        signal.signal(signal.SIGINT, signal_handler)

    def start_recording(self, camera_name: str = None):
        """Start recording for one or all cameras."""
        targets = [camera_name] if camera_name else list(self._cameras.keys())
        for name in targets:
            if name not in self._recorders:
                self._recorders[name] = Recorder(name)
            path = self._recorders[name].start()
            camera_db = self.db.get_camera_by_host(self._cameras[name].host)
            if camera_db:
                self.db.log_recording(camera_db["id"], path, "manual")
            logger.info(f"Recording {name} -> {path}")

    def stop_recording(self, camera_name: str = None):
        """Stop recording for one or all cameras."""
        targets = [camera_name] if camera_name else list(self._recorders.keys())
        for name in targets:
            if name in self._recorders:
                info = self._recorders[name].stop()
                if info:
                    logger.info(f"Stopped recording {name}: {info.get('file_path', '')}")

    def enable_object_detection(self, model: str = "yolov8n.pt"):
        """Enable YOLO object detection (lazy-loads model)."""
        self._object_detector = ObjectDetector(model_name=model)
        logger.info(f"Object detection enabled (model: {model})")

    def enable_face_detection(self):
        """Enable face detection."""
        self._face_detector = FaceDetector()
        logger.info("Face detection enabled")

    def run_viewer(self):
        """Run the OpenCV viewer loop (blocking)."""
        print("\nNVR Viewer — Controls:")
        print("  q     = Quit")
        print("  r     = Toggle recording")
        print("  d     = Toggle object detection")
        print("  f     = Toggle face detection")
        print("  s     = Screenshot all cameras")
        print()

        recording_on = False

        while not self._stop_event.is_set():
            key = self.viewer.show()

            if key == ord("q"):
                break
            elif key == ord("r"):
                recording_on = not recording_on
                if recording_on:
                    self.start_recording()
                else:
                    self.stop_recording()
            elif key == ord("d"):
                if self._object_detector is None:
                    self.enable_object_detection()
                else:
                    self._object_detector = None
                    print("Object detection disabled")
            elif key == ord("f"):
                if self._face_detector is None:
                    self.enable_face_detection()
                else:
                    self._face_detector = None
                    print("Face detection disabled")
            elif key == ord("s"):
                self._screenshot_all()

        self.stop()

    def _screenshot_all(self):
        """Save a screenshot from each camera."""
        snap_dir = Path.home() / ".nvr-viewer" / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        import cv2
        ts = time.strftime("%Y%m%d_%H%M%S")
        for name, frame in self.viewer._frames.items():
            path = snap_dir / f"{name.replace(' ', '_')}_{ts}.jpg"
            cv2.imwrite(str(path), frame)
            print(f"Screenshot: {path}")

    def stop(self):
        """Stop all streams and cleanup."""
        self._stop_event.set()
        self.stop_recording()
        self.viewer.close()
        self.db.close()
        logger.info("NVR stopped")
