"""Service/daemon management router."""
import io
import logging
import platform
import contextlib
from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from ...daemon import (
    service_install, service_uninstall, service_start,
    service_stop, service_status, service_logs,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["service"])


class ServiceInstallRequest(BaseModel):
    port: int = 8080
    host: str = "0.0.0.0"


def _capture_output(func, *args, **kwargs) -> str:
    """Run a function and capture its print output."""
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            func(*args, **kwargs)
    except SystemExit:
        pass
    except Exception as e:
        buf.write(f"\n[ERROR] {e}\n")
    return buf.getvalue()


@router.get("/service/status")
async def get_service_status():
    """Get the current service/daemon status."""
    output = _capture_output(service_status)
    plat = platform.system().lower()
    plat_name = {"linux": "linux", "darwin": "macos", "windows": "windows"}.get(plat, plat)
    return {"platform": plat_name, "output": output.strip()}


@router.post("/service/install")
async def install_service(req: Optional[ServiceInstallRequest] = None):
    """Install NVR Viewer as a system service."""
    port = req.port if req else 8080
    host = req.host if req else "0.0.0.0"
    output = _capture_output(service_install, port, host)
    return {"action": "install", "output": output.strip()}


@router.post("/service/uninstall")
async def uninstall_service():
    """Uninstall the NVR Viewer system service."""
    output = _capture_output(service_uninstall)
    return {"action": "uninstall", "output": output.strip()}


@router.post("/service/start")
async def start_service():
    """Start the NVR Viewer system service."""
    output = _capture_output(service_start)
    return {"action": "start", "output": output.strip()}


@router.post("/service/stop")
async def stop_service():
    """Stop the NVR Viewer system service."""
    output = _capture_output(service_stop)
    return {"action": "stop", "output": output.strip()}


@router.get("/service/logs")
async def get_service_logs():
    """Get recent service logs."""
    output = _capture_output(service_logs, follow=False, lines=50)
    return {"output": output.strip()}
