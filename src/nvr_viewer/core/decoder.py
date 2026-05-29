"""H264 decoder using PyAV CodecContext."""
import av
import numpy as np
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class H264Decoder:
    """Decodes H264 NAL unit data into BGR frames using PyAV."""

    def __init__(self, sps_pps: bytes = b""):
        self._codec: Optional[av.CodecContext] = None
        self._sps_pps = sps_pps
        self._frame_count = 0
        self._init_decoder()

    def _init_decoder(self):
        self._codec = av.CodecContext.create("h264", "r")
        if self._sps_pps:
            self._codec.extradata = self._sps_pps
        self._codec.open()
        logger.info("H264 decoder initialized")

    def decode(self, nal_data: bytes) -> list[np.ndarray]:
        """Decode NAL unit data (with Annex B start codes) into BGR frames.

        Args:
            nal_data: Raw H264 NAL units with 0x00000001 start codes

        Returns:
            List of BGR numpy arrays (usually 0 or 1 frames)
        """
        frames = []
        try:
            packet = av.Packet(nal_data)
            decoded = self._codec.decode(packet)
            for vf in decoded:
                img = vf.to_ndarray(format="bgr24")
                frames.append(img)
                self._frame_count += 1
        except Exception as e:
            if self._frame_count == 0:
                pass  # Normal — waiting for keyframe
            else:
                logger.debug(f"Decode error: {e}")
        return frames

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def close(self):
        if self._codec:
            self._codec.close()
            self._codec = None
