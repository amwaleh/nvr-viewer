# NVR Viewer

Network Video Recorder with camera auto-detection, recording, and AI-powered detection.

## Features

- **Camera Auto-Detection** — Scans your network for RTSP cameras (port 554), probes common paths, works with Yoosee/HIipCamera and standard ONVIF cameras
- **Live Viewing** — Multi-camera live view with OpenCV GUI
- **Recording** — Record streams directly to MP4 files (manual or motion-triggered)
- **SD Card Access** — List and download recordings from camera SD cards
- **Motion Detection** — Background subtraction-based motion detection (MOG2)
- **Object Detection** — YOLOv8-powered person, animal, vehicle, and object detection
- **Face Detection** — OpenCV DNN/Haar cascade face detection
- **Detection Database** — All detection events logged to SQLite with timestamps, confidence, bounding boxes, and snapshots
- **Encrypted Credentials** — Camera passwords stored with Fernet encryption

## Quick Start

```powershell
# Install
git clone https://github.com/alexmwaleh/nvr-viewer.git
cd nvr-viewer
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# Activate
.\.venv\Scripts\activate

# Scan for cameras
nvr-viewer scan

# Store credentials
nvr-viewer creds set --host 192.168.1.3 -u admin -p YOUR_PASSWORD

# View cameras (auto-discover)
nvr-viewer view --discover

# View specific cameras
nvr-viewer view -c 192.168.1.3 -c 192.168.1.5 -u admin -p YOUR_PASSWORD

# View with object detection
nvr-viewer view --discover --detect-objects --detect-faces

# View with recording
nvr-viewer view -c 192.168.1.3 -p PASSWORD --record

# Query detection events
nvr-viewer events --type person --limit 20
```

## Keyboard Controls (Live Viewer)

| Key | Action |
|-----|--------|
| `q` | Quit |
| `r` | Toggle recording |
| `d` | Toggle YOLO object detection |
| `f` | Toggle face detection |
| `s` | Screenshot all cameras |

## Architecture

```
src/nvr_viewer/
├── app.py              # Main application controller
├── __main__.py         # CLI entry point
├── core/
│   ├── rtsp_client.py  # Manual RTSP handshake (non-standard camera support)
│   ├── decoder.py      # H264 decoding via PyAV
│   └── recorder.py     # MP4 stream recording
├── network/
│   ├── scanner.py      # Network camera auto-detection
│   └── sdcard.py       # SD card file access
├── detection/
│   ├── motion.py       # Motion detection (MOG2)
│   ├── detector.py     # YOLO object + face detection
│   └── events.py       # Event processing and deduplication
├── storage/
│   ├── database.py     # SQLite database
│   ├── credentials.py  # Encrypted credential store
│   └── models.py       # Data models
└── ui/
    └── viewer.py       # OpenCV multi-camera display
```

## Requirements

- Python 3.10+
- Windows 10/11 (tested), Linux (should work)

## Camera Compatibility

**Tested:**
- Yoosee / Jortan cameras (RTSP server: `RtspServer_0.0.0.2`)

**Should work with:**
- Any camera with RTSP on port 554
- ONVIF-compatible cameras

## Data Storage

All data stored in `~/.nvr-viewer/`:
- `nvr_viewer.db` — SQLite database (cameras, detection events, recordings)
- `credentials.enc` — Encrypted camera credentials
- `.key` — Fernet encryption key
- `recordings/` — Recorded MP4 files
- `snapshots/` — Detection event snapshots
- `models/` — YOLO model weights

## License

MIT
