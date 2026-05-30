"""Stream recorder — saves H264 stream to MP4 files."""
import av
import cv2
import numpy as np
import logging
import time
from pathlib import Path
from datetime import datetime
from typing import Optional
from threading import Lock

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path.home() / ".nvr-viewer" / "recordings"


class Recorder:
    """Records video frames to MP4 files using PyAV."""

    def __init__(self, camera_name: str, output_dir: Path = RECORDINGS_DIR,
                 fps: float = 15.0, max_duration: int = 3600):
        self.camera_name = camera_name
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.fps = fps
        self.max_duration = max_duration
        self._container: Optional[av.OutputContainer] = None
        self._stream = None
        self._file_path: str = ""
        self._start_time: float = 0
        self._frame_count: int = 0
        self._lock = Lock()
        self._recording = False

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def file_path(self) -> str:
        return self._file_path

    def start(self, width: int = 0, height: int = 0) -> str:
        """Start recording. Returns the output file path."""
        with self._lock:
            if self._recording:
                return self._file_path

            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = self.camera_name.replace(" ", "_")
            self._file_path = str(self.output_dir / f"{safe_name}_{ts}.mp4")

            self._container = av.open(self._file_path, mode="w",
                                      options={"movflags": "frag_keyframe+empty_moov+default_base_moof"})
            self._stream = self._container.add_stream("libx264", rate=int(self.fps))
            if width and height:
                self._stream.width = width
                self._stream.height = height
            self._stream.pix_fmt = "yuv420p"
            self._stream.options = {"crf": "23", "preset": "fast"}

            self._start_time = time.time()
            self._frame_count = 0
            self._recording = True
            logger.info(f"Recording started: {self._file_path}")
            return self._file_path

    def write_frame(self, frame: np.ndarray):
        """Write a BGR frame to the recording."""
        with self._lock:
            if not self._recording or self._container is None:
                return

            # Initialize stream dimensions from first frame (even dims for libx264)
            h, w = frame.shape[:2]
            if self._stream.width == 0:
                self._stream.width = w & ~1
                self._stream.height = h & ~1

            try:
                h, w = frame.shape[:2]
                sw, sh = self._stream.width, self._stream.height
                if w != sw or h != sh:
                    frame = cv2.resize(frame, (sw, sh))
                vf = av.VideoFrame.from_ndarray(frame, format="bgr24")
                vf.pts = self._frame_count
                for packet in self._stream.encode(vf):
                    self._container.mux(packet)
                self._frame_count += 1
            except Exception as e:
                logger.error(f"Write frame error: {e}")

            # Auto-stop if max duration exceeded
            if time.time() - self._start_time > self.max_duration:
                self._stop_internal()

    def stop(self) -> dict:
        """Stop recording. Returns recording info dict."""
        with self._lock:
            return self._stop_internal()

    def _stop_internal(self) -> dict:
        if not self._recording:
            return {}

        try:
            # Flush remaining frames
            if self._stream:
                for packet in self._stream.encode():
                    self._container.mux(packet)
            if self._container:
                self._container.close()
        except Exception as e:
            logger.error(f"Error closing recording: {e}")

        self._recording = False
        duration = time.time() - self._start_time

        info = {
            "file_path": self._file_path,
            "frames": self._frame_count,
            "duration": round(duration, 1),
            "camera": self.camera_name,
        }
        logger.info(f"Recording stopped: {self._file_path} "
                     f"({self._frame_count} frames, {duration:.1f}s)")

        self._container = None
        self._stream = None
        return info
