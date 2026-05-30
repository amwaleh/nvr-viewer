"""Stream workers for RTSP and MJPEG cameras.

Manages background threads that connect to cameras, decode frames,
run detection pipelines, and feed the event processor.
"""
import cv2
import logging
import numpy as np
import threading
import time
import urllib.request

from ..core.rtsp_client import RTSPClient, CameraConfig
from ..core.decoder import H264Decoder
from ..detection.motion import MotionDetector
from ..detection.detector import ObjectDetector, FaceDetector
from . import state

logger = logging.getLogger(__name__)


def _run_detection(frame, camera_id: int, camera_name: str,
                   motion_det: MotionDetector, stream_info: dict,
                   frame_skip: int):
    """Shared detection pipeline for both RTSP and MJPEG workers."""
    detections = []

    if state.cam_detection_enabled(camera_id, "motion"):
        try:
            detections.extend(motion_det.detect(frame))
        except Exception as e:
            logger.debug(f"Motion detect error: {e}")

    if state.cam_detection_enabled(camera_id, "objects") and (detections or frame_skip % 15 == 0):
        try:
            if state.object_detector is None:
                state.object_detector = ObjectDetector()
            detections.extend(state.object_detector.detect(frame))
        except Exception as e:
            logger.debug(f"Object detect error: {e}")

    if state.cam_detection_enabled(camera_id, "faces") and (detections or frame_skip % 15 == 0):
        try:
            if state.face_detector is None:
                state.face_detector = FaceDetector()
            detections.extend(state.face_detector.detect(frame))
        except Exception as e:
            logger.debug(f"Face detect error: {e}")

    if detections:
        stream_info["active_detections"] = detections
        try:
            new_events = state.event_processor.process(
                camera_id, camera_name, frame, detections)
            if new_events:
                stream_info["last_detections"] = new_events
        except Exception as e:
            logger.debug(f"Event processing error: {e}")
    else:
        stream_info["active_detections"] = []


def _stream_worker(camera_key: str, config: CameraConfig):
    """Background thread for RTSP camera streams."""
    stream_info = state.active_streams.get(camera_key)
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

    if camera_key not in state.motion_detector_cache:
        state.motion_detector_cache[camera_key] = MotionDetector()
    motion_det = state.motion_detector_cache[camera_key]

    cam_db = state.db.get_camera_by_host(config.host)
    camera_id = cam_db["id"] if cam_db else 0
    frame_skip = 0

    def on_frame(nal_data: bytes, is_first: bool):
        nonlocal frame_skip
        frames = decoder.decode(nal_data)
        for frame in frames:
            stream_info["latest_frame"] = frame
            stream_info["frame_count"] = stream_info.get("frame_count", 0) + 1

            rec = stream_info.get("recorder")
            if rec and rec.recording:
                rec.write_frame(frame)

            frame_skip += 1
            state.event_processor.buffer_frame(camera_id, frame)

            if frame_skip % 5 != 0:
                continue

            _run_detection(frame, camera_id, config.name, motion_det,
                           stream_info, frame_skip)

    client.read_frames(on_frame, stop_event.is_set)
    decoder.close()
    stream_info["status"] = "disconnected"
    logger.info(f"Stream ended: {camera_key}")


def _mjpeg_stream_worker(camera_key: str, stream_url: str):
    """Background thread for MJPEG camera streams."""
    stream_info = state.active_streams.get(camera_key)
    if not stream_info:
        return

    stop_event = stream_info["stop_event"]

    if camera_key not in state.motion_detector_cache:
        state.motion_detector_cache[camera_key] = MotionDetector()
    motion_det = state.motion_detector_cache[camera_key]

    cam_db = state.db.get_camera_by_host(stream_info.get("_host", ""))
    camera_id = cam_db["id"] if cam_db else 0
    camera_name = stream_info.get("_name", f"MJPEG-{camera_key}")
    frame_skip = 0

    try:
        req = urllib.request.Request(stream_url)
        resp = urllib.request.urlopen(req, timeout=10)

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

            while True:
                soi = buf.find(b"\xff\xd8")
                if soi == -1:
                    buf = buf[-2:]
                    break
                eoi = buf.find(b"\xff\xd9", soi + 2)
                if eoi == -1:
                    break

                jpeg_bytes = buf[soi:eoi + 2]
                buf = buf[eoi + 2:]

                frame_skip += 1
                stream_info["frame_count"] = stream_info.get("frame_count", 0) + 1

                # Write to recorder if active
                rec = stream_info.get("recorder")
                if rec and rec.recording:
                    decode_frame = cv2.imdecode(
                        np.frombuffer(jpeg_bytes, dtype=np.uint8),
                        cv2.IMREAD_COLOR
                    )
                    if decode_frame is not None:
                        rec.write_frame(decode_frame)

                stream_info["latest_jpeg"] = jpeg_bytes

                run_detection = (frame_skip % 5 == 0)
                if frame_skip % 3 == 0 or run_detection:
                    frame = cv2.imdecode(
                        np.frombuffer(jpeg_bytes, dtype=np.uint8),
                        cv2.IMREAD_COLOR
                    )
                    if frame is None:
                        continue

                    stream_info["latest_frame"] = frame
                    state.event_processor.buffer_frame(camera_id, frame)

                    if not run_detection:
                        continue

                    _run_detection(frame, camera_id, camera_name, motion_det,
                                   stream_info, frame_skip)

        resp.close()
    except Exception as e:
        logger.error(f"MJPEG stream error: {e}")

    stream_info["status"] = "disconnected"
    logger.info(f"MJPEG stream ended: {camera_key}")


def start_stream(camera_key: str, config: CameraConfig = None,
                 camera_type: str = "rtsp", stream_url: str = "",
                 camera_name: str = "", camera_host: str = ""):
    """Start a camera stream if not already running."""
    with state.stream_lock:
        if camera_key in state.active_streams and \
                state.active_streams[camera_key]["status"] == "streaming":
            return

        stop_event = threading.Event()
        state.active_streams[camera_key] = {
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
        state.active_streams[camera_key]["thread"] = t


def stop_stream(camera_key: str):
    """Stop a camera stream."""
    with state.stream_lock:
        if camera_key in state.active_streams:
            state.active_streams[camera_key]["stop_event"].set()
            rec = state.active_streams[camera_key].get("recorder")
            if rec and rec.recording:
                rec.stop()
            del state.active_streams[camera_key]
