"""End-to-end workflow tests — simulate real user journeys across multiple endpoints.

These tests verify that multi-step operations work correctly when chained together,
catching integration issues that single-endpoint tests miss.
"""
import pytest
from httpx import AsyncClient, ASGITransport
import tempfile
import os

from nvr_viewer.web.api import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestCameraLifecycle:
    """E2E: Add camera → list → update → start stream (fails, no real camera)
    → stop → delete → verify gone."""

    @pytest.mark.anyio
    async def test_full_camera_lifecycle(self, client):
        # 1. Start with known state
        initial = await client.get("/api/cameras")
        initial_count = len(initial.json())

        # 2. Add camera
        resp = await client.post("/api/cameras", json={
            "name": "E2E Test Cam",
            "host": "10.0.0.99",
            "port": 554,
            "path": "/onvif1",
            "username": "admin",
            "password": "test123",
            "type": "rtsp",
            "stream_url": "",
        })
        assert resp.status_code == 200
        cam_id = resp.json()["id"]

        # 3. Verify it shows in list
        resp = await client.get("/api/cameras")
        cameras = resp.json()
        cam = next((c for c in cameras if c["id"] == cam_id), None)
        assert cam is not None
        assert cam["name"] == "E2E Test Cam"
        assert cam["host"] == "10.0.0.99"

        # 4. Update the name
        resp = await client.put(f"/api/cameras/{cam_id}",
                                json={"name": "Renamed E2E Cam"})
        assert resp.status_code == 200

        # 5. Verify rename
        resp = await client.get("/api/cameras")
        cam = next(c for c in resp.json() if c["id"] == cam_id)
        assert cam["name"] == "Renamed E2E Cam"

        # 6. Try to stream — should fail (no real camera)
        resp = await client.post(f"/api/stream/{cam_id}/start")
        # Will start connecting but we can stop immediately
        resp = await client.post(f"/api/stream/{cam_id}/stop")
        assert resp.status_code == 200

        # 7. Try snapshot — should fail (not streaming)
        resp = await client.get(f"/api/snapshot/{cam_id}")
        assert resp.status_code in (400, 404)

        # 8. Try record — should fail (not streaming)
        resp = await client.post(f"/api/record/{cam_id}/start")
        assert resp.status_code in (400, 404)

        # 9. Delete camera
        resp = await client.delete(f"/api/cameras/{cam_id}")
        assert resp.status_code == 200

        # 10. Verify it's gone
        resp = await client.get("/api/cameras")
        ids = [c["id"] for c in resp.json()]
        assert cam_id not in ids


class TestDetectionSettingsFlow:
    """E2E: Set defaults → set per-camera overrides → verify effective →
    reset camera → verify reverts."""

    @pytest.mark.anyio
    async def test_detection_settings_flow(self, client):
        # 1. Get initial defaults
        resp = await client.get("/api/detection")
        assert resp.status_code == 200
        original_defaults = resp.json()["default"].copy()

        # 2. Change defaults — disable motion
        resp = await client.post("/api/detection",
                                 json={"motion": False, "objects": True, "faces": False})
        assert resp.status_code == 200
        assert resp.json()["default"]["motion"] is False
        assert resp.json()["default"]["faces"] is False

        # 3. Set per-camera override for camera 42 — re-enable motion
        resp = await client.post("/api/detection/42",
                                 json={"motion": True})
        assert resp.status_code == 200

        # 4. Verify effective settings for camera 42
        resp = await client.get("/api/detection/42")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_custom"] is True
        assert data["settings"]["motion"] is True   # overridden
        assert data["settings"]["faces"] is False    # inherited from default

        # 5. Verify camera 99 (no override) inherits defaults
        resp = await client.get("/api/detection/99")
        assert resp.json()["is_custom"] is False
        assert resp.json()["settings"]["motion"] is False  # from default

        # 6. Reset camera 42 to defaults
        resp = await client.delete("/api/detection/42")
        assert resp.status_code == 200

        # 7. Camera 42 should now match defaults
        resp = await client.get("/api/detection/42")
        assert resp.json()["is_custom"] is False
        assert resp.json()["settings"]["motion"] is False  # now from default

        # 8. Verify it shows in the cameras list on GET /api/detection
        resp = await client.get("/api/detection")
        assert "42" not in resp.json()["cameras"]

        # 9. Restore original defaults
        await client.post("/api/detection", json=original_defaults)


class TestEventsFlow:
    """E2E: List events → verify structure → bulk delete with non-existent IDs."""

    @pytest.mark.anyio
    async def test_events_list_and_delete(self, client):
        # 1. List all events
        resp = await client.get("/api/events?limit=5&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "total" in data
        assert "limit" in data
        assert "offset" in data
        assert data["limit"] == 5
        assert data["offset"] == 0

        # 2. Filter by type
        resp = await client.get("/api/events?detection_type=motion&limit=5")
        assert resp.status_code == 200

        # 3. Attempt bulk delete of non-existent IDs
        resp = await client.request("DELETE", "/api/events",
                                    json={"ids": [999998, 999999]})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0
        assert resp.json()["files_removed"] == 0

        # 4. Empty list should be rejected
        resp = await client.request("DELETE", "/api/events", json={"ids": []})
        assert resp.status_code == 422  # Pydantic validation: min_length=1


class TestRecordingsFlow:
    """E2E: List recordings → try play/download non-existent → delete non-existent."""

    @pytest.mark.anyio
    async def test_recordings_flow(self, client):
        # 1. List recordings
        resp = await client.get("/api/recordings")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

        # 2. Try to stream non-existent
        resp = await client.get("/api/recordings/fake_recording.mp4")
        assert resp.status_code == 404

        # 3. Try to download non-existent
        resp = await client.get("/api/recordings/fake_recording.mp4/download")
        assert resp.status_code == 404

        # 4. Try to delete non-existent
        resp = await client.delete("/api/recordings/fake_recording.mp4")
        assert resp.status_code == 404


class TestRecordingsCRUD:
    """E2E: Create a real recording file → list → stream → download → delete → verify gone."""

    @pytest.mark.anyio
    async def test_recording_full_lifecycle(self, client):
        from nvr_viewer.web.state import RECORDINGS_DIR

        # Create a real test mp4 file in the recordings directory
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        test_file = RECORDINGS_DIR / "e2e_test_recording.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70" + b"\x00" * 100)  # minimal mp4-like

        try:
            # 1. List should include our file
            resp = await client.get("/api/recordings")
            assert resp.status_code == 200
            recordings = resp.json()
            names = [r["name"] for r in recordings]
            assert "e2e_test_recording.mp4" in names

            # Verify response shape has no server paths
            rec = next(r for r in recordings if r["name"] == "e2e_test_recording.mp4")
            assert "name" in rec
            assert "size" in rec
            assert "size_mb" in rec
            assert "modified" in rec
            assert "path" not in rec  # server path must not be exposed

            # 2. Stream (inline playback) should return 200
            resp = await client.get("/api/recordings/e2e_test_recording.mp4")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "video/mp4"
            # No content-disposition means inline (not forced download)
            assert "attachment" not in resp.headers.get("content-disposition", "")

            # 3. Download should return 200 with attachment disposition
            resp = await client.get("/api/recordings/e2e_test_recording.mp4/download")
            assert resp.status_code == 200
            assert "attachment" in resp.headers.get("content-disposition", "")
            assert "e2e_test_recording.mp4" in resp.headers["content-disposition"]

            # 4. Delete should succeed
            resp = await client.delete("/api/recordings/e2e_test_recording.mp4")
            assert resp.status_code == 200
            assert "Deleted" in resp.json()["message"]

            # 5. File should be gone from disk
            assert not test_file.exists()

            # 6. List should no longer include it
            resp = await client.get("/api/recordings")
            names = [r["name"] for r in resp.json()]
            assert "e2e_test_recording.mp4" not in names

            # 7. Attempting to delete again should 404
            resp = await client.delete("/api/recordings/e2e_test_recording.mp4")
            assert resp.status_code == 404

        finally:
            # Cleanup if test failed before delete
            if test_file.exists():
                test_file.unlink()

    @pytest.mark.anyio
    async def test_recording_delete_multiple_sequential(self, client):
        """Create multiple recordings, delete each, verify independent."""
        from nvr_viewer.web.state import RECORDINGS_DIR
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

        files = []
        for i in range(3):
            f = RECORDINGS_DIR / f"e2e_multi_{i}.mp4"
            f.write_bytes(b"\x00" * 50)
            files.append(f)

        try:
            # All three should be listed
            resp = await client.get("/api/recordings")
            names = [r["name"] for r in resp.json()]
            for f in files:
                assert f.name in names

            # Delete the middle one
            resp = await client.delete("/api/recordings/e2e_multi_1.mp4")
            assert resp.status_code == 200
            assert not files[1].exists()

            # Others still exist
            resp = await client.get("/api/recordings")
            names = [r["name"] for r in resp.json()]
            assert "e2e_multi_0.mp4" in names
            assert "e2e_multi_1.mp4" not in names
            assert "e2e_multi_2.mp4" in names

            # Delete remaining
            for name in ["e2e_multi_0.mp4", "e2e_multi_2.mp4"]:
                resp = await client.delete(f"/api/recordings/{name}")
                assert resp.status_code == 200

            # All gone
            resp = await client.get("/api/recordings")
            names = [r["name"] for r in resp.json()]
            for f in files:
                assert f.name not in names

        finally:
            for f in files:
                if f.exists():
                    f.unlink()

    @pytest.mark.anyio
    async def test_recording_path_traversal_blocked(self, client):
        """Path traversal attempts should be rejected."""
        # Payloads with slashes: FastAPI routes them to non-existent paths → 404
        slash_payloads = [
            "../etc/passwd",
            "subdir/file.mp4",
            "../../secret.mp4",
            "....//....//etc/passwd",
        ]
        for payload in slash_payloads:
            resp = await client.get(f"/api/recordings/{payload}")
            assert resp.status_code in (400, 404), f"Slash traversal not blocked: {payload}"

        # Payloads our guard catches directly → 400
        guard_payloads = [
            "..\\windows\\system32",
            "..secret.mp4",       # contains ".."
            "...mp4",             # contains ".."
        ]
        for payload in guard_payloads:
            for method in ["get", "delete"]:
                func = getattr(client, method)
                resp = await func(f"/api/recordings/{payload}")
                assert resp.status_code == 400, f"{method.upper()} traversal not blocked: {payload}"

    @pytest.mark.anyio
    async def test_recording_special_characters_in_name(self, client):
        """Filenames with spaces and special chars should work."""
        from nvr_viewer.web.state import RECORDINGS_DIR
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

        test_name = "cam 1 - 2026-05-30 13_45_00.mp4"
        test_file = RECORDINGS_DIR / test_name
        test_file.write_bytes(b"\x00" * 50)

        try:
            # Should appear in listing
            resp = await client.get("/api/recordings")
            names = [r["name"] for r in resp.json()]
            assert test_name in names

            # Should be deletable via URL-encoded name
            from urllib.parse import quote
            resp = await client.delete(f"/api/recordings/{quote(test_name)}")
            assert resp.status_code == 200
            assert not test_file.exists()
        finally:
            if test_file.exists():
                test_file.unlink()


class TestStorageSettingsFlow:
    """E2E: Get storage → change to temp dir → verify → reject relative path."""

    @pytest.mark.anyio
    async def test_storage_settings_flow(self, client):
        # 1. Get current storage
        resp = await client.get("/api/settings/storage")
        assert resp.status_code == 200
        original_dir = resp.json()["storage_dir"]

        # 2. Change to a temp directory
        test_dir = tempfile.mkdtemp(prefix="nvr_e2e_storage_")
        resp = await client.post("/api/settings/storage",
                                 json={"storage_dir": test_dir})
        assert resp.status_code == 200
        assert resp.json()["storage_dir"] == test_dir

        # 3. Verify it persisted
        resp = await client.get("/api/settings/storage")
        assert resp.json()["storage_dir"] == test_dir

        # 4. Relative path should be rejected
        resp = await client.post("/api/settings/storage",
                                 json={"storage_dir": "not/absolute"})
        assert resp.status_code == 400

        # 5. Restore original
        await client.post("/api/settings/storage",
                          json={"storage_dir": original_dir})


class TestCredentialsFlow:
    """E2E: Add credential → list → verify present → delete → verify gone."""

    @pytest.mark.anyio
    async def test_credentials_lifecycle(self, client):
        host = "192.168.200.200"

        # 1. Add
        resp = await client.post("/api/credentials",
                                 json={"host": host, "username": "admin",
                                       "password": "secret"})
        assert resp.status_code == 200

        # 2. List and verify
        resp = await client.get("/api/credentials")
        assert resp.status_code == 200
        hosts = resp.json()
        assert host in [h if isinstance(h, str) else h.get("host", "") for h in hosts]

        # 3. Delete
        resp = await client.delete(f"/api/credentials/{host}")
        assert resp.status_code == 200


class TestStatusAndFrontend:
    """E2E: Load frontend pages → check status → verify structure."""

    @pytest.mark.anyio
    async def test_frontend_and_status(self, client):
        # 1. Index page loads
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

        # 2. Events gallery loads
        resp = await client.get("/events")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

        # 3. Status endpoint returns correct structure
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["cameras_registered"], int)
        assert isinstance(data["streams_active"], int)
        assert isinstance(data["recordings_count"], int)
        assert isinstance(data["streams"], dict)
        assert isinstance(data["detection"], dict)
        for key in ("motion", "objects", "faces"):
            assert key in data["detection"]


class TestMultiCameraDetection:
    """E2E: Add 2 cameras → set different detection per camera →
    verify isolation → clean up."""

    @pytest.mark.anyio
    async def test_per_camera_detection_isolation(self, client):
        # 1. Add two cameras
        r1 = await client.post("/api/cameras", json={
            "name": "Front Door", "host": "10.0.0.1", "port": 554,
            "path": "/onvif1", "username": "a", "password": "b",
            "type": "rtsp", "stream_url": ""})
        r2 = await client.post("/api/cameras", json={
            "name": "Backyard", "host": "10.0.0.2", "port": 554,
            "path": "/onvif1", "username": "a", "password": "b",
            "type": "rtsp", "stream_url": ""})
        id1 = r1.json()["id"]
        id2 = r2.json()["id"]

        try:
            # 2. Set Front Door: motion only
            await client.post(f"/api/detection/{id1}",
                              json={"motion": True, "objects": False, "faces": False})
            # 3. Set Backyard: objects + faces only
            await client.post(f"/api/detection/{id2}",
                              json={"motion": False, "objects": True, "faces": True})

            # 4. Verify isolation
            r1_det = (await client.get(f"/api/detection/{id1}")).json()
            r2_det = (await client.get(f"/api/detection/{id2}")).json()

            assert r1_det["settings"]["motion"] is True
            assert r1_det["settings"]["objects"] is False
            assert r2_det["settings"]["motion"] is False
            assert r2_det["settings"]["faces"] is True

            # 5. Reset one — should not affect other
            await client.delete(f"/api/detection/{id1}")
            r1_det = (await client.get(f"/api/detection/{id1}")).json()
            r2_det = (await client.get(f"/api/detection/{id2}")).json()
            assert r1_det["is_custom"] is False
            assert r2_det["is_custom"] is True

        finally:
            # Cleanup
            await client.delete(f"/api/detection/{id1}")
            await client.delete(f"/api/detection/{id2}")
            await client.delete(f"/api/cameras/{id1}")
            await client.delete(f"/api/cameras/{id2}")
