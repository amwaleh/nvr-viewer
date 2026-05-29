"""RTSP client for Yoosee/HIipCamera cameras with non-standard protocol handling."""

import base64
import hashlib
import logging
import re
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass
class CameraConfig:
    """Camera connection configuration."""

    host: str
    port: int = 554
    path: str = "/onvif1"
    username: str = "admin"
    password: str = ""
    name: str = ""

    def __post_init__(self) -> None:
        """Populate a friendly default camera name."""
        if not self.name:
            self.name = f"Camera_{self.host}"

    @property
    def url(self) -> str:
        """Return the full RTSP URL for the configured path."""
        return f"rtsp://{self.host}:{self.port}{self.path}"


class RTSPClient:
    """Low-level RTSP client with manual handshake for non-standard cameras.

    Handles Digest authentication, TCP interleaved transport, and
    H264 RTP depacketization (FU-A, STAP-A, single NAL).
    """

    COMMON_PATHS = ["/onvif1", "/11", "/stream1", "/h264", "/ch0_0.h264", "/live/ch0", "/1"]

    def __init__(self, config: CameraConfig):
        self.config = config
        self._sock: Optional[socket.socket] = None
        self._session_id: str = ""
        self._realm: str = ""
        self._nonce: str = ""
        self._cseq: int = 1
        self._connected: bool = False
        self.sps_pps: bytes = b""
        self._leftover: bytes = b""

        # RTP depacketization state
        self._fua_buf: bytes = b""
        self._fua_header: int = 0
        self._nal_buffer: bytes = b""

    @property
    def connected(self) -> bool:
        """Return whether the RTSP session is active."""
        return self._connected

    def _digest_auth(self, method: str, uri: str) -> str:
        """Compute a Digest authentication header value."""
        user = self.config.username
        pwd = self.config.password
        ha1 = hashlib.md5(f"{user}:{self._realm}:{pwd}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
        response = hashlib.md5(f"{ha1}:{self._nonce}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{user}", realm="{self._realm}", '
            f'nonce="{self._nonce}", uri="{uri}", response="{response}"'
        )

    def _send(self, msg: str) -> None:
        """Send an RTSP request over the socket."""
        if self._sock is None:
            raise RuntimeError("RTSP socket is not connected")
        logger.debug("Sending RTSP request:\n%s", msg.rstrip())
        self._sock.sendall(msg.encode())

    def _recv(self, timeout: float = 5.0) -> bytes:
        """Receive an RTSP response while honoring Content-Length when present."""
        if self._sock is None:
            return b""

        buf = b""
        self._sock.settimeout(timeout)
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                buf += chunk

                if b"\r\n\r\n" not in buf:
                    continue

                header, _, remainder = buf.partition(b"\r\n\r\n")
                header_text = header.decode(errors="replace")
                match = re.search(r"Content-Length:\s*(\d+)", header_text, re.I)
                if match:
                    body_len = int(match.group(1))
                    if len(remainder) >= body_len:
                        break
                else:
                    break
        except socket.timeout:
            logger.debug("Timed out waiting for RTSP response")

        logger.debug("Received %d RTSP bytes", len(buf))
        return buf

    def connect(self) -> bool:
        """Perform the DESCRIBE -> SETUP -> PLAY handshake.

        Returns:
            True if the client is connected and ready to receive interleaved RTP.
        """
        url = self.config.url
        host = self.config.host
        port = self.config.port

        logger.info("Connecting to RTSP camera %s at %s:%s", self.config.name, host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(10)

        try:
            self._sock.connect((host, port))
        except Exception as exc:
            logger.error("TCP connect to %s:%s failed: %s", host, port, exc)
            self.disconnect()
            return False

        self._cseq = 1

        # DESCRIBE without auth to retrieve the Digest challenge.
        self._send(
            f"DESCRIBE {url} RTSP/1.0\r\n"
            f"CSeq: {self._cseq}\r\n"
            f"Accept: application/sdp\r\n\r\n"
        )
        resp = self._recv()
        resp_text = resp.decode(errors="replace")
        self._cseq += 1

        realm_match = re.search(r'realm="([^"]+)"', resp_text)
        nonce_match = re.search(r'nonce="([^"]+)"', resp_text)
        if not realm_match or not nonce_match:
            logger.error("No Digest auth challenge in DESCRIBE response")
            self.disconnect()
            return False

        self._realm = realm_match.group(1)
        self._nonce = nonce_match.group(1)
        logger.info("Received Digest challenge realm=%s", self._realm)

        # DESCRIBE with auth.
        auth = self._digest_auth("DESCRIBE", url)
        self._send(
            f"DESCRIBE {url} RTSP/1.0\r\n"
            f"CSeq: {self._cseq}\r\n"
            f"Accept: application/sdp\r\n"
            f"Authorization: {auth}\r\n\r\n"
        )
        resp = self._recv()
        resp_text = resp.decode(errors="replace")
        self._cseq += 1

        first_line = resp_text.splitlines()[0] if resp_text else ""
        if "200" not in first_line:
            logger.error("DESCRIBE failed: %s", first_line.strip())
            self.disconnect()
            return False
        logger.info("DESCRIBE OK for %s", url)

        # Extract SPS/PPS from SDP if available.
        self.sps_pps = b""
        sprop_match = re.search(r"sprop-parameter-sets=([^\s;]+)", resp_text)
        if sprop_match:
            for part in sprop_match.group(1).split(","):
                try:
                    nal = base64.b64decode(part)
                    self.sps_pps += b"\x00\x00\x00\x01" + nal
                except Exception as exc:
                    logger.debug("Failed to decode SPS/PPS block: %s", exc)
            logger.info("Extracted SPS/PPS from SDP (%d bytes)", len(self.sps_pps))

        # Find the video track control URL.
        tracks = []
        for line in resp_text.splitlines():
            if "a=control:" not in line or "*" in line:
                continue
            ctrl = line.split("a=control:", 1)[1].strip()
            tracks.append(ctrl if "rtsp://" in ctrl else f"{url}/{ctrl}")
        track = tracks[0] if tracks else f"{url}/track1"
        logger.info("Using RTSP control track %s", track)

        # SETUP with the transport string that works against the camera.
        auth = self._digest_auth("SETUP", track)
        self._send(
            f"SETUP {track} RTSP/1.0\r\n"
            f"CSeq: {self._cseq}\r\n"
            f"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n"
            f"Authorization: {auth}\r\n\r\n"
        )
        resp = self._recv()
        resp_text = resp.decode(errors="replace")
        self._cseq += 1

        first_line = resp_text.splitlines()[0] if resp_text else ""
        if "200" not in first_line:
            logger.error("SETUP failed: %s", first_line.strip())
            self.disconnect()
            return False

        transport_match = re.search(r"Transport:\s*(.+)", resp_text)
        if transport_match:
            logger.info("SETUP transport response: %s", transport_match.group(1).strip())

        session_match = re.search(r"Session:\s*([^\s;]+)", resp_text)
        self._session_id = session_match.group(1) if session_match else ""
        logger.info("SETUP OK (session=%s)", self._session_id or "<missing>")

        # PLAY. The camera may stream binary immediately and may not send a clean 200 OK.
        auth = self._digest_auth("PLAY", url)
        self._send(
            f"PLAY {url} RTSP/1.0\r\n"
            f"CSeq: {self._cseq}\r\n"
            f"Session: {self._session_id}\r\n"
            f"Range: npt=0.000-\r\n"
            f"Authorization: {auth}\r\n\r\n"
        )

        self._leftover = b""
        self._sock.settimeout(3)
        try:
            self._leftover = self._sock.recv(65536)
        except socket.timeout:
            logger.debug("PLAY response timeout; continuing because camera may stream immediately")

        if self._leftover.startswith(b"RTSP/") and b"\r\n\r\n" in self._leftover:
            header_end = self._leftover.index(b"\r\n\r\n") + 4
            play_header = self._leftover[:header_end].decode(errors="replace")
            logger.debug("PLAY response header:\n%s", play_header.rstrip())
            self._leftover = self._leftover[header_end:]

        self._sock.settimeout(5)
        self._connected = True
        logger.info("PLAY OK, streaming from %s", self.config.name)
        return True

    def disconnect(self) -> None:
        """Close the RTSP connection and reset connection state."""
        self._connected = False
        self._session_id = ""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def read_frames(
        self,
        callback: Callable[[bytes, bool], None],
        stop_check: Callable[[], bool] = lambda: False,
    ) -> None:
        """Read RTP packets and depacketize H264 NAL units.

        The callback receives Annex B encoded H264 access units. SPS/PPS is
        prepended to the first frame when it was advertised in SDP.

        Args:
            callback: Called with ``(nal_bytes, is_first_frame)`` for each frame.
            stop_check: Returns ``True`` when the receive loop should stop.
        """
        if not self._connected or self._sock is None:
            logger.debug("read_frames called while RTSP client is disconnected")
            return

        buf = self._leftover
        self._fua_buf = b""
        self._fua_header = 0
        self._nal_buffer = b""
        frame_count = 0

        while not stop_check():
            try:
                data = self._sock.recv(65536)
                if not data:
                    logger.info("RTSP socket closed by peer")
                    break
                buf += data
            except socket.timeout:
                continue
            except Exception as exc:
                logger.error("Socket error: %s", exc)
                break

            while len(buf) >= 4:
                # RTSP interleaved RTP frames begin with '$' (0x24).
                if buf[0] != 0x24:
                    idx = buf.find(b"\x24", 1)
                    if idx == -1:
                        logger.debug("Discarding %d non-interleaved bytes", len(buf))
                        buf = b""
                        break
                    logger.debug("Skipping %d bytes before next interleaved packet", idx)
                    buf = buf[idx:]
                    continue

                channel = buf[1]
                plen = struct.unpack("!H", buf[2:4])[0]
                if len(buf) < 4 + plen:
                    break

                if channel == 0 and plen > 12:
                    rtp = buf[4 : 4 + plen]
                    marker = (rtp[1] >> 7) & 1
                    payload = rtp[12:]

                    if payload:
                        nal_type = payload[0] & 0x1F

                        if 1 <= nal_type <= 23:
                            self._nal_buffer += b"\x00\x00\x00\x01" + payload
                        elif nal_type == 28 and len(payload) >= 2:
                            fu_indicator = payload[0]
                            fu_header = payload[1]
                            start = (fu_header >> 7) & 1
                            end = (fu_header >> 6) & 1
                            nal_t = fu_header & 0x1F
                            fragment = payload[2:]

                            if start:
                                self._fua_header = (fu_indicator & 0xE0) | nal_t
                                self._fua_buf = bytes([self._fua_header]) + fragment
                            elif self._fua_buf:
                                self._fua_buf += fragment

                            if end and self._fua_buf:
                                self._nal_buffer += b"\x00\x00\x00\x01" + self._fua_buf
                                self._fua_buf = b""
                        elif nal_type == 24:
                            offset = 1
                            while offset + 2 <= len(payload):
                                size = struct.unpack("!H", payload[offset : offset + 2])[0]
                                offset += 2
                                if offset + size <= len(payload):
                                    self._nal_buffer += b"\x00\x00\x00\x01" + payload[offset : offset + size]
                                offset += size

                    if marker and self._nal_buffer:
                        is_first = frame_count == 0
                        nal_data = self.sps_pps + self._nal_buffer if is_first else self._nal_buffer
                        callback(nal_data, is_first)
                        frame_count += 1
                        self._nal_buffer = b""

                buf = buf[4 + plen :]

        self.disconnect()
        logger.info("Stream ended after %d frames", frame_count)

    @staticmethod
    def probe_camera(host: str, port: int = 554, timeout: float = 3.0) -> Optional[dict]:
        """Probe a host for an RTSP service and likely working stream paths.

        The probe intentionally tolerates incomplete or non-standard responses so
        it can identify Yoosee/HIipCamera devices that still expose enough RTSP
        metadata to be usable.

        Returns:
            A dictionary containing ``host``, ``port``, ``realm``, ``server`` and
            ``paths`` when an RTSP endpoint is detected; otherwise ``None``.
        """
        info = {"host": host, "port": port, "realm": None, "server": None, "paths": []}

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((host, port))
        except Exception:
            return None

        try:
            sock.sendall(f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            sock.settimeout(timeout)
            resp = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    resp += chunk
                    if b"\r\n\r\n" in resp:
                        break
            except socket.timeout:
                logger.debug("OPTIONS probe timed out for %s:%s", host, port)

            resp_text = resp.decode(errors="replace")
            if "RTSP/" not in resp_text:
                return None

            server_match = re.search(r"Server:\s*(.+)", resp_text)
            if server_match:
                info["server"] = server_match.group(1).strip()

            realm_match = re.search(r'realm="([^"]+)"', resp_text)
            if realm_match:
                info["realm"] = realm_match.group(1)
        except Exception as exc:
            logger.debug("OPTIONS probe failed for %s:%s: %s", host, port, exc)
        finally:
            sock.close()

        for path in RTSPClient.COMMON_PATHS:
            url = f"rtsp://{host}:{port}{path}"
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((host, port))
                sock.sendall(
                    f"DESCRIBE {url} RTSP/1.0\r\n"
                    f"CSeq: 1\r\n"
                    f"Accept: application/sdp\r\n\r\n".encode()
                )

                resp = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        resp += chunk
                        if b"\r\n\r\n" in resp:
                            break
                except socket.timeout:
                    logger.debug("DESCRIBE probe timed out for %s", url)

                resp_text = resp.decode(errors="replace")
                first_line = resp_text.splitlines()[0] if resp_text else ""
                if "401" in first_line or "200" in first_line:
                    info["paths"].append(path)
                    if not info["realm"]:
                        realm_match = re.search(r'realm="([^"]+)"', resp_text)
                        if realm_match:
                            info["realm"] = realm_match.group(1)
                time.sleep(0.05)
            except Exception:
                logger.debug("Failed probing RTSP path %s", url)
            finally:
                try:
                    sock.close()
                except Exception:
                    pass

        if info["server"] == "RtspServer_0.0.0.2":
            logger.info("Detected Yoosee-style RTSP server at %s:%s", host, port)

        if info["paths"]:
            logger.info("RTSP probe found candidate paths for %s:%s: %s", host, port, info["paths"])

        return info if info["paths"] or info["server"] else None
