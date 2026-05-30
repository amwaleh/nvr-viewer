"""Detection events — list, delete, serve snapshots and clips."""
import logging
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..state import db, SNAPSHOTS_DIR, CLIPS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["events"])


class DeleteEventsRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1)


def _safe_subpath(base_dir: Path, filepath: str) -> Path:
    """Resolve a sub-path and ensure it stays within base_dir."""
    resolved = (base_dir / filepath).resolve()
    if not str(resolved).startswith(str(base_dir.resolve())):
        raise HTTPException(400, "Invalid file path")
    return resolved


@router.get("/events")
async def list_events(
    camera_id: Optional[int] = None,
    detection_type: Optional[str] = None,
    since: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Query detection events with pagination."""
    events = db.get_events(camera_id=camera_id, detection_type=detection_type,
                           since=since, limit=limit, offset=offset)
    total = db.count_events(camera_id=camera_id, detection_type=detection_type,
                            since=since)
    for ev in events:
        sp = ev.get("snapshot_path")
        if sp and Path(sp).exists():
            try:
                rel = Path(sp).relative_to(SNAPSHOTS_DIR)
                ev["snapshot_url"] = f"/api/snapshots/{rel.as_posix()}"
            except ValueError:
                ev["snapshot_url"] = f"/api/snapshots/{Path(sp).name}"
        meta = ev.get("metadata", "")
        if meta and meta.endswith(".mp4"):
            try:
                rel = Path(meta).relative_to(CLIPS_DIR)
                ev["clip_url"] = f"/api/clips/{rel.as_posix()}"
            except ValueError:
                ev["clip_url"] = f"/api/clips/{Path(meta).name}"
    return {"events": events, "total": total, "limit": limit, "offset": offset}


@router.delete("/events")
async def delete_events(request: DeleteEventsRequest):
    """Delete detection events by IDs and their associated files."""
    events = db.get_events_by_ids(request.ids)
    deleted_files = 0
    for ev in events:
        sp = ev.get("snapshot_path")
        if sp:
            p = Path(sp)
            if p.exists():
                p.unlink()
                deleted_files += 1
        meta = ev.get("metadata", "")
        if meta and meta.endswith(".mp4"):
            p = Path(meta)
            if p.exists():
                p.unlink()
                deleted_files += 1
    count = db.delete_events(request.ids)
    return {"deleted": count, "files_removed": deleted_files}


@router.get("/snapshots/{filepath:path}")
async def get_snapshot(filepath: str):
    """Serve a detection snapshot image."""
    file_path = _safe_subpath(SNAPSHOTS_DIR, filepath)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Snapshot not found")
    return FileResponse(file_path, media_type="image/jpeg",
                        filename=file_path.name)


@router.get("/clips/{filepath:path}")
async def get_clip(filepath: str):
    """Serve a detection video clip."""
    file_path = _safe_subpath(CLIPS_DIR, filepath)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(404, "Clip not found")
    return FileResponse(file_path, media_type="video/mp4")
