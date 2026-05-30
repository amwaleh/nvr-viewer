"""Timeline API — browse continuous recordings by camera, date, and hour."""
import logging
import os
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..state import db, RECORDINGS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/timeline", tags=["timeline"])


@router.get("/cameras")
async def timeline_cameras():
    """List cameras that have recordings."""
    rows = db.execute(
        """SELECT DISTINCT r.camera_id, c.name
           FROM recordings r
           LEFT JOIN cameras c ON c.id = r.camera_id
           ORDER BY c.name""")
    # Also scan filesystem for recordings not in DB
    db_cameras = {r["camera_id"]: r["name"] or f"Camera {r['camera_id']}" for r in rows}

    # Scan flat files in RECORDINGS_DIR
    if RECORDINGS_DIR.exists():
        for f in RECORDINGS_DIR.glob("*.mp4"):
            parts = f.stem.rsplit("_", 2)
            if len(parts) >= 3:
                cam_name = parts[0]
                if cam_name not in [v for v in db_cameras.values()]:
                    db_cameras[cam_name] = cam_name

    return [{"id": k, "name": v} for k, v in db_cameras.items()]


@router.get("/{camera_id}/dates")
async def timeline_dates(camera_id: int):
    """Get dates with recordings for a camera."""
    dates = db.get_recording_dates(camera_id)

    # Also scan filesystem for this camera's recordings
    fs_dates = set()
    cam_row = db.execute("SELECT name FROM cameras WHERE id = ?", (camera_id,))
    cam_name = cam_row[0]["name"] if cam_row else None
    if cam_name and RECORDINGS_DIR.exists():
        safe_name = cam_name.replace(" ", "_")
        for f in RECORDINGS_DIR.glob(f"{safe_name}_*.mp4"):
            # Parse date from filename: CameraName_YYYYMMDD_HHMMSS.mp4
            parts = f.stem.replace(safe_name + "_", "", 1)
            if len(parts) >= 8:
                date_str = f"{parts[:4]}-{parts[4:6]}-{parts[6:8]}"
                fs_dates.add(date_str)

    all_dates = sorted(set(dates) | fs_dates, reverse=True)
    return all_dates


@router.get("/{camera_id}/segments")
async def timeline_segments(camera_id: int, date: str):
    """Get recording segments for a camera on a specific date.

    Returns segments with hour grouping for timeline display.
    """
    # DB recordings
    segments = db.get_recordings_for_date(camera_id, date)

    # Also scan filesystem
    cam_row = db.execute("SELECT name FROM cameras WHERE id = ?", (camera_id,))
    cam_name = cam_row[0]["name"] if cam_row else None
    db_paths = {s["file_path"] for s in segments}

    if cam_name and RECORDINGS_DIR.exists():
        safe_name = cam_name.replace(" ", "_")
        date_compact = date.replace("-", "")
        for f in RECORDINGS_DIR.glob(f"{safe_name}_{date_compact}_*.mp4"):
            fpath = str(f)
            if fpath not in db_paths:
                parts = f.stem.replace(safe_name + "_", "", 1)
                time_part = parts[9:] if len(parts) >= 15 else "000000"
                hour = time_part[:2] if len(time_part) >= 2 else "00"
                minute = time_part[2:4] if len(time_part) >= 4 else "00"
                second = time_part[4:6] if len(time_part) >= 6 else "00"
                start = f"{date} {hour}:{minute}:{second}"
                stat = f.stat()
                segments.append({
                    "id": None,
                    "camera_id": camera_id,
                    "start_time": start,
                    "end_time": None,
                    "file_path": fpath,
                    "file_size": stat.st_size,
                    "trigger": "filesystem",
                })

    # Sort by start_time and group by hour
    segments.sort(key=lambda s: s["start_time"] or "")
    hours = {}
    for seg in segments:
        st = seg["start_time"] or ""
        h = st[11:13] if len(st) >= 13 else "00"
        if h not in hours:
            hours[h] = []
        # Add playback URL
        fp = seg["file_path"]
        fname = Path(fp).name if fp else ""
        seg["playback_url"] = f"/api/recordings/{fname}" if fname else ""
        seg["filename"] = fname
        hours[h].append(seg)

    return {"date": date, "camera_id": camera_id, "hours": hours, "total_segments": len(segments)}


@router.get("/play/{filename}")
async def play_segment(filename: str):
    """Stream a recording segment for playback."""
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    file_path = (RECORDINGS_DIR / filename).resolve()
    if not str(file_path).startswith(str(RECORDINGS_DIR.resolve())):
        raise HTTPException(400, "Invalid filename")
    if not file_path.exists():
        raise HTTPException(404, "Recording not found")
    return FileResponse(file_path, media_type="video/mp4")
