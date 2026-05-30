"""Recording file management — list, stream, download, delete."""
import logging
import time
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..state import RECORDINGS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["recordings"])


def _safe_recording_path(filename: str) -> Path:
    """Resolve a filename and ensure it stays within RECORDINGS_DIR."""
    # Block path separators to prevent traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    file_path = (RECORDINGS_DIR / filename).resolve()
    # Verify resolved path is still under recordings dir
    if not str(file_path).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(400, "Invalid filename")
    return file_path


@router.get("/recordings")
async def list_recordings():
    """List all local recording files."""
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for f in sorted(RECORDINGS_DIR.glob("*.mp4"),
                    key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        files.append({
            "name": f.name,
            "size": stat.st_size,
            "size_mb": round(stat.st_size / 1048576, 1),
            "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(stat.st_mtime)),
        })
    return files


@router.get("/recordings/{filename}")
async def stream_recording(filename: str):
    """Stream a recording file for in-browser playback."""
    file_path = _safe_recording_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Recording not found")
    return FileResponse(file_path, media_type="video/mp4")


@router.get("/recordings/{filename}/download")
async def download_recording(filename: str):
    """Download a recording file."""
    file_path = _safe_recording_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Recording not found")
    return FileResponse(file_path, media_type="video/mp4", filename=filename)


@router.delete("/recordings/{filename}")
async def delete_recording(filename: str):
    """Delete a recording file."""
    file_path = _safe_recording_path(filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Recording not found")
    file_path.unlink()
    return {"message": f"Deleted {filename}"}
