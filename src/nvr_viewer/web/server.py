"""Web server launcher for NVR Viewer."""
import uvicorn
import logging


def run(host: str = "0.0.0.0", port: int = 8080, reload: bool = False):
    """Start the NVR Viewer web server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"\n  NVR Viewer Web UI: http://localhost:{port}")
    print(f"  API Docs:          http://localhost:{port}/docs")
    print(f"  Press Ctrl+C to stop\n")

    uvicorn.run(
        "nvr_viewer.web.api:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


if __name__ == "__main__":
    run()
