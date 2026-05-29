"""SD card file access via camera HTTP/RTSP interfaces."""
import socket
import re
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class SDCardAccess:
    """Access SD card files on Yoosee/IP cameras.

    Most Yoosee cameras store recordings on the SD card but don't expose
    them via standard protocols. This module attempts common access methods:
    1. RTSP playback of recorded files (if supported)
    2. HTTP file listing (common on some brands)
    3. ONVIF recording search (if camera supports it)
    """

    def __init__(self, host: str, port: int = 554, username: str = "admin",
                 password: str = ""):
        self.host = host
        self.port = port
        self.username = username
        self.password = password

    def list_files(self) -> list[dict]:
        """Try to list SD card files via available methods.

        Returns list of dicts: [{name, path, size, date}]
        """
        # Try HTTP port 80
        files = self._try_http_listing()
        if files:
            return files

        # Try HTTP port 8080
        files = self._try_http_listing(port=8080)
        if files:
            return files

        logger.warning(f"SD card listing not available on {self.host} — "
                       "camera may not support remote file access")
        return []

    def _try_http_listing(self, port: int = 80) -> list[dict]:
        """Try HTTP-based SD card file listing."""
        common_paths = [
            "/sd/",
            "/sdcard/",
            "/cgi-bin/sdcard.cgi",
            "/tmpfs/sd/",
        ]

        for path in common_paths:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(3)
                sock.connect((self.host, port))

                req = (f"GET {path} HTTP/1.1\r\n"
                       f"Host: {self.host}\r\n"
                       f"Connection: close\r\n\r\n")
                sock.sendall(req.encode())

                resp = b""
                try:
                    while True:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        resp += chunk
                except socket.timeout:
                    pass
                sock.close()

                resp_text = resp.decode(errors="replace")
                if "200 OK" in resp_text:
                    return self._parse_file_listing(resp_text)

            except Exception:
                continue

        return []

    @staticmethod
    def _parse_file_listing(html: str) -> list[dict]:
        """Parse directory listing HTML into file dicts."""
        files = []
        # Common patterns in camera directory listings
        for m in re.finditer(r'href="([^"]+\.(mp4|avi|h264|jpg))"', html, re.I):
            files.append({
                "name": m.group(1).split("/")[-1],
                "path": m.group(1),
                "size": 0,
                "date": "",
            })
        return files

    def download_file(self, remote_path: str, local_path: str,
                      port: int = 80) -> bool:
        """Download a file from the camera's SD card via HTTP."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((self.host, port))

            req = (f"GET {remote_path} HTTP/1.1\r\n"
                   f"Host: {self.host}\r\n"
                   f"Connection: close\r\n\r\n")
            sock.sendall(req.encode())

            resp = b""
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                resp += chunk
            sock.close()

            if b"200 OK" not in resp[:100]:
                logger.error(f"Download failed: HTTP error")
                return False

            # Split header and body
            if b"\r\n\r\n" in resp:
                body = resp.split(b"\r\n\r\n", 1)[1]
            else:
                body = resp

            Path(local_path).parent.mkdir(parents=True, exist_ok=True)
            Path(local_path).write_bytes(body)
            logger.info(f"Downloaded {remote_path} -> {local_path} ({len(body)} bytes)")
            return True

        except Exception as e:
            logger.error(f"Download error: {e}")
            return False
