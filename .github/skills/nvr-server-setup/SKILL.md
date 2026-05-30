---
name: nvr-server-setup
description: >
  Automated setup of NVR Viewer on a server (Linux/Windows/macOS). Installs dependencies,
  configures cameras and credentials, sets up notifications (email/webhook), enables the
  disk space guardian, installs the daemon service, and verifies everything is running.
  Covers the full path from fresh clone to 24/7 surveillance.
license: MIT
---

# NVR Server Setup Skill

Automates the full setup of NVR Viewer on a server so it runs 24/7 as a daemon with
notifications for detections, camera disconnects, and disk space warnings.

## When to use

- Setting up NVR Viewer on a new server or machine
- Reconfiguring an existing installation (cameras, notifications, storage)
- When the user says "set up NVR", "deploy NVR", "configure NVR server", or similar
- After a fresh clone of the nvr-viewer repository

## Goal

Get NVR Viewer running as a 24/7 daemon with:
1. All cameras configured and streaming
2. Detection enabled (motion + objects + faces)
3. Notifications configured (email and/or webhook)
4. Disk space guardian active (30% free minimum)
5. Service installed and auto-starting on boot

## Prerequisites

- Python 3.10+ installed
- Git installed
- Network access to cameras (same subnet)
- For email notifications: SMTP server credentials
- For webhook notifications: Slack/Discord/Teams webhook URL

## Steps

### Phase 1: Install & Verify

1. **Check Python version**
   ```
   python --version   # or python3 --version
   ```
   Must be 3.10+. If not, guide the user to install it.

2. **Clone and install**
   ```powershell
   # If not already cloned:
   git clone https://github.com/amwaleh/nvr-viewer.git
   cd nvr-viewer

   # Windows
   powershell -ExecutionPolicy Bypass -File scripts\install.ps1

   # Linux/macOS
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Verify installation**
   ```
   nvr-viewer --help
   ```

4. **Start the web server temporarily** (for configuration)
   ```
   nvr-viewer web --port 8080
   ```
   Verify it's accessible at `http://<server-ip>:8080`

### Phase 2: Configure Cameras

Ask the user for each camera:
- **Name** (e.g., "Front Gate", "Backyard")
- **Type**: RTSP or MJPEG
- **Connection details**: Host IP, port, path (RTSP) or stream URL (MJPEG)
- **Credentials**: Username and password

For each camera, use the API:

```bash
# Store credentials (encrypted)
curl -X POST http://localhost:8080/api/credentials \
  -H "Content-Type: application/json" \
  -d '{"host": "192.168.1.3", "username": "admin", "password": "SECRET"}'

# Add camera
curl -X POST http://localhost:8080/api/cameras \
  -H "Content-Type: application/json" \
  -d '{"name": "Front Gate", "host": "192.168.1.3", "port": 554, "path": "/onvif1", "username": "admin", "password": "SECRET", "type": "rtsp"}'

# Start the stream to verify connectivity
curl -X POST http://localhost:8080/api/stream/1/start
```

If the user doesn't know their camera details, run a network scan:
```bash
curl http://localhost:8080/api/scan
```

### Phase 3: Configure Detection

Enable detection types per camera or globally:

```bash
# Set global detection defaults
curl -X POST http://localhost:8080/api/detection \
  -H "Content-Type: application/json" \
  -d '{"motion": true, "objects": true, "faces": true}'
```

### Phase 4: Configure Storage

```bash
# Set storage directory (use a drive with plenty of space)
curl -X POST http://localhost:8080/api/settings/storage \
  -H "Content-Type: application/json" \
  -d '{"storage_dir": "/mnt/nvr-data"}'

# Set disk space guardian (keep 30% free)
curl -X POST http://localhost:8080/api/settings/disk-guard \
  -H "Content-Type: application/json" \
  -d '{"min_free_percent": 30}'
```

### Phase 5: Configure Notifications

Ask the user which notification channels they want:

#### Option A: Email (SMTP)

```bash
curl -X POST http://localhost:8080/api/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "email_enabled": true,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "your-email@gmail.com",
    "smtp_password": "your-app-password",
    "smtp_use_tls": true,
    "email_to": ["alert-recipient@gmail.com"],
    "notify_on_detection": true,
    "notify_detection_types": ["person", "face", "vehicle"],
    "notify_on_camera_disconnect": true,
    "notify_on_disk_warning": true,
    "notify_on_recording_paused": true,
    "cooldown_seconds": 60
  }'
```

#### Option B: Webhook (Slack/Discord/Teams)

```bash
curl -X POST http://localhost:8080/api/notifications \
  -H "Content-Type: application/json" \
  -d '{
    "enabled": true,
    "webhook_enabled": true,
    "webhook_url": "https://hooks.slack.com/services/YOUR/WEBHOOK/URL",
    "notify_on_detection": true,
    "notify_detection_types": ["person", "face", "vehicle"],
    "notify_on_camera_disconnect": true,
    "notify_on_disk_warning": true,
    "cooldown_seconds": 60
  }'
```

#### Verify notifications work

```bash
curl -X POST http://localhost:8080/api/notifications/test \
  -H "Content-Type: application/json" \
  -d '{"channel": "all"}'
```

### Phase 6: Install Service & Start Daemon

Stop the temporary web server, then install the daemon:

```bash
# Install as a system service
nvr-viewer service install --port 8080

# Start the service
nvr-viewer service start

# Verify it's running
nvr-viewer service status

# Check logs
nvr-viewer service logs
```

### Phase 7: Final Verification

Run a complete health check:

```bash
# Check system status
curl http://localhost:8080/api/status | python -m json.tool

# Verify cameras are connected
curl http://localhost:8080/api/cameras

# Verify detection is enabled
curl http://localhost:8080/api/detection

# Verify notifications are configured
curl http://localhost:8080/api/notifications

# Verify disk guardian is active
curl http://localhost:8080/api/settings/storage
```

Expected output should show:
- All cameras registered and streaming
- Detection enabled for motion, objects, faces
- Disk status: "healthy" with 30%+ free
- Notifications: enabled with at least one channel active
- Service: running

## Notification Events

| Event | When | Default |
|-------|------|---------|
| Detection alert | Person, face, or vehicle detected | ON |
| Camera disconnect | Camera stops responding | ON |
| Camera reconnect | Camera comes back online | ON |
| Disk space warning | Free space < configured threshold | ON |
| Recording paused | Disk critically low (< 2 GB) | ON |
| Recording resumed | Disk space recovered | ON |

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Camera not found on scan | Check camera is on same subnet, try manual IP |
| Stream won't connect | Verify credentials, try different RTSP path (/onvif1, /stream1, /h264) |
| Email not sending | Use app-specific password for Gmail, check SMTP port/TLS |
| Webhook not firing | Verify URL is correct, check server can reach the webhook endpoint |
| Service won't start | Check logs with `nvr-viewer service logs`, verify Python path |
| High disk usage | Lower min_free_percent, reduce camera resolution, or add storage |
