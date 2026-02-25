# Face Attendance RTSP ➜ HLS System (CPU-first, GPU-boosted) — Full Design Document

## 1) Goal

Build a fast, optimized service that:

* **Receives an RTSP stream**
* **Detects + recognizes faces**, draws **`ID + Name`** over the video
* **Outputs HLS** (playable in browsers)
* Provides API endpoints to:

  * Upload a new person’s photo(s) with an **ID** and **Name**
  * Start / stop **attendance sessions**
  * List **sessions** and **students detected** per session (with timestamps)
* **No database**: everything stored as **JSON files + folders**
* Runs well on **CPU**, but **automatically accelerates** if GPU is detected.

---

## 2) Architecture Overview

### Components

1. **API Server**

   * Handles uploads, identity metadata, session control, session queries.

2. **Stream Worker**

   * Reads RTSP frames (OpenCV)
   * Detects faces, tracks them, runs recognition
   * Draws overlays (ID + Name)
   * Feeds frames to FFmpeg for HLS output

3. **FFmpeg Encoder**

   * Turns processed frames into HLS segments + playlist

### Why split API & Worker?

* Keeps RTSP + video pipeline stable even if API receives many requests.
* Worker can restart independently without breaking identity/session storage.

---

## 3) Folder & File Layout (No DB)

```
data/
  identities/
    <person_id>/
      meta.json
      photos/
        001.jpg
        002.jpg
      embeddings.json
  attendance/
    sessions/
      <session_id>.json
    index.json
  streams/
    active.json
  hls/
    live/
      index.m3u8
      seg_00001.ts
      seg_00002.ts
      ...
  logs/
    worker.log
    api.log
```

### Identity files

**`data/identities/<id>/meta.json`**

```json
{
  "id": "S001",
  "name": "Alice",
  "created_at": "2026-02-25T14:20:10+02:00",
  "updated_at": "2026-02-25T14:22:10+02:00"
}
```

**`data/identities/<id>/embeddings.json`**

```json
{
  "model": "insightface/arcface",
  "updated_at": "2026-02-25T14:22:10+02:00",
  "embeddings": [
    [0.01, -0.02, ...],
    [0.00, -0.01, ...]
  ],
  "avg_embedding": [0.005, -0.015, ...]
}
```

### Attendance session file

**`data/attendance/sessions/<session_id>.json`**

```json
{
  "session_id": "sess_20260225_142500_cam1",
  "camera_id": "cam1",
  "rtsp_url": "rtsp://....",
  "started_at": "2026-02-25T14:25:00+02:00",
  "ended_at": null,
  "status": "active",
  "auto_stop_at": "2026-02-25T15:25:00+02:00",
  "seen": {
    "S001": {
      "id": "S001",
      "name": "Alice",
      "first_seen": "2026-02-25T14:25:16+02:00",
      "last_seen": "2026-02-25T14:26:03+02:00",
      "count": 14,
      "best_similarity": 0.63
    }
  },
  "events": [
    {
      "ts": "2026-02-25T14:25:16+02:00",
      "type": "recognized",
      "id": "S001",
      "name": "Alice",
      "similarity": 0.61
    }
  ]
}
```

### Attendance index

**`data/attendance/index.json`**

```json
{
  "latest_session_id": "sess_20260225_142500_cam1",
  "sessions": [
    { "session_id": "...", "camera_id": "cam1", "started_at": "...", "ended_at": "...", "status": "ended" }
  ]
}
```

---

## 4) API Endpoints

### 4.1 Identities

#### Create / update person metadata

`POST /identities`
Body:

```json
{ "id": "S001", "name": "Alice" }
```

Behavior:

* Create folder if missing
* Write `meta.json`

#### Upload a photo for a person

`POST /identities/{id}/photos` (multipart form)
Form fields:

* `photo`: image file
  Behavior:
* Save into `photos/`
* Extract face embedding(s)
* Update `embeddings.json` (append + recompute average)

#### List identities

`GET /identities`
Returns:

```json
[{ "id":"S001", "name":"Alice" }, ...]
```

#### Get identity

`GET /identities/{id}`

---

### 4.2 Stream Control

#### Start stream worker

`POST /stream/start`
Body:

```json
{ "camera_id":"cam1", "rtsp_url":"rtsp://..." }
```

Behavior:

* Save `data/streams/active.json`
* Worker reads that file and starts pipeline (or API signals it via lightweight IPC)

#### Stop stream

`POST /stream/stop`

#### Stream status

`GET /stream/status`
Returns:

```json
{ "running": true, "camera_id":"cam1", "hls":"/hls/live/index.m3u8" }
```

---

### 4.3 Attendance Sessions

#### Start attendance

`POST /attendance/start`
Body:

```json
{ "camera_id":"cam1" }
```

Behavior:

* Creates a new session JSON file
* Sets session `status = active`
* Auto-stop time = now + 1 hour
* Returns session_id

#### Stop attendance

`POST /attendance/stop`
Body:

```json
{ "session_id":"sess_..." }
```

#### List sessions

`GET /attendance/sessions`

#### Get session details

`GET /attendance/sessions/{session_id}`

---

## 5) Face Pipeline (Fast + Stable)

### 5.1 Frame flow

1. OpenCV `VideoCapture(rtsp_url)`
2. Read frames
3. Every **N frames** → run **face detection**
4. Track faces between detections (to avoid re-detecting constantly)
5. For each track:

   * Occasionally compute embedding & match identity
6. Draw overlay: `ID` and `Name` on the bounding box
7. Push frames into FFmpeg → HLS

### 5.2 “Don’t recognize every frame” rule (major speed boost)

* Detect every **5–10 frames**
* Recognize per track at most **1–2 times per second**
* Cache result per track:

  * `track_id -> last_identity, last_similarity, last_checked_ts`

### 5.3 Matching logic

* Compute cosine similarity vs known embeddings
* Select best match
* If `similarity >= threshold` → recognized
* Else → “Unknown”

Typical thresholds depend on model; you’ll tune it, but start with:

* `threshold = 0.55` (adjust after testing)

### 5.4 Attendance write rules (avoid false positives)

A student is counted “present” if:

* recognized above threshold
* AND same track remains recognized for at least:

  * `min_stable_ms = 700ms` (example)
* Then update:

  * first_seen / last_seen / count / best_similarity

---

## 6) CPU vs GPU: Auto Optimization

### 6.1 CPU-first design

Everything must run fine on CPU:

* Lower detection input size (ex: 640 width)
* Detect less frequently (every 8–12 frames)
* Use lightweight tracker
* HLS encoding: `libx264` with “veryfast” preset

### 6.2 GPU auto-detect (best effort)

On startup, worker checks:

* NVIDIA: `nvidia-smi` exists and returns OK
* Also check if FFmpeg supports `h264_nvenc`
* Also check if face embedding backend can use GPU (depends on your choice)

If GPU found:

* Increase detection frequency slightly (more accurate)
* Encode with `h264_nvenc` (big CPU savings)
* If embedding model uses GPU runtime → enable it

### 6.3 Encoder mode selection

**CPU encode**

* `libx264`, preset `veryfast`, tune `zerolatency`

**NVIDIA encode**

* `h264_nvenc`, low-latency params

---

## 7) OpenCV RTSP Stability Guide (Important)

OpenCV RTSP can be flaky; these choices improve stability:

### 7.1 Use FFmpeg backend explicitly (if available)

* Ensure OpenCV was built with FFmpeg.
* Use:

  * `cv2.CAP_FFMPEG`

### 7.2 Force TCP transport (reduces packet loss)

RTSP over TCP is usually more stable than UDP.
Many cameras allow:

* `rtsp://...` and OpenCV/FFmpeg options

If you can pass options via environment (varies by build), prefer:

* `rtsp_transport=tcp`
* Increase buffer / timeout

### 7.3 Reconnect strategy (must-have)

Worker loop should:

* If `read()` fails for X frames / seconds:

  * Release capture
  * Wait short backoff (0.5s → 2s)
  * Reopen RTSP
* Keep HLS running with last frame or a “Reconnecting…” frame

### 7.4 Separate “grab” and “decode” (optional)

If you notice lag:

* Use a reader thread that always reads latest frame
* Processor thread takes the most recent frame (drops old frames)
  This keeps latency lower.

### 7.5 Frame dropping is GOOD

For live attendance, you want “current” frames, not processing every frame.
So the system should skip frames under load.

---

## 8) HLS Output Strategy (Smooth + Browser Friendly)

* Segment duration: `1–2s`
* Playlist size: `6–10`
* Delete old segments to save disk

Folder:

* `data/hls/live/index.m3u8`

Serve with Caddy/Nginx:

* `/hls/*` maps to `data/hls/`

---

## 9) Configuration File (Single source of truth)

**`config.json`**

```json
{
  "rtsp": {
    "read_timeout_ms": 5000,
    "reconnect_backoff_ms": 700,
    "max_consecutive_failures": 50
  },
  "pipeline": {
    "detect_every_n_frames_cpu": 10,
    "detect_every_n_frames_gpu": 6,
    "recognize_cooldown_ms": 600,
    "detector_input_width": 640,
    "min_stable_ms": 700,
    "similarity_threshold": 0.55
  },
  "hls": {
    "output_dir": "data/hls/live",
    "segment_time_sec": 2,
    "list_size": 8
  }
}
```

---

## 10) Worker State Machine

### States

* `STOPPED`
* `STREAMING` (RTSP -> HLS, overlays)
* `ATTENDANCE_ACTIVE` (streaming + writing session)

### Attendance auto-stop

When session starts:

* `auto_stop_at = now + 1 hour`
  Worker checks each loop:
* if `now >= auto_stop_at`: stop session

### Crash safety

Since sessions are JSON:

* Update session file in safe way:

  * write to temp file
  * atomic rename to final
    This prevents corrupted JSON if power fails.

---

## 11) Security Notes (basic)

* Validate uploaded images (type/size)
* Require a simple API key header (even if internal)
* Don’t expose RTSP credentials in public responses

---

## 12) Recommended “MVP Build Order”

1. JSON storage + identity upload + meta saving
2. Embedding extraction & `embeddings.json`
3. RTSP read + overlay demo (no recognition yet)
4. Recognition + threshold tuning
5. Attendance session JSON writing
6. Reconnect & stability improvements
7. GPU detection + encoder switching
