# Building Your Own NVR: A Developer's Guide to Network Video Recording

*How I turned cheap IP cameras and a Python backend into a full-featured surveillance system — and what I learned about video streaming along the way.*

---

## The $2,000 Problem

Commercial NVR systems are expensive. A decent 4-channel unit runs $200–$500, and that's before you add cameras, storage, and the inevitable subscription for "cloud features." Meanwhile, most of us already have IP cameras sitting on our networks — budget cameras, repurposed webcams, or that smart doorbell you hacked the RTSP stream out of.

What if you could build your own NVR for free?

That's exactly what I did. The result is [nvr-viewer](https://github.com/amwaleh/nvr-viewer) — a Python-powered NVR that runs on any machine, supports AI-powered detection, and gives you a timeline playback experience rivaling commercial systems.

But this article isn't just about the app. It's about **understanding the technology** that makes video surveillance work.

---

## Understanding Video Streams: RTSP vs. MJPEG

Every IP camera speaks one (or both) of two protocols. Understanding the difference is crucial.

### RTSP (Real-Time Streaming Protocol)

RTSP is the industry standard for IP cameras. Think of it as "HTTP for video" — it negotiates a session, then streams compressed video over RTP (Real-time Transport Protocol).

```
rtsp://admin:password@192.168.1.5:554/onvif1
```

**How it works:**
1. Your client sends a `DESCRIBE` request to discover available streams
2. The camera responds with an SDP (Session Description Protocol) payload describing codecs, resolution, and framerate
3. You send `SETUP` to configure transport (UDP or TCP interleaved)
4. `PLAY` starts the stream — raw H.264 NAL units flow to your decoder

**Why RTSP matters:** H.264/H.265 compression means a 1080p stream uses only **2–4 Mbps** of bandwidth. That's 10x more efficient than raw video. The trade-off? You need a decoder. Every frame depends on previous frames (P-frames reference I-frames), so you can't just grab a random frame — you need the full decode chain.

```python
# Simplified RTSP frame pipeline
client = RTSPClient(config)
client.connect()  # DESCRIBE → SETUP → PLAY

decoder = H264Decoder(client.sps_pps)  # Need SPS/PPS from negotiation

def on_frame(nal_data):
    frames = decoder.decode(nal_data)  # May return 0-N frames
    for frame in frames:
        process(frame)  # BGR numpy array, ready for OpenCV

client.read_frames(on_frame)
```

### MJPEG (Motion JPEG)

MJPEG is the simpler, older approach. Each frame is an independent JPEG image streamed over HTTP with multipart boundaries.

```
http://192.168.1.22:8081/0/stream
```

**How it works:**
```
--boundary
Content-Type: image/jpeg

<JPEG bytes>
--boundary
Content-Type: image/jpeg

<JPEG bytes>
...
```

**The trade-off is stark:**

| | RTSP (H.264) | MJPEG |
|---|---|---|
| Bandwidth | 2–4 Mbps (1080p) | 15–30 Mbps (1080p) |
| CPU decode | Higher (needs H.264 decoder) | Lower (just JPEG decompress) |
| Frame independence | No (temporal compression) | Yes (each frame standalone) |
| Seeking/thumbnails | Hard | Easy |
| Camera support | Most IP cameras | Budget/IoT cameras |

**Rule of thumb:** Use RTSP when bandwidth matters (multiple cameras, remote access). Use MJPEG for simplicity or when your camera only supports it.

---

## The Recording Pipeline: From Pixels to Playback

Recording video isn't just "save frames to disk." There's an engineering challenge at every step.

### 1. Frame Acquisition

Your stream worker runs in a background thread, decoding frames at 15–30 fps. Each decoded frame is a NumPy array — a 1080p BGR frame is **6.2 MB** in memory.

```python
# 1920 × 1080 × 3 channels × 1 byte = 6,220,800 bytes per frame
# At 30 fps = 186 MB/s of raw video data flowing through memory
```

### 2. Encoding to Disk

Writing raw frames would fill a 1TB drive in **90 minutes**. Instead, we re-encode to H.264 using PyAV (FFmpeg's Python bindings):

```python
container = av.open("output.mp4", mode="w",
    options={"movflags": "frag_keyframe+empty_moov"})
stream = container.add_stream("libx264", rate=15)
stream.options = {"crf": "23", "preset": "fast"}
```

Key settings explained:
- **CRF 23**: Constant Rate Factor — the quality dial. Lower = better quality, bigger files. 23 is visually lossless for surveillance.
- **preset "fast"**: Balances encoding speed vs. compression. "ultrafast" for low-CPU machines, "slow" for maximum compression.
- **frag_keyframe+empty_moov**: This is critical. Standard MP4 writes the file index (moov atom) at the end. If power cuts during recording, the file is **unplayable**. Fragmented MP4 writes the index upfront and creates self-contained fragments — the file is always playable, even mid-write.

### 3. Segment Rotation

Continuous recording creates very large files. A single 24-hour recording at CRF 23 is roughly **8–15 GB**. Problems:
- Filesystem limits on large files
- Seeking is slow in huge files
- Corruption affects the entire recording

The solution: **segment rotation**. Every 30 minutes, finalize the current file and start a new one:

```
Living_room_20260530_090000.mp4  (478 MB)
Living_room_20260530_093000.mp4  (512 MB)
Living_room_20260530_100000.mp4  (495 MB)
```

Each segment is independently playable. The timeline UI stitches them together visually.

---

## Resource Optimization: Running on Modest Hardware

A commercial NVR has dedicated hardware. Your laptop doesn't. Here's how to keep things efficient.

### CPU: Detection Frame Skipping

AI detection (YOLO, motion, face) is expensive. Running it on every frame at 30 fps would peg your CPU at 100%. Instead, skip frames:

```python
frame_count += 1
if frame_count % 5 != 0:  # Only process every 5th frame
    continue
run_detection(frame)  # ~6 fps effective detection rate
```

For object detection, go even sparser — every 15th frame. Motion detection is cheap; object/face detection is expensive. Layer them:

```python
# Cheap motion check first — gate expensive detection
if motion_detected:
    run_object_detection(frame)  # Only when something moved
```

### Memory: Frame Buffer Management

Don't keep frame history in memory. Buffer only what you need:
- **Latest frame**: For live view (1 frame per camera)
- **Event buffer**: Last 3 seconds for clip generation (~45 frames)
- **Detection frame**: Single frame for AI processing

### Disk: Tiered Storage

Not all recordings have equal value:
- **Hot (0–7 days)**: Full quality H.264, CRF 23
- **Warm (7–30 days)**: Re-encode to H.265, CRF 28 (50% size reduction)
- **Cold (30–90 days)**: H.265 at 720p, 5 fps (90% size reduction)

A storage guardian thread monitors disk usage and auto-compresses or deletes old recordings when space runs low.

### Network: Bandwidth Budget

Each 1080p RTSP stream consumes ~3 Mbps. Four cameras = 12 Mbps constant. On a 100 Mbps network that's fine, but on WiFi it adds up. Solutions:
- Use sub-streams (lower resolution) for live preview
- Record from main stream (full resolution)
- Request I-frame-only streams for bandwidth-constrained links

---

## Getting Started with nvr-viewer

### Prerequisites

- Python 3.10+
- IP cameras on your network (RTSP or MJPEG)
- ~500 MB RAM per camera

### Installation

```bash
git clone https://github.com/amwaleh/nvr-viewer.git
cd nvr-viewer
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
```

### Launch

```bash
python -m nvr_viewer web --port 8080
```

Open `http://localhost:8080` and add your cameras. That's it.

### Features

- **Multi-protocol**: RTSP and MJPEG cameras side by side
- **AI Detection**: Motion, object (YOLO), and face detection with configurable per-camera toggles
- **Continuous Recording**: Toggle per camera — recordings persist across browser sessions and server restarts
- **Timeline Playback**: Browse recordings by date and hour with a visual timeline bar
- **Event Gallery**: Detected events with clips and snapshots
- **Storage Management**: Auto-cleanup when disk runs low
- **Dark UI**: Purpose-built NVR interface with drag-and-drop camera grid

### Finding Your Camera's Stream URL

Most cameras use one of these RTSP paths:

```
rtsp://<user>:<pass>@<ip>:554/onvif1          # ONVIF standard
rtsp://<user>:<pass>@<ip>:554/stream1          # Common path
rtsp://<user>:<pass>@<ip>:554/cam/realmonitor  # Alternative path
rtsp://<user>:<pass>@<ip>:554/11               # Generic/budget cameras
```

For MJPEG cameras:
```
http://<ip>:8081/0/stream
http://<ip>/mjpg/video.mjpg
```

**Pro tip:** Use `nmap -p 554 192.168.1.0/24` to find RTSP cameras on your network. The app includes a built-in network scanner that does this automatically.

---

## What I Learned

Building an NVR taught me more about video engineering than any course could:

1. **Video is just math** — H.264 is discrete cosine transforms and motion estimation. Understanding the codec helps you make better trade-offs.
2. **Fragmented MP4 is essential** — Never use `faststart` for live recording. One power outage and your footage is gone.
3. **Detection is the easy part** — YOLO runs in milliseconds. The hard part is the plumbing: frame buffers, thread safety, segment rotation, disk management.
4. **Cameras are unreliable** — Streams drop, frames corrupt, cameras reboot randomly at 3 AM. Your system needs to reconnect automatically and not lose recordings in the process.

The code is open source. Fork it, break it, make it better. Your cameras are already on your network — might as well put them to work.

---

*Built with FastAPI, PyAV, OpenCV, and too much coffee. Star the repo if you found this useful: [github.com/amwaleh/nvr-viewer](https://github.com/amwaleh/nvr-viewer)*
