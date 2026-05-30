"""Storage management with disk space monitoring and automatic cleanup.

Ensures a minimum percentage of disk space remains free by cleaning up
old recordings, snapshots, and clips when thresholds are breached.
"""
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default: keep at least 30% of disk free
DEFAULT_MIN_FREE_PERCENT = 30
# Check interval: every 5 minutes
CHECK_INTERVAL_SECONDS = 300
# Critical threshold: pause recording if less than 2 GB free
CRITICAL_FREE_BYTES = 2 * 1024 * 1024 * 1024


class StorageManager:
    """Monitors disk usage and enforces free-space policies.

    Runs a background thread that periodically checks disk space and
    cleans up the oldest files when usage exceeds the configured threshold.
    """

    def __init__(
        self,
        storage_dir: Path,
        min_free_percent: int = DEFAULT_MIN_FREE_PERCENT,
        check_interval: int = CHECK_INTERVAL_SECONDS,
        notifier=None,
    ):
        self.storage_dir = storage_dir
        self.min_free_percent = max(5, min(80, min_free_percent))
        self.check_interval = check_interval
        self.notifier = notifier

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recording_paused = False
        self._last_cleanup: Optional[str] = None
        self._bytes_cleaned_total = 0
        self._lock = threading.Lock()

    # --- Public API ---

    def start(self):
        """Start the background disk monitor."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="storage-manager"
        )
        self._thread.start()
        logger.info(
            "Storage manager started: min %d%% free on %s",
            self.min_free_percent, self.storage_dir,
        )

    def stop(self):
        """Stop the background monitor."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("Storage manager stopped")

    @property
    def recording_paused(self) -> bool:
        """True if recording should be paused due to low disk space."""
        return self._recording_paused

    def get_disk_status(self) -> dict:
        """Return current disk usage stats."""
        usage = shutil.disk_usage(self.storage_dir)
        free_percent = (usage.free / usage.total) * 100 if usage.total else 0
        storage_used = self._get_storage_used()

        return {
            "disk_total_gb": round(usage.total / (1024 ** 3), 1),
            "disk_free_gb": round(usage.free / (1024 ** 3), 1),
            "disk_free_percent": round(free_percent, 1),
            "min_free_percent": self.min_free_percent,
            "storage_used_gb": round(storage_used / (1024 ** 3), 2),
            "recording_paused": self._recording_paused,
            "last_cleanup": self._last_cleanup,
            "bytes_cleaned_total": self._bytes_cleaned_total,
            "status": self._status_label(free_percent),
        }

    def check_and_cleanup(self) -> dict:
        """Run a single check-and-cleanup cycle. Returns cleanup summary."""
        usage = shutil.disk_usage(self.storage_dir)
        free_percent = (usage.free / usage.total) * 100 if usage.total else 0

        # Critical check: pause recording if dangerously low
        if usage.free < CRITICAL_FREE_BYTES:
            if not self._recording_paused:
                self._recording_paused = True
                logger.warning(
                    "CRITICAL: Only %.1f GB free — recording paused",
                    usage.free / (1024 ** 3),
                )
                if self.notifier:
                    self.notifier.notify_recording_paused("disk space critically low")
        elif free_percent > self.min_free_percent + 5:
            # Resume once we have comfortable headroom (5% above threshold)
            if self._recording_paused:
                self._recording_paused = False
                logger.info("Disk space recovered — recording resumed")
                if self.notifier:
                    self.notifier.notify_recording_resumed()

        # Warn if below threshold (but not yet critical)
        if free_percent < self.min_free_percent and not self._recording_paused:
            if self.notifier:
                self.notifier.notify_disk_warning(
                    free_percent, usage.free / (1024 ** 3))

        # Cleanup if below threshold
        if free_percent < self.min_free_percent:
            target_free = (self.min_free_percent / 100) * usage.total
            bytes_to_free = int(target_free - usage.free)
            logger.info(
                "Disk %.1f%% free (target %d%%) — need to free %.1f MB",
                free_percent, self.min_free_percent,
                bytes_to_free / (1024 ** 2),
            )
            freed = self._cleanup_oldest(bytes_to_free)
            self._last_cleanup = datetime.now().isoformat()
            self._bytes_cleaned_total += freed
            return {
                "action": "cleanup",
                "freed_mb": round(freed / (1024 ** 2), 1),
                "target_mb": round(bytes_to_free / (1024 ** 2), 1),
            }

        return {"action": "none", "free_percent": round(free_percent, 1)}

    # --- Internal ---

    def _monitor_loop(self):
        """Background loop that periodically checks disk space."""
        while not self._stop_event.is_set():
            try:
                self.check_and_cleanup()
            except Exception:
                logger.exception("Storage manager check failed")
            self._stop_event.wait(self.check_interval)

    def _status_label(self, free_percent: float) -> str:
        if self._recording_paused:
            return "critical"
        if free_percent < self.min_free_percent:
            return "warning"
        if free_percent < self.min_free_percent + 10:
            return "ok"
        return "healthy"

    def _get_storage_used(self) -> int:
        """Total bytes used by NVR storage directories."""
        total = 0
        for dirpath, _, filenames in os.walk(self.storage_dir):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        return total

    def _cleanup_oldest(self, bytes_to_free: int) -> int:
        """Delete oldest files across recordings/snapshots/clips until
        enough space is freed. Returns total bytes freed."""
        # Collect all media files with their modification times
        media_files = []
        for subdir in ["recordings", "snapshots", "clips"]:
            target = self.storage_dir / subdir
            if not target.exists():
                continue
            for f in target.rglob("*"):
                if f.is_file():
                    try:
                        stat = f.stat()
                        media_files.append((stat.st_mtime, stat.st_size, f))
                    except OSError:
                        pass

        # Sort oldest first
        media_files.sort(key=lambda x: x[0])

        freed = 0
        deleted_count = 0
        for mtime, size, filepath in media_files:
            if freed >= bytes_to_free:
                break
            try:
                filepath.unlink()
                freed += size
                deleted_count += 1
                logger.debug("Cleaned up: %s (%.1f MB)", filepath.name,
                             size / (1024 ** 2))
            except OSError as e:
                logger.warning("Failed to delete %s: %s", filepath, e)

        # Remove empty directories
        for subdir in ["recordings", "snapshots", "clips"]:
            target = self.storage_dir / subdir
            if not target.exists():
                continue
            for d in sorted(target.rglob("*"), reverse=True):
                if d.is_dir():
                    try:
                        d.rmdir()  # only removes empty dirs
                    except OSError:
                        pass

        if deleted_count:
            logger.info(
                "Cleanup complete: deleted %d files, freed %.1f MB",
                deleted_count, freed / (1024 ** 2),
            )
        return freed
