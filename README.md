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
git clone https://github.com/amwaleh/nvr-viewer.git
cd nvr-viewer
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# Activate
.\.venv\Scripts\activate
```

### API / Web UI (Recommended)

```powershell
# Start the web server
nvr-viewer web --port 8080
```

Open **http://localhost:8080** in your browser.

**Endpoints:**

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/cameras` | List all cameras |
| `POST` | `/api/cameras` | Add a camera |
| `PUT` | `/api/cameras/{id}` | Update a camera |
| `DELETE` | `/api/cameras/{id}` | Delete a camera |
| `POST` | `/api/stream/{id}/start` | Start a camera stream |
| `POST` | `/api/stream/{id}/stop` | Stop a camera stream |
| `GET` | `/api/stream/{id}` | MJPEG stream feed |
| `GET` | `/api/snapshot/{id}` | Capture a snapshot |
| `POST` | `/api/record/{id}/start` | Start recording |
| `POST` | `/api/record/{id}/stop` | Stop recording |
| `GET` | `/api/recordings` | List recordings |
| `GET` | `/api/recordings/{file}` | Stream a recording |
| `DELETE` | `/api/recordings/{file}` | Delete a recording |
| `GET` | `/api/events` | List detection events |
| `DELETE` | `/api/events` | Delete events by IDs |
| `GET` | `/api/scan` | Scan network for cameras |
| `GET` | `/api/status` | System status |
| `GET/POST` | `/api/detection` | Global detection settings |
| `GET/POST/DELETE` | `/api/detection/{id}` | Per-camera detection |
| `GET/POST` | `/api/credentials` | Credential management |
| `GET/POST` | `/api/settings/storage` | Storage directory config |
| `POST` | `/api/settings/disk-guard` | Disk space threshold |
| `GET/POST` | `/api/notifications` | Notification settings |
| `POST` | `/api/notifications/test` | Send a test notification |

**Web UI features:**

- **Scan** your network for cameras (RTSP + MJPEG auto-detection)
- **Add/edit/delete** cameras manually
- **Live view** all camera streams in a grid
- **Toggle detection** (Motion, Objects, Faces) per camera
- **Record** streams to MP4 with in-browser playback
- **Browse events** in the paginated gallery at `/events`
- **View clips** — 10-second detection videos with bounding boxes

### Running as a Service / Daemon

Once you've configured your cameras and detection settings through the web UI,
install NVR Viewer as a background service so it runs 24/7 without a terminal window.

#### Step 1 — Install the service

```powershell
# Uses your current Python environment and settings automatically
nvr-viewer service install --port 8080
```

This creates a platform-native service that auto-starts on boot:

| Platform | What it creates | Config location |
|----------|----------------|-----------------|
| **Linux** | systemd unit at `/etc/systemd/system/nvr-viewer.service` | `sudo` required |
| **Windows** | NSSM service (or Task Scheduler if NSSM unavailable) | Run as Administrator |
| **macOS** | launchd agent at `~/Library/LaunchAgents/com.nvr-viewer.plist` | User-level |

> **Windows note:** For best results, install [NSSM](https://nssm.cc/download) first (`winget install nssm`).
> Without NSSM, the fallback Task Scheduler task runs at logon instead of boot.

#### Step 2 — Start the service

```powershell
nvr-viewer service start
```

The web UI is now available at `http://<your-ip>:8080` — open it from any device on your network (phone, tablet, another PC).

#### Step 3 — Verify it's running

```powershell
nvr-viewer service status
```

#### Managing the service

```powershell
# View recent logs
nvr-viewer service logs

# Follow logs in real-time (Ctrl+C to stop)
nvr-viewer service logs -f

# Stop the service
nvr-viewer service stop

# Remove the service entirely
nvr-viewer service uninstall
```

#### Platform-specific commands (alternative)

```bash
# Linux
sudo systemctl status nvr-viewer
sudo journalctl -u nvr-viewer -f

# Windows (NSSM)
nssm status nvr-viewer

# macOS
launchctl list com.nvr-viewer
```

### Notifications

NVR Viewer can alert you via **email** (SMTP) and/or **webhook** (Slack, Discord, Teams) when important events occur.

#### Supported Events

| Event | Trigger |
|-------|---------|
| **Detection** | Person, vehicle, face, or motion detected |
| **Camera disconnect** | Camera stream drops unexpectedly |
| **Disk warning** | Storage falls below the free-space threshold |
| **Recording paused** | Recording auto-paused due to critically low disk (<2 GB) |

#### Setup via API

```bash
# Configure email notifications
curl -X POST http://localhost:8080/api/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "you@gmail.com",
    "smtp_password": "app-password",
    "smtp_use_tls": true,
    "email_to": "alerts@example.com"
  }'

# Configure webhook (Slack / Discord / Teams)
curl -X POST http://localhost:8080/api/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "webhook_url": "https://hooks.slack.com/services/T.../B.../xxx"
  }'

# Send a test notification
curl -X POST http://localhost:8080/api/notifications/test
```

#### Configuration Options

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `false` | Master switch for all notifications |
| `smtp_host` | string | `""` | SMTP server hostname |
| `smtp_port` | int | `587` | SMTP port (587 for STARTTLS, 465 for SSL) |
| `smtp_user` | string | `""` | SMTP username / sender address |
| `smtp_password` | string | `""` | SMTP password or app password |
| `smtp_use_tls` | bool | `true` | Use STARTTLS encryption |
| `email_to` | string | `""` | Recipient email address |
| `webhook_url` | string | `""` | Webhook endpoint URL |
| `notify_on_detection` | bool | `true` | Alert on AI detections |
| `notify_detection_types` | list | `["person","vehicle"]` | Which detection types trigger alerts |
| `notify_on_camera_disconnect` | bool | `true` | Alert when a camera goes offline |
| `notify_on_disk_warning` | bool | `true` | Alert when disk space is low |
| `notify_on_recording_paused` | bool | `true` | Alert when recording auto-pauses |
| `cooldown_seconds` | int | `60` | Minimum seconds between repeated alerts of the same type |

#### Gmail Setup

1. Enable [2-Step Verification](https://myaccount.google.com/security) on your Google account
2. Generate an **App Password** at https://myaccount.google.com/apppasswords
3. Use `smtp.gmail.com`, port `587`, your Gmail as `smtp_user`, and the app password as `smtp_password`

#### Webhook Formats

The webhook payload is compatible with **Slack**, **Discord**, and **Microsoft Teams** Incoming Webhooks:

```json
{
  "text": "🚨 NVR Alert: Person detected on Front Door",
  "username": "NVR Viewer",
  "icon_emoji": ":rotating_light:"
}
```

Config is persisted at `~/.nvr-viewer/notifications.json`.

### CLI

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
│   ├── database.py     # Thread-safe SQLite database
│   ├── credentials.py  # Fernet-encrypted credential store
│   └── models.py       # Data models
├── web/
│   ├── api.py          # FastAPI app orchestrator (CORS, static, router includes)
│   ├── state.py        # Centralized mutable state (db, creds, streams, settings)
│   ├── streaming.py    # RTSP + MJPEG stream workers with detection
│   ├── server.py       # Uvicorn server launcher
│   ├── routers/
│   │   ├── cameras.py  # Camera CRUD, streaming, snapshots, recording
│   │   ├── recordings.py # Recording file list/stream/download/delete
│   │   ├── events.py   # Detection events + snapshot/clip serving
│   │   ├── detection.py # Global and per-camera detection settings
│   │   ├── settings.py # Storage dir + credential management
│   │   └── system.py   # Status, network scan, SD card access
│   ├── static/app.js   # Frontend SPA JavaScript
│   └── templates/      # HTML templates (index.html, events.html)
└── ui/
    └── viewer.py       # OpenCV multi-camera display
```

## Requirements

- Python 3.10+
- Windows 10/11 (tested), Linux (should work)

## Development

```powershell
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check src/ tests/
```

## Testing

77 tests covering:
- **Route inventory** — all API endpoints exist and respond
- **CRUD operations** — cameras, credentials, detection settings, storage
- **E2E workflows** — full camera lifecycle, events flow, recording lifecycle
- **Security** — path traversal protection, credential encryption, input validation

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
