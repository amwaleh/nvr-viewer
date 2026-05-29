"""Encrypted credential storage for camera passwords."""
import json
import os
from pathlib import Path
from typing import Optional
import logging
from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

CRED_DIR = Path.home() / ".nvr-viewer"
KEY_FILE = CRED_DIR / ".key"
CRED_FILE = CRED_DIR / "credentials.enc"


class CredentialStore:
    """Encrypted credential storage using Fernet symmetric encryption."""
    
    def __init__(self, cred_dir: Path = CRED_DIR):
        self._cred_dir = cred_dir
        self._cred_dir.mkdir(parents=True, exist_ok=True)
        self._key_file = self._cred_dir / ".key"
        self._cred_file = self._cred_dir / "credentials.enc"
        self._fernet = self._load_or_create_key()
        self._credentials: dict = self._load()
    
    def _load_or_create_key(self) -> Fernet:
        if self._key_file.exists():
            key = self._key_file.read_bytes()
        else:
            key = Fernet.generate_key()
            self._key_file.write_bytes(key)
            # Restrict permissions (best effort on Windows)
            try:
                os.chmod(self._key_file, 0o600)
            except Exception:
                pass
            logger.info("Generated new encryption key")
        return Fernet(key)
    
    def _load(self) -> dict:
        if not self._cred_file.exists():
            return {}
        try:
            encrypted = self._cred_file.read_bytes()
            decrypted = self._fernet.decrypt(encrypted)
            return json.loads(decrypted.decode())
        except Exception as e:
            logger.error(f"Failed to load credentials: {e}")
            return {}
    
    def _save(self):
        data = json.dumps(self._credentials).encode()
        encrypted = self._fernet.encrypt(data)
        self._cred_file.write_bytes(encrypted)
    
    def set(self, camera_host: str, username: str, password: str):
        """Store credentials for a camera host."""
        self._credentials[camera_host] = {"username": username, "password": password}
        self._save()
        logger.info(f"Saved credentials for {camera_host}")
    
    def get(self, camera_host: str) -> Optional[dict]:
        """Get credentials for a camera host. Returns {username, password} or None."""
        return self._credentials.get(camera_host)
    
    def delete(self, camera_host: str) -> bool:
        """Delete credentials for a camera host."""
        if camera_host in self._credentials:
            del self._credentials[camera_host]
            self._save()
            return True
        return False
    
    def list_hosts(self) -> list[str]:
        """List all hosts with stored credentials."""
        return list(self._credentials.keys())
