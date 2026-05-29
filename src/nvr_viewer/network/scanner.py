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
    """Discover RTSP and ONVIF cameras on the local network."""

    RTSP_PORT = 554
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
    ) -> list[str]:
        """Scan a subnet for hosts responding on any requested TCP port."""
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

        responsive_hosts: set[str] = set()
        started_at = time.perf_counter()

        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="nvr-scan") as executor:
            futures = {
                executor.submit(self._probe_host, host, ports, timeout): host
                for host in hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    logger.debug("Port scan failed for %s: %s", host, exc)
                    continue
                if result:
                    responsive_hosts.add(result)

        elapsed = time.perf_counter() - started_at
        ordered_hosts = sorted(responsive_hosts, key=self._host_sort_key)
        logger.info(
            "Subnet scan complete: %d responsive hosts found in %.2fs",
            len(ordered_hosts),
            elapsed,
        )
        return ordered_hosts

    def discover_cameras(self, subnet: str | None = None) -> list[dict]:
        """Scan the local subnet and probe each RTSP-responsive host."""
        from ..core.rtsp_client import RTSPClient

        subnet = subnet or self.get_local_subnet()
        responsive_hosts = self.scan_subnet(subnet, ports=[self.RTSP_PORT], timeout=1.0)
        if not responsive_hosts:
            logger.info("No RTSP-responsive hosts found in %s", subnet)
            return []

        worker_count = min(32, max(4, len(responsive_hosts)))
        logger.info("Probing %d responsive hosts for RTSP cameras", len(responsive_hosts))

        cameras: list[dict] = []
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="rtsp-probe") as executor:
            futures = {
                executor.submit(RTSPClient.probe_camera, host, self.RTSP_PORT, 2.0): host
                for host in responsive_hosts
            }
            for future in as_completed(futures):
                host = futures[future]
                try:
                    camera_info = future.result()
                except Exception as exc:
                    logger.debug("RTSP probe failed for %s: %s", host, exc)
                    continue
                if camera_info:
                    cameras.append(camera_info)

        cameras.sort(key=lambda item: self._host_sort_key(item["host"]))
        logger.info("Discovered %d RTSP camera(s) in %s", len(cameras), subnet)
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
