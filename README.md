# NVR Viewer

Network Video Recorder with camera auto-detection, recording, and AI-powered detection.

## Features

- **Web UI** — Dark-themed SPA with live camera grid, camera management, detection toggles, and events gallery
- **Camera Auto-Detection** — Scans your network for RTSP and MJPEG cameras, probes common paths, works with Yoosee/HIipCamera, Motion, and standard ONVIF cameras
- **MJPEG Support** — Connect HTTP MJPEG cameras (Motion, IP Webcam, etc.) alongside RTSP cameras
- **Live Viewing** — Multi-camera live view in browser or OpenCV GUI
- **Recording** — Record streams directly to MP4 files (manual or motion-triggered)
- **SD Card Access** — List and download recordings from camera SD cards
- **Motion Detection** — Background subtraction-based motion detection (MOG2)
- **Object Detection** — YOLOv8s-powered person, animal, vehicle, and object detection
- **Face Detection** — YuNet (ONNX) face detection with Haar cascade fallback
- **Events Gallery** — Paginated event browser with filters, thumbnails, lightbox, and detection clips
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
```

### Web UI (Recommended)

```powershell
# Start the web interface
nvr-viewer web --port 8080
```

Open **http://localhost:8080** in your browser. From the web UI you can:

- **Scan** your network for cameras (RTSP + MJPEG auto-detection)
- **Add/delete** cameras manually
- **Live view** all camera streams in a grid
- **Toggle detection** (Motion, Objects, Faces) per camera
- **Browse events** in the paginated gallery at **http://localhost:8080/events**
- **View clips** — 10-second detection videos with bounding boxes

### CLI (Advanced)

```powershell
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
│   ├── scanner.py      # Network camera auto-detection (RTSP + MJPEG)
│   └── sdcard.py       # SD card file access
├── detection/
│   ├── motion.py       # Motion detection (MOG2, downscaled)
│   ├── detector.py     # YOLO object + face detection
│   └── events.py       # Event processing, clip recording, deduplication
├── storage/
│   ├── database.py     # SQLite database
│   ├── credentials.py  # Encrypted credential store
│   └── models.py       # Data models
├── web/
│   ├── api.py          # FastAPI backend (streaming, detection, camera CRUD)
│   ├── static/app.js   # Frontend SPA JavaScript
│   └── templates/      # HTML templates (index.html, events.html)
└── ui/
    └── viewer.py       # OpenCV multi-camera display
```

## Requirements

- Python 3.10+
- Windows 10/11 (tested), Linux (should work)

## Camera Compatibility

**Tested:**
- Yoosee / Jortan cameras (RTSP server: `RtspServer_0.0.0.2`)
- Motion (Linux MJPEG camera server)

**Should work with:**
- Any camera with RTSP on port 554
- ONVIF-compatible cameras
- Any HTTP MJPEG stream source (IP Webcam, Motion, etc.)

---

## Connecting Cameras

### 1. Linux — Motion (Webcam / USB Camera)

[Motion](https://motion-project.github.io/) turns any USB or built-in camera into a network MJPEG stream.

#### Install

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y motion

# Fedora
sudo dnf install motion
```

#### Configure

Edit `/etc/motion/motion.conf` (or `~/.motion/motion.conf`):

```ini
# Stream settings
stream_port 8081
stream_localhost off
stream_maxrate 15
stream_quality 75

# Web control (optional)
webcontrol_port 8080
webcontrol_localhost off

# Video device
videodevice /dev/video0
width 1280
height 720
framerate 15

# Disable file output (NVR handles recording)
output_pictures off
ffmpeg_output_movies off
```

#### Start

```bash
# Foreground (testing)
motion

# As a service
sudo systemctl enable --now motion
```

#### Add to NVR Viewer

The camera will be auto-discovered when you click **Scan Network** in the web UI. It will show up with an orange **MJPEG** badge.

To add manually, go to **Cameras → Add Camera**, select **MJPEG (HTTP)** type, and enter:

```
Stream URL: http://<LINUX_IP>:8081/0/stream
```

---

### 2. Android — IP Webcam App

[IP Webcam](https://play.google.com/store/apps/details?id=com.pas.webcam) turns your Android phone into a network camera with MJPEG and RTSP streams.

#### Setup

1. Install **IP Webcam** from Google Play Store
2. Open the app → scroll to the bottom → tap **Start server**
3. Note the IP address shown (e.g., `http://192.168.1.100:8080`)

#### Recommended Settings

| Setting | Value |
|---------|-------|
| Video resolution | 1280×720 |
| Video quality | 60–80% |
| Orientation | Landscape |
| Audio mode | Disabled (saves bandwidth) |

#### Add to NVR Viewer

**Option A — MJPEG (simpler, auto-detected by scan):**

Select **MJPEG (HTTP)** type in Add Camera:

```
Stream URL: http://<PHONE_IP>:8080/video
```

**Option B — RTSP (lower latency):**

Select **RTSP** type in Add Camera:

```
Host: <PHONE_IP>
Port: 8080
Path: /h264_ulaw.sdp
```

> **Tip:** Enable "Prevent display from dimming" in IP Webcam settings for continuous use. Connect the phone to a charger.

---

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
