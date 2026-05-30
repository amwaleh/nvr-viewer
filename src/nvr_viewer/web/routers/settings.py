"""Settings — storage directory and credentials management."""
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import creds, update_storage_dir, storage_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])


# --- Models ---

class CredentialSet(BaseModel):
    host: str
    username: str = "admin"
    password: str = ""


class StorageSettings(BaseModel):
    storage_dir: str


# --- Credentials ---

@router.get("/credentials")
async def list_credentials():
    """List hosts with stored credentials (passwords hidden)."""
    hosts = creds.list_hosts()
    return [{"host": h, "username": creds.get(h)["username"]} for h in hosts]


@router.post("/credentials")
async def set_credentials(cred: CredentialSet):
    creds.set(cred.host, cred.username, cred.password)
    return {"message": f"Credentials saved for {cred.host}"}


@router.delete("/credentials/{host}")
async def delete_credentials(host: str):
    creds.delete(host)
    return {"message": f"Credentials removed for {host}"}


# --- Storage ---

@router.get("/settings/storage")
async def get_storage_settings():
    """Get current storage directory and disk status."""
    from ..state import STORAGE_DIR, min_free_percent
    return {
        "storage_dir": str(STORAGE_DIR),
        "min_free_percent": min_free_percent,
        "disk": storage_manager.get_disk_status(),
    }


@router.post("/settings/storage")
async def set_storage_settings(settings: StorageSettings):
    """Update storage directory."""
    new_dir = Path(settings.storage_dir)
    if not new_dir.is_absolute():
        raise HTTPException(400, "Storage directory must be an absolute path")
    update_storage_dir(new_dir)
    return {"message": "Storage directory updated", "storage_dir": str(new_dir)}


class DiskGuardSettings(BaseModel):
    min_free_percent: int


@router.post("/settings/disk-guard")
async def set_disk_guard(settings: DiskGuardSettings):
    """Update the minimum free disk space threshold (5-80%)."""
    import nvr_viewer.web.state as st
    pct = max(5, min(80, settings.min_free_percent))
    st.min_free_percent = pct
    st.storage_manager.min_free_percent = pct
    st.save_settings()
    return {
        "message": f"Disk guard set to {pct}% minimum free",
        "min_free_percent": pct,
        "disk": storage_manager.get_disk_status(),
    }
