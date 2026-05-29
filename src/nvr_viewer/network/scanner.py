"""Local-network camera discovery utilities."""

from __future__ import annotations

import ipaddress
import logging
import platform
import re
import socket
import struct
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class NetworkScanner:
    """Discover RTSP and MJPEG cameras on the local network."""

    RTSP_PORT = 554
    MJPEG_PORTS = [8080, 8081, 8082, 80]
    ONVIF_MULTICAST_IP = "239.255.255.250"
    ONVIF_MULTICAST_PORT = 3702

    def get_local_subnet(self) -> str:
        """Detect the active local IPv4 subnet."""
        local_ip = self._get_local_ip()
        prefix_len = self._detect_prefix_length(local_ip)
        subnet = str(ipaddress.ip_network(f"{local_ip}/{prefix_len}", strict=False))
        logger.info("Detected local subnet %s from IP %s", subnet, local_ip)
        return subnet

    def scan_subnet(
        self,
        subnet: str,
        ports: list[int] | None = None,
        timeout: float = 1.0,
    ) -> list[dict]:
        """Scan a subnet for hosts responding on any requested TCP port.
        
        Returns list of dicts: [{host: str, open_ports: [int]}]
        """
        ports = ports or [self.RTSP_PORT]
        network = ipaddress.ip_network(subnet, strict=False)
        hosts = [str(ip) for ip in network.hosts()]
        if not hosts:
            logger.warning("Subnet %s has no usable hosts", subnet)
            return []

        worker_count = min(100, max(50, len(hosts)))
        logger.info(
            "Scanning %d hosts in %s across ports %s with %d workers",
            len(hosts),
            subnet,
            ports,
            worker_count,
        )

        responsive_hosts: dict[str, list[int]] = {}
        started_at = time.perf_counter()

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="nvr-scan") as executor:
            futures = {
                executor.submit(self._probe_host_ports, host, ports, timeout): host
                for host in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    open_ports = future.result()
                except Exception as exc:
                    logger.debug("Port scan failed for %s: %s", host, exc)
                    continue
                if open_ports:
                    responsive_hosts[host] = open_ports

        elapsed = time.perf_counter() - started_at
        result = sorted(
            [{"host": h, "open_ports": p} for h, p in responsive_hosts.items()],
            key=lambda x: self._host_sort_key(x["host"]),
        )
        logger.info(
            "Subnet scan complete: %d responsive hosts found in %.2fs",
            len(result),
            elapsed,
        )
        return result

    def discover_cameras(self, subnet: str | None = None) -> list[dict]:
        """Scan the local subnet and probe for RTSP and MJPEG cameras."""
        import http.client
        from ..core.rtsp_client import RTSPClient

        subnet = subnet or self.get_local_subnet()
        local_ip = self._get_local_ip()
        all_ports = [self.RTSP_PORT] + self.MJPEG_PORTS
        responsive = self.scan_subnet(subnet, ports=all_ports, timeout=1.0)
        if not responsive:
            logger.info("No responsive hosts found in %s", subnet)
            return []

        worker_count = min(32, max(4, len(responsive)))
        logger.info("Probing %d responsive hosts for cameras", len(responsive))

        cameras: list[dict] = []

        # Separate RTSP and MJPEG candidates, excluding our own IP
        rtsp_hosts = [r["host"] for r in responsive if self.RTSP_PORT in r["open_ports"]]
        mjpeg_candidates = [
            (r["host"], p)
            for r in responsive
            for p in r["open_ports"]
            if p != self.RTSP_PORT and r["host"] != local_ip
        ]

        # Probe RTSP cameras
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rtsp-probe") as executor:
            futures = {
                executor.submit(RTSPClient.probe_camera, host, self.RTSP_PORT, 2.0): host
                for host in rtsp_hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    camera_info = future.result()
                except Exception as exc:
                    logger.debug("RTSP probe failed for %s: %s", host, exc)
                    continue
                if camera_info:
                    camera_info["type"] = "rtsp"
                    cameras.append(camera_info)

        # Probe MJPEG cameras (check for Motion or generic MJPEG streams)
        rtsp_found = {c["host"] for c in cameras}
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="mjpeg-probe") as executor:
            futures = {
                executor.submit(self._probe_mjpeg, host, port, 3.0): (host, port)
                for host, port in mjpeg_candidates
                if host not in rtsp_found  # Skip hosts already found as RTSP
            }
            for future in as_completed(futures):
                host, port = futures[future]
                try:
                    mjpeg_info = future.result()
                except Exception as exc:
                    logger.debug("MJPEG probe failed for %s:%d: %s", host, port, exc)
                    continue
                if mjpeg_info:
                        # Deduplicate: prefer Motion-detected entry over raw MJPEG
                        existing_idx = next(
                            (i for i, c in enumerate(cameras) if c["host"] == host and c.get("type") == "mjpeg"),
                            None
                        )
                        if existing_idx is not None:
                            # Keep the one with "Motion" server (more specific)
                            if mjpeg_info.get("server") == "Motion":
                                cameras[existing_idx] = mjpeg_info
                        else:
                            cameras.append(mjpeg_info)

        cameras.sort(key=lambda item: self._host_sort_key(item["host"]))
        logger.info("Discovered %d camera(s) in %s", len(cameras), subnet)
        return cameras

    def discover_onvif(self, timeout: float = 3.0) -> list[dict]:
        """Best-effort ONVIF WS-Discovery probe over multicast."""
        message_id = f"uuid:{uuid.uuid4()}"
        probe = f"""<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>{message_id}</w:MessageID>
    <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body>
    <d:Probe>
      <d:Types>dn:NetworkVideoTransmitter</d:Types>
    </d:Probe>
  </e:Body>
</e:Envelope>"""

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.settimeout(timeout)
            self._configure_multicast_socket(sock)
            local_ip = self._get_local_ip()
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            except OSError:
                logger.debug("Could not bind ONVIF discovery to interface %s", local_ip)

            for _ in range(2):
                sock.sendto(
                    probe.encode("utf-8"),
                    (self.ONVIF_MULTICAST_IP, self.ONVIF_MULTICAST_PORT),
                )
                time.sleep(0.1)

            devices: dict[str, dict] = {}
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    remaining = max(0.1, deadline - time.monotonic())
                    sock.settimeout(remaining)
                    payload, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                except OSError as exc:
                    logger.debug("ONVIF discovery receive error: %s", exc)
                    break

                device = self._parse_onvif_response(payload, addr[0])
                if device:
                    devices[device["host"]] = device
        except OSError as exc:
            logger.warning("ONVIF discovery unavailable: %s", exc)
            return []
        finally:
            sock.close()

        discovered = sorted(devices.values(), key=lambda item: self._host_sort_key(item["host"]))
        logger.info("Discovered %d ONVIF device(s)", len(discovered))
        return discovered

    @staticmethod
    def _probe_host_ports(host: str, ports: list[int], timeout: float) -> list[int]:
        """Return list of open ports on the host."""
        open_ports = []
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                if sock.connect_ex((host, port)) == 0:
                    logger.debug("Host %s responded on port %d", host, port)
                    open_ports.append(port)
            except OSError as exc:
                logger.debug("Connect probe failed for %s:%d: %s", host, port, exc)
            finally:
                sock.close()
        return open_ports

    @staticmethod
    def _probe_host(host: str, ports: list[int], timeout: float) -> str | None:
        """Return the host if any requested port accepts a TCP connection."""
        for port in ports:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(timeout)
                if sock.connect_ex((host, port)) == 0:
                    logger.debug("Host %s responded on port %d", host, port)
                    return host
            except OSError as exc:
                logger.debug("Connect probe failed for %s:%d: %s", host, port, exc)
            finally:
                sock.close()
        return None

    @staticmethod
    def _probe_mjpeg(host: str, port: int, timeout: float) -> dict | None:
        """Probe an HTTP port to detect MJPEG camera streams.
        
        Checks for Motion web UI or generic MJPEG stream endpoints.
        Returns camera info dict or None.
        """
        import http.client

        probe_timeout = min(timeout, 2.0)

        try:
            # First check the root page for Motion or camera signature
            conn = http.client.HTTPConnection(host, port, timeout=probe_timeout)
            try:
                conn.request("GET", "/")
                resp = conn.getresponse()
                ct = resp.getheader("Content-Type", "")
                body = resp.read(2000).decode("utf-8", errors="ignore")
            except Exception:
                body = ""
                ct = ""
            finally:
                conn.close()

            # Check if root itself is an MJPEG stream
            if "multipart" in ct or "mjpeg" in ct.lower():
                stream_url = f"http://{host}:{port}/"
                logger.info("MJPEG stream at root %s:%d", host, port)
                return {
                    "host": host, "port": port, "name": f"MJPEG Camera ({host})",
                    "path": "", "type": "mjpeg", "stream_url": stream_url, "server": "MJPEG",
                }

            is_motion = "<title>Motion</title>" in body or "motion-project" in body
            # Quick check: if not Motion and no camera-related keywords, skip
            camera_keywords = ["camera", "stream", "video", "mjpeg", "surveillance", "ipcam", "webcam"]
            looks_like_camera = is_motion or any(kw in body.lower() for kw in camera_keywords)
            if not looks_like_camera:
                return None

            name = "Motion Camera" if is_motion else f"MJPEG Camera ({host})"

            if is_motion:
                # Motion streams on port+1 (web control on 8080, stream on 8081)
                stream_port = port + 1
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    sock.settimeout(1.0)
                    if sock.connect_ex((host, stream_port)) == 0:
                        stream_url = f"http://{host}:{stream_port}/0/stream"
                    else:
                        stream_url = f"http://{host}:{port}/0/stream"
                except OSError:
                    stream_url = f"http://{host}:{port}/0/stream"
                finally:
                    sock.close()
            else:
                # Try common MJPEG paths with short timeout
                stream_paths = ["/0/stream", "/video", "/mjpeg", "/stream"]
                stream_url = None
                for path in stream_paths:
                    try:
                        conn = http.client.HTTPConnection(host, port, timeout=1.5)
                        conn.request("GET", path)
                        resp = conn.getresponse()
                        path_ct = resp.getheader("Content-Type", "")
                        resp.read(128)
                        conn.close()
                        if "multipart" in path_ct or "mjpeg" in path_ct.lower() or "image/jpeg" in path_ct:
                            stream_url = f"http://{host}:{port}{path}"
                            break
                    except Exception:
                        pass
                    finally:
                        try: conn.close()
                        except Exception: pass

                if not stream_url:
                    return None

            logger.info("MJPEG camera found at %s:%d (stream: %s)", host, port, stream_url)
            return {
                "host": host, "port": port, "name": name,
                "path": "", "type": "mjpeg", "stream_url": stream_url,
                "server": "Motion" if is_motion else "MJPEG",
            }

        except Exception as exc:
            logger.debug("MJPEG probe error for %s:%d: %s", host, port, exc)
            return None

    @staticmethod
    def _get_local_ip() -> str:
        """Resolve the local IPv4 address used for outbound traffic."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                local_ip = sock.getsockname()[0]
                if local_ip and not local_ip.startswith("127."):
                    return local_ip
        except OSError:
            logger.debug("UDP-based local IP detection failed", exc_info=True)

        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            if local_ip:
                return local_ip
        except OSError:
            logger.debug("Hostname-based local IP detection failed", exc_info=True)

        logger.warning("Falling back to loopback address for subnet detection")
        return "127.0.0.1"

    def _detect_prefix_length(self, local_ip: str) -> int:
        """Best-effort detection of the subnet prefix length for the local IP."""
        detectors = [
            self._detect_prefix_from_ip_addr,
            self._detect_prefix_from_ipconfig,
            self._detect_prefix_from_ifconfig,
        ]
        if platform.system().lower() == "windows":
            detectors = [
                self._detect_prefix_from_ipconfig,
                self._detect_prefix_from_ip_addr,
                self._detect_prefix_from_ifconfig,
            ]

        for detector in detectors:
            prefix = detector(local_ip)
            if prefix:
                logger.debug("Detected prefix /%d for %s using %s", prefix, local_ip, detector.__name__)
                return prefix

        logger.warning("Unable to determine subnet mask for %s, defaulting to /24", local_ip)
        return 24

    @staticmethod
    def _detect_prefix_from_ipconfig(local_ip: str) -> int | None:
        """Parse Windows ipconfig output for the subnet mask."""
        try:
            result = subprocess.run(
                ["ipconfig"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None

        for block in re.split(r"\r?\n\r?\n", result.stdout):
            if local_ip not in block:
                continue
            match = re.search(r"Subnet Mask[ .]*:\s*([0-9.]+)", block, re.I)
            if match:
                return NetworkScanner._mask_to_prefix(match.group(1))
        return None

    @staticmethod
    def _detect_prefix_from_ip_addr(local_ip: str) -> int | None:
        """Parse `ip addr` output for prefix length."""
        try:
            result = subprocess.run(
                ["ip", "-o", "-f", "inet", "addr", "show"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None

        for line in result.stdout.splitlines():
            match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line)
            if match and match.group(1) == local_ip:
                return int(match.group(2))
        return None

    @staticmethod
    def _detect_prefix_from_ifconfig(local_ip: str) -> int | None:
        """Parse ifconfig output for dotted or hex netmasks."""
        try:
            result = subprocess.run(
                ["ifconfig"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=3,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return None

        for block in re.split(r"\n(?=\S)", result.stdout):
            if local_ip not in block:
                continue

            dotted = re.search(r"netmask\s+(\d+\.\d+\.\d+\.\d+)", block)
            if dotted:
                return NetworkScanner._mask_to_prefix(dotted.group(1))

            hex_mask = re.search(r"netmask\s+(0x[0-9a-fA-F]+)", block)
            if hex_mask:
                return NetworkScanner._mask_to_prefix(hex_mask.group(1))
        return None

    @staticmethod
    def _mask_to_prefix(mask: str) -> int:
        """Convert a dotted or hex netmask into a prefix length."""
        if mask.startswith("0x"):
            packed = struct.pack("!I", int(mask, 16))
            mask = socket.inet_ntoa(packed)
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen

    @staticmethod
    def _host_sort_key(host: str) -> tuple[int, int | str]:
        """Sort IP addresses numerically and fall back to lexical hostnames."""
        try:
            return (0, int(ipaddress.ip_address(host)))
        except ValueError:
            return (1, host)

    @staticmethod
    def _configure_multicast_socket(sock: socket.socket) -> None:
        """Apply best-effort multicast settings across Windows and Linux."""
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except OSError:
            logger.debug("SO_REUSEADDR not available for multicast socket")

        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                logger.debug("SO_REUSEPORT not available for multicast socket")

        try:
            sock.bind(("", 0))
        except OSError:
            sock.bind(("0.0.0.0", 0))

        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        except OSError:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("B", 2))

    @staticmethod
    def _parse_onvif_response(payload: bytes, source_ip: str) -> dict | None:
        """Extract ONVIF endpoint metadata from a WS-Discovery response."""
        try:
            text = payload.decode("utf-8", errors="ignore")
        except Exception:
            return None

        xaddrs_match = re.search(r"<(?:\w+:)?XAddrs>(.*?)</(?:\w+:)?XAddrs>", text, re.S)
        endpoint_match = re.search(r"<(?:\w+:)?Address>(.*?)</(?:\w+:)?Address>", text, re.S)
        scopes_match = re.search(r"<(?:\w+:)?Scopes[^>]*>(.*?)</(?:\w+:)?Scopes>", text, re.S)

        xaddrs = []
        if xaddrs_match:
            xaddrs = [url.strip() for url in xaddrs_match.group(1).split() if url.strip()]

        host = source_ip
        if xaddrs:
            parsed_host = urlparse(xaddrs[0]).hostname
            if parsed_host:
                host = parsed_host

        return {
            "host": host,
            "xaddrs": xaddrs,
            "endpoint": endpoint_match.group(1).strip() if endpoint_match else None,
            "scopes": scopes_match.group(1).split() if scopes_match else [],
        }


__all__ = ["NetworkScanner"]
