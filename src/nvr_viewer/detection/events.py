"""Detection event processing and notification."""
import av
import cv2
import numpy as np
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SNAPSHOT_DIR = Path.home() / ".nvr-viewer" / "snapshots"
CLIPS_DIR = Path.home() / ".nvr-viewer" / "clips"


class ClipRecorder:
    """Records a short detection clip from a frame buffer + live frames."""

    def __init__(self, output_path: str, pre_frames: list[np.ndarray],
                 post_duration: float = 7.0, fps: float = 10.0):
        self.output_path = output_path
        self.post_duration = post_duration
        self.fps = fps
        self._lock = threading.Lock()
        self._active = True
        self._start_time = time.time()
        self._detections: list[dict] = []

        h, w = pre_frames[0].shape[:2] if pre_frames else (720, 1280)
        self._container = av.open(output_path, mode="w")
        self._stream = self._container.add_stream("libx264", rate=int(fps))
        self._stream.width = w
        self._stream.height = h
        self._stream.pix_fmt = "yuv420p"
        self._stream.options = {"crf": "23", "preset": "fast"}
        self._frame_count = 0

        # Write pre-event buffer frames
        for frame in pre_frames:
            self._write(frame)

    @property
    def active(self) -> bool:
        return self._active

    def set_detections(self, detections: list[dict]):
        """Update current detections for bbox overlay."""
        self._detections = detections

    def add_frame(self, frame: np.ndarray):
        """Add a live frame with detection overlay."""
        if not self._active:
            return
        annotated = self._draw_boxes(frame)
        self._write(annotated)
        if time.time() - self._start_time >= self.post_duration:
            self.finish()

    def _draw_boxes(self, frame: np.ndarray) -> np.ndarray:
        if not self._detections:
            return frame
        out = frame.copy()
        for det in self._detections:
            x, y, w, h = det["bbox"]
            color = {"motion": (0, 255, 255), "object": (0, 255, 0),
                     "face": (255, 0, 255)}.get(det["type"], (0, 255, 0))
            cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
            label = f"{det.get('label', det['type'])} {det.get('confidence', 0):.0%}"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(out, (x, y - th - 6), (x + tw + 4, y), color, -1)
            cv2.putText(out, label, (x + 2, y - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1)
        return out

    def _write(self, frame: np.ndarray):
        with self._lock:
            if not self._active:
                return
            try:
                vf = av.VideoFrame.from_ndarray(frame, format="bgr24")
                vf.pts = self._frame_count
                for pkt in self._stream.encode(vf):
                    self._container.mux(pkt)
                self._frame_count += 1
            except Exception as e:
                logger.debug(f"Clip write error: {e}")

    def finish(self):
        with self._lock:
            if not self._active:
                return
            self._active = False
            try:
                for pkt in self._stream.encode():
                    self._container.mux(pkt)
                self._container.close()
            except Exception as e:
                logger.debug(f"Clip close error: {e}")
            logger.info(f"Clip saved: {self.output_path} ({self._frame_count} frames)")


class EventProcessor:
    """Processes detection events — saves clips with bboxes, logs to DB, deduplicates."""

    def __init__(self, db=None, snapshot_dir: Path = SNAPSHOT_DIR,
                 clips_dir: Path = CLIPS_DIR, cooldown_seconds: float = 12.0,
                 pre_buffer_seconds: float = 3.0, clip_duration: float = 10.0,
                 fps: float = 10.0):
        self.db = db
        self.snapshot_dir = snapshot_dir
        self.clips_dir = clips_dir
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.clips_dir.mkdir(parents=True, exist_ok=True)
        self.cooldown = cooldown_seconds
        self.pre_buffer_seconds = pre_buffer_seconds
        self.post_duration = clip_duration - pre_buffer_seconds
        self.fps = fps
        self._last_events: dict[str, datetime] = {}
        # Per-camera rolling frame buffer for pre-event capture
        self._frame_buffers: dict[int, deque] = {}
        # Active clip recorders per camera
        self._clip_recorders: dict[int, ClipRecorder] = {}

    def buffer_frame(self, camera_id: int, frame: np.ndarray):
        """Add frame to rolling pre-event buffer (call on every Nth frame)."""
        max_frames = int(self.pre_buffer_seconds * self.fps)
        if camera_id not in self._frame_buffers:
            self._frame_buffers[camera_id] = deque(maxlen=max_frames)
        # Store reference, only copy when clip starts
        self._frame_buffers[camera_id].append(frame)

        # Feed active clip recorder
        rec = self._clip_recorders.get(camera_id)
        if rec and rec.active:
            rec.add_frame(frame)
        elif rec and not rec.active:
            del self._clip_recorders[camera_id]

    def process(self, camera_id: int, camera_name: str, frame: np.ndarray,
                detections: list[dict]) -> list[dict]:
        """Process detections: deduplicate, save clip + thumbnail, log to DB."""
        new_events = []

        # Update active clip recorder with latest detections
        rec = self._clip_recorders.get(camera_id)
        if rec and rec.active:
            rec.set_detections(detections)

        for det in detections:
            key = f"{camera_name}:{det['type']}:{det.get('label', '')}"
            now = datetime.now()

            if key in self._last_events:
                elapsed = (now - self._last_events[key]).total_seconds()
                if elapsed < self.cooldown:
                    continue

            self._last_events[key] = now
            ts = now.strftime("%Y%m%d_%H%M%S")
            safe_name = camera_name.replace(" ", "_")

            # Save thumbnail with bbox
            snapshot_path = ""
            try:
                fname = f"{safe_name}_{det['type']}_{ts}.jpg"
                snapshot_path = str(self.snapshot_dir / fname)
                snap = frame.copy()
                for d in detections:
                    x, y, w, h = d["bbox"]
                    color = {"motion": (0, 255, 255), "object": (0, 255, 0),
                             "face": (255, 0, 255)}.get(d["type"], (0, 255, 0))
                    cv2.rectangle(snap, (x, y), (x + w, y + h), color, 2)
                    lbl = f"{d.get('label', d['type'])} {d.get('confidence', 0):.0%}"
                    cv2.putText(snap, lbl, (x, y - 10),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
                cv2.imwrite(snapshot_path, snap)
            except Exception as e:
                logger.warning(f"Failed to save snapshot: {e}")
                snapshot_path = ""

            # Start clip recorder if not already active for this camera
            clip_path = ""
            if camera_id not in self._clip_recorders or not self._clip_recorders[camera_id].active:
                try:
                    clip_fname = f"{safe_name}_{det['type']}_{ts}.mp4"
                    clip_path = str(self.clips_dir / clip_fname)
                    pre_frames = list(self._frame_buffers.get(camera_id, []))
                    clip_rec = ClipRecorder(clip_path, pre_frames,
                                           post_duration=self.post_duration,
                                           fps=self.fps)
                    clip_rec.set_detections(detections)
                    self._clip_recorders[camera_id] = clip_rec
                    logger.info(f"Clip recording started: {clip_fname} "
                                f"({len(pre_frames)} pre-frames)")
                except Exception as e:
                    logger.warning(f"Failed to start clip: {e}")
                    clip_path = ""

            # Log to database
            if self.db:
                try:
                    self.db.log_detection(
                        camera_id=camera_id,
                        detection_type=det["type"],
                        confidence=det.get("confidence", 0),
                        label=det.get("label", ""),
                        bbox=det["bbox"],
                        snapshot_path=snapshot_path,
                        metadata=clip_path,
                    )
                except Exception as e:
                    logger.warning(f"Failed to log detection: {e}")

            det["snapshot_path"] = snapshot_path
            det["clip_path"] = clip_path
            new_events.append(det)
            logger.info(f"[{camera_name}] {det['type']}: {det.get('label', '')} "
                        f"({det.get('confidence', 0):.0%})")

        return new_events
