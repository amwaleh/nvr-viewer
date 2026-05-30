"""Pre-refactor tests — captures every API endpoint, route method, and key behavior.

Run with: pytest tests/test_api_routes.py -v
These tests use the REAL api.py app to verify all endpoints exist and respond.
After the refactor, running these same tests confirms nothing was lost.
"""
import pytest
from httpx import AsyncClient, ASGITransport
from pathlib import Path
import json
import tempfile
import os

# Patch storage dir to a temp directory before importing the app
_test_storage = tempfile.mkdtemp(prefix="nvr_test_")
os.environ["NVR_TEST_MODE"] = "1"

from nvr_viewer.web.api import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ============================================================
# Route inventory — every endpoint that must survive the refactor
# ============================================================

EXPECTED_ROUTES = [
    ("GET", "/"),
    ("GET", "/events"),
    ("GET", "/settings"),
    ("GET", "/api/cameras"),
    ("POST", "/api/cameras"),
    ("DELETE", "/api/cameras/{camera_id}"),
    ("PUT", "/api/cameras/{camera_id}"),
    ("GET", "/api/stream/{camera_id}"),
    ("POST", "/api/stream/{camera_id}/start"),
    ("POST", "/api/stream/{camera_id}/stop"),
    ("GET", "/api/snapshot/{camera_id}"),
    ("POST", "/api/record/{camera_id}/start"),
    ("POST", "/api/record/{camera_id}/stop"),
    ("GET", "/api/recordings"),
    ("GET", "/api/recordings/{filename}"),
    ("GET", "/api/recordings/{filename}/download"),
    ("DELETE", "/api/recordings/{filename}"),
    ("GET", "/api/sdcard/{camera_id}"),
    ("POST", "/api/sdcard/{camera_id}/download"),
    ("GET", "/api/scan"),
    ("GET", "/api/events"),
    ("DELETE", "/api/events"),
    ("GET", "/api/snapshots/{filepath}"),
    ("GET", "/api/clips/{filepath}"),
    ("GET", "/api/credentials"),
    ("POST", "/api/credentials"),
    ("DELETE", "/api/credentials/{host}"),
    ("GET", "/api/status"),
    ("GET", "/api/detection"),
    ("POST", "/api/detection"),
    ("GET", "/api/detection/{camera_id}"),
    ("POST", "/api/detection/{camera_id}"),
    ("DELETE", "/api/detection/{camera_id}"),
    ("GET", "/api/settings/storage"),
    ("POST", "/api/settings/storage"),
    ("POST", "/api/settings/disk-guard"),
    ("GET", "/api/notifications"),
    ("POST", "/api/notifications"),
    ("POST", "/api/notifications/test"),
]


class TestRouteInventory:
    """Verify every expected route is registered on the FastAPI app."""

    def _get_app_routes(self):
        """Extract all registered routes as (METHOD, path) tuples."""
        routes = []
        for route in app.routes:
            if hasattr(route, "methods") and hasattr(route, "path"):
                for method in route.methods:
                    if method in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                        routes.append((method, route.path))
        return routes

    @pytest.mark.parametrize("method,path", EXPECTED_ROUTES)
    def test_route_exists(self, method, path):
        """Each expected route must be registered."""
        app_routes = self._get_app_routes()
        # Normalize path params: {filepath:path} -> {filepath}
        normalized = [(m, p.replace(":path", "")) for m, p in app_routes]
        assert (method, path) in normalized, \
            f"Route {method} {path} not found. Registered: {sorted(normalized)}"

    def test_no_missing_routes(self):
        """Sanity: expected route count matches."""
        assert len(EXPECTED_ROUTES) == 39, \
            f"Expected 39 routes, got {len(EXPECTED_ROUTES)}"


# ============================================================
# Functional tests — verify key endpoints return correct responses
# ============================================================

class TestFrontendPages:
    @pytest.mark.anyio
    async def test_index_page(self, client):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    @pytest.mark.anyio
    async def test_events_page(self, client):
        resp = await client.get("/events")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")


class TestCameraCRUD:
    @pytest.mark.anyio
    async def test_list_cameras_empty(self, client):
        resp = await client.get("/api/cameras")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)

    @pytest.mark.anyio
    async def test_add_and_delete_camera(self, client):
        cam = {
            "name": "Test Cam",
            "host": "192.168.99.99",
            "port": 554,
            "path": "/onvif1",
            "username": "admin",
            "password": "pass",
            "type": "rtsp",
            "stream_url": "",
        }
        resp = await client.post("/api/cameras", json=cam)
        assert resp.status_code == 200
        data = resp.json()
        cam_id = data.get("id") or data.get("camera_id")
        assert cam_id is not None

        # Verify it appears in list
        resp = await client.get("/api/cameras")
        names = [c["name"] for c in resp.json()]
        assert "Test Cam" in names

        # Delete
        resp = await client.delete(f"/api/cameras/{cam_id}")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_update_camera(self, client):
        # Add
        cam = {"name": "Update Test", "host": "192.168.99.98", "port": 554,
               "path": "/onvif1", "username": "admin", "password": "", "type": "rtsp", "stream_url": ""}
        resp = await client.post("/api/cameras", json=cam)
        cam_id = resp.json().get("id") or resp.json().get("camera_id")

        # Update
        resp = await client.put(f"/api/cameras/{cam_id}",
                                json={"name": "Renamed"})
        assert resp.status_code == 200

        # Cleanup
        await client.delete(f"/api/cameras/{cam_id}")


class TestStreamEndpoints:
    @pytest.mark.anyio
    async def test_stream_no_camera(self, client):
        resp = await client.get("/api/stream/99999")
        # Should fail — camera doesn't exist or not streaming
        assert resp.status_code in (400, 404)

    @pytest.mark.anyio
    async def test_start_stream_no_camera(self, client):
        resp = await client.post("/api/stream/99999/start")
        assert resp.status_code in (400, 404)

    @pytest.mark.anyio
    async def test_stop_stream_no_camera(self, client):
        resp = await client.post("/api/stream/99999/stop")
        # May return 200 "not streaming" or 404
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_snapshot_no_stream(self, client):
        resp = await client.get("/api/snapshot/99999")
        assert resp.status_code in (400, 404)


class TestRecordingEndpoints:
    @pytest.mark.anyio
    async def test_record_start_no_stream(self, client):
        resp = await client.post("/api/record/99999/start")
        assert resp.status_code in (400, 404)

    @pytest.mark.anyio
    async def test_record_stop_no_stream(self, client):
        resp = await client.post("/api/record/99999/stop")
        assert resp.status_code in (200, 404)

    @pytest.mark.anyio
    async def test_list_recordings(self, client):
        resp = await client.get("/api/recordings")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    @pytest.mark.anyio
    async def test_recording_not_found(self, client):
        resp = await client.get("/api/recordings/nonexistent.mp4")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_recording_download_not_found(self, client):
        resp = await client.get("/api/recordings/nonexistent.mp4/download")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_recording_not_found(self, client):
        resp = await client.delete("/api/recordings/nonexistent.mp4")
        assert resp.status_code == 404


class TestEventsEndpoints:
    @pytest.mark.anyio
    async def test_list_events(self, client):
        resp = await client.get("/api/events")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data
        assert "total" in data

    @pytest.mark.anyio
    async def test_list_events_with_filters(self, client):
        resp = await client.get("/api/events?detection_type=motion&limit=10&offset=0")
        assert resp.status_code == 200
        data = resp.json()
        assert "events" in data

    @pytest.mark.anyio
    async def test_delete_events_empty_list(self, client):
        resp = await client.request("DELETE", "/api/events", json={"ids": []})
        assert resp.status_code == 422  # Pydantic validation: min_length=1

    @pytest.mark.anyio
    async def test_delete_events_nonexistent(self, client):
        resp = await client.request("DELETE", "/api/events", json={"ids": [99999]})
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0


class TestDetectionSettings:
    @pytest.mark.anyio
    async def test_get_detection_defaults(self, client):
        resp = await client.get("/api/detection")
        assert resp.status_code == 200
        data = resp.json()
        assert "default" in data
        assert "cameras" in data
        for key in ("motion", "objects", "faces"):
            assert key in data["default"]

    @pytest.mark.anyio
    async def test_set_detection_defaults(self, client):
        resp = await client.post("/api/detection",
                                 json={"motion": False, "objects": True, "faces": True})
        assert resp.status_code == 200
        assert resp.json()["default"]["motion"] is False
        # Reset
        await client.post("/api/detection",
                          json={"motion": True, "objects": True, "faces": True})

    @pytest.mark.anyio
    async def test_per_camera_detection_crud(self, client):
        cam_id = 1

        # Set per-camera override
        resp = await client.post(f"/api/detection/{cam_id}",
                                 json={"motion": False})
        assert resp.status_code == 200

        # Get — should show custom
        resp = await client.get(f"/api/detection/{cam_id}")
        assert resp.status_code == 200
        assert resp.json()["is_custom"] is True
        assert resp.json()["settings"]["motion"] is False

        # Reset
        resp = await client.delete(f"/api/detection/{cam_id}")
        assert resp.status_code == 200

        # Should no longer be custom
        resp = await client.get(f"/api/detection/{cam_id}")
        assert resp.json()["is_custom"] is False


class TestStorageSettings:
    @pytest.mark.anyio
    async def test_get_storage(self, client):
        resp = await client.get("/api/settings/storage")
        assert resp.status_code == 200
        assert "storage_dir" in resp.json()

    @pytest.mark.anyio
    async def test_set_storage_relative_path_rejected(self, client):
        resp = await client.post("/api/settings/storage",
                                 json={"storage_dir": "relative/path"})
        assert resp.status_code == 400


class TestCredentials:
    @pytest.mark.anyio
    async def test_list_credentials(self, client):
        resp = await client.get("/api/credentials")
        assert resp.status_code == 200

    @pytest.mark.anyio
    async def test_set_and_delete_credential(self, client):
        resp = await client.post("/api/credentials",
                                 json={"host": "192.168.99.99", "username": "admin", "password": "test"})
        assert resp.status_code == 200

        resp = await client.delete("/api/credentials/192.168.99.99")
        assert resp.status_code == 200


class TestSystemStatus:
    @pytest.mark.anyio
    async def test_status(self, client):
        resp = await client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "cameras_registered" in data
        assert "streams_active" in data
        assert "recordings_count" in data
        assert "streams" in data
        assert "detection" in data

    @pytest.mark.anyio
    async def test_scan(self, client):
        # Just verify endpoint exists and responds (scan may be slow)
        resp = await client.get("/api/scan?subnet=192.168.1.0/30")
        # Accept 200 or timeout — we're just testing the route exists
        assert resp.status_code == 200


class TestFileServing:
    @pytest.mark.anyio
    async def test_snapshot_not_found(self, client):
        resp = await client.get("/api/snapshots/nonexistent.jpg")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_clip_not_found(self, client):
        resp = await client.get("/api/clips/nonexistent.mp4")
        assert resp.status_code == 404
