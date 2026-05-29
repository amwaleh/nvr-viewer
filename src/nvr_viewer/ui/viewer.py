"""OpenCV GUI viewer for camera streams."""
import cv2
import numpy as np
import logging
import time
from typing import Optional
from threading import Lock

logger = logging.getLogger(__name__)


class Viewer:
    """Multi-camera display using OpenCV windows."""

    def __init__(self, max_width: int = 960):
        self.max_width = max_width
        self._frames: dict[str, np.ndarray] = {}
        self._lock = Lock()
        self._overlays: dict[str, list[dict]] = {}
        self._recording_status: dict[str, bool] = {}

    def update_frame(self, camera_name: str, frame: np.ndarray,
                     detections: list[dict] = None, recording: bool = False):
        """Update the frame for a camera."""
        display = frame.copy()

        # Draw detection overlays
        if detections:
            for det in detections:
                x, y, w, h = det["bbox"]
                color = self._color_for_type(det["type"])
                cv2.rectangle(display, (x, y), (x + w, y + h), color, 2)
                label = f"{det.get('label', det['type'])} {det.get('confidence', 0):.0%}"
                cv2.putText(display, label, (x, y - 8),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # Recording indicator
        if recording:
            cv2.circle(display, (20, 20), 8, (0, 0, 255), -1)
            cv2.putText(display, "REC", (35, 28),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)

        # Timestamp
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(display, ts, (10, display.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255), 1)

        with self._lock:
            self._frames[camera_name] = display

    def show(self) -> int:
        """Display all camera frames. Returns waitKey result."""
        with self._lock:
            for name, frame in self._frames.items():
                h, w = frame.shape[:2]
                if w > self.max_width:
                    scale = self.max_width / w
                    display = cv2.resize(frame, (self.max_width, int(h * scale)))
                else:
                    display = frame
                cv2.imshow(name, display)

        return cv2.waitKey(30) & 0xFF

    def close(self):
        """Destroy all windows."""
        cv2.destroyAllWindows()

    @staticmethod
    def _color_for_type(det_type: str) -> tuple:
        colors = {
            "motion": (0, 255, 255),    # Yellow
            "person": (0, 255, 0),      # Green
            "face": (255, 0, 255),      # Magenta
            "animal": (0, 165, 255),    # Orange
            "vehicle": (255, 0, 0),     # Blue
            "object": (128, 128, 128),  # Gray
        }
        return colors.get(det_type, (255, 255, 255))
