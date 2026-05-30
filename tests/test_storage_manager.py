"""Tests for StorageManager disk space guardian."""
import os
import pytest
import tempfile
import time
from pathlib import Path

from nvr_viewer.storage.manager import StorageManager


@pytest.fixture
def storage_dir():
    """Create a temp directory with subdirs mimicking NVR storage."""
    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        (base / "recordings").mkdir()
        (base / "snapshots").mkdir()
        (base / "clips").mkdir()
        yield base


class TestStorageManager:
    def test_get_disk_status(self, storage_dir):
        mgr = StorageManager(storage_dir, min_free_percent=30)
        status = mgr.get_disk_status()
        assert "disk_total_gb" in status
        assert "disk_free_gb" in status
        assert "disk_free_percent" in status
        assert status["min_free_percent"] == 30
        assert status["recording_paused"] is False
        assert status["status"] in ("healthy", "ok", "warning", "critical")

    def test_status_healthy_when_plenty_of_space(self, storage_dir):
        mgr = StorageManager(storage_dir, min_free_percent=5)
        status = mgr.get_disk_status()
        # Most dev machines have >5% free
        assert status["status"] in ("healthy", "ok")
        assert status["recording_paused"] is False

    def test_cleanup_deletes_oldest_first(self, storage_dir):
        rec_dir = storage_dir / "recordings"
        # Create 3 files with staggered mtimes
        files = []
        for i in range(3):
            f = rec_dir / f"rec_{i}.mp4"
            f.write_bytes(b"x" * 1024)
            # Set mtime: oldest first
            os.utime(f, (time.time() - (300 - i * 100),
                         time.time() - (300 - i * 100)))
            files.append(f)

        mgr = StorageManager(storage_dir, min_free_percent=30)
        # Ask to free 1500 bytes — should delete the 2 oldest (1024 each)
        freed = mgr._cleanup_oldest(1500)
        assert freed >= 1500
        # Oldest two should be gone
        assert not files[0].exists()
        assert not files[1].exists()
        # Newest should remain
        assert files[2].exists()

    def test_cleanup_across_subdirs(self, storage_dir):
        # Files in recordings and snapshots
        (storage_dir / "recordings" / "old_rec.mp4").write_bytes(b"a" * 512)
        snap = storage_dir / "snapshots" / "old_snap.jpg"
        snap.write_bytes(b"b" * 512)
        os.utime(snap, (time.time() - 600, time.time() - 600))

        clip = storage_dir / "clips" / "old_clip.mp4"
        clip.write_bytes(b"c" * 512)
        os.utime(clip, (time.time() - 1200, time.time() - 1200))

        mgr = StorageManager(storage_dir, min_free_percent=30)
        freed = mgr._cleanup_oldest(600)
        assert freed >= 512
        # The oldest file (clip at -1200s) should be deleted first
        assert not clip.exists()

    def test_cleanup_removes_empty_dirs(self, storage_dir):
        nested = storage_dir / "recordings" / "2026" / "05" / "30"
        nested.mkdir(parents=True)
        f = nested / "test.mp4"
        f.write_bytes(b"x" * 100)

        mgr = StorageManager(storage_dir, min_free_percent=30)
        mgr._cleanup_oldest(200)
        # File deleted
        assert not f.exists()
        # Empty parent dirs cleaned up
        assert not (storage_dir / "recordings" / "2026" / "05" / "30").exists()

    def test_check_and_cleanup_no_action_when_healthy(self, storage_dir):
        mgr = StorageManager(storage_dir, min_free_percent=1)
        result = mgr.check_and_cleanup()
        assert result["action"] == "none"

    def test_min_free_percent_clamped(self, storage_dir):
        mgr1 = StorageManager(storage_dir, min_free_percent=2)
        assert mgr1.min_free_percent == 5  # clamped to minimum 5

        mgr2 = StorageManager(storage_dir, min_free_percent=95)
        assert mgr2.min_free_percent == 80  # clamped to maximum 80

    def test_start_stop(self, storage_dir):
        mgr = StorageManager(storage_dir, min_free_percent=30,
                              check_interval=60)
        mgr.start()
        assert mgr._thread is not None
        assert mgr._thread.is_alive()
        mgr.stop()
        assert not mgr._thread.is_alive()

    def test_storage_used_calculation(self, storage_dir):
        (storage_dir / "recordings" / "a.mp4").write_bytes(b"x" * 1000)
        (storage_dir / "snapshots" / "b.jpg").write_bytes(b"y" * 500)

        mgr = StorageManager(storage_dir, min_free_percent=30)
        used = mgr._get_storage_used()
        assert used >= 1500
