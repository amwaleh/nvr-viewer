"""Settings — storage directory and credentials management."""
import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..state import (db, creds, STORAGE_DIR, update_storage_dir)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["settings"])


# --- Credentials ---

@router.get("/credentials")
async def list_credentials():
    return creds.list_hosts()


@router.post("/credentials")
async def set_credentials(cred: CredentialSet):
    creds.set(cred.host, cred.username, cred.password)
    return {"message": f"Credentials saved for {cred.host}"}


@router.delete("/credentials/{host}")
async def delete_credentials(host: str):
    creds.delete(host)
    return {"message": f"Credentials removed for {host}"}


# --- Storage ---

class StorageSettings(BaseModel):
    storage_dir: str


class CredentialSet(BaseModel):
    host: str
    username: str = "admin"
    password: str = ""


@router.get("/settings/storage")
async def get_storage_settings():
    """Get current storage directory."""
    from ..state import STORAGE_DIR
    return {"storage_dir": str(STORAGE_DIR)}


@router.post("/settings/storage")
async def set_storage_settings(settings: StorageSettings):
    """Update storage directory."""
    new_dir = Path(settings.storage_dir)
    if not new_dir.is_absolute():
        raise HTTPException(400, "Storage directory must be an absolute path")
    update_storage_dir(new_dir)
    return {"message": "Storage directory updated", "storage_dir": str(new_dir)}
