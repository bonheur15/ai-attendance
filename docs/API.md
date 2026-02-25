# API Test Guide (`curl` + Expected Results)

Base URL:

```bash
BASE_URL="http://localhost:8000"
API_KEY="change-me"
```

Common header:

```bash
-H "X-API-Key: $API_KEY"
```

## 1) Health

### `GET /health`

```bash
curl -s "$BASE_URL/health"
```

Expected `200`:

```json
{ "ok": true }
```

## 2) Identities

### `POST /identities`

Create or update identity metadata.

```bash
curl -s -X POST "$BASE_URL/identities" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"id":"S001","name":"Alice"}'
```

Expected `200`:

```json
{
  "id": "S001",
  "name": "Alice",
  "created_at": "2026-02-25T16:00:00+00:00",
  "updated_at": "2026-02-25T16:00:00+00:00"
}
```

Errors:

- `401` missing/invalid API key
- `422` invalid payload (`id` or `name` missing/empty)

### `POST /identities/{id}/photos`

Upload JPEG/PNG image; at least one face must be detected.

```bash
curl -s -X POST "$BASE_URL/identities/S001/photos" \
  -H "X-API-Key: $API_KEY" \
  -F "photo=@/absolute/path/alice.jpg"
```

Expected `200`:

```json
{
  "identity_id": "S001",
  "photo_file": "001.jpg",
  "faces_detected": 1,
  "embedding_model": "opencv-hist-fallback",
  "embeddings_total": 1
}
```

Errors:

- `400` invalid file format (non JPEG/PNG)
- `400` image decode failed
- `400` no face detected
- `404` identity not found
- `413` image too large (>8MB)
- `401` missing/invalid API key

### `GET /identities`

```bash
curl -s "$BASE_URL/identities" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
[
  { "id": "S001", "name": "Alice" }
]
```

Errors:

- `401` missing/invalid API key

### `GET /identities/{id}`

```bash
curl -s "$BASE_URL/identities/S001" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "meta": {
    "id": "S001",
    "name": "Alice",
    "created_at": "2026-02-25T16:00:00+00:00",
    "updated_at": "2026-02-25T16:02:00+00:00"
  },
  "photos": ["001.jpg"],
  "embeddings": {
    "model": "opencv-hist-fallback",
    "updated_at": "2026-02-25T16:02:00+00:00",
    "embeddings": [[0.01, 0.02]],
    "avg_embedding": [0.01, 0.02]
  }
}
```

Errors:

- `404` identity not found
- `401` missing/invalid API key

## 3) Stream Control

### `POST /stream/start`

```bash
curl -s -X POST "$BASE_URL/stream/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"camera_id":"cam1","rtsp_url":"rtsp://user:pass@camera/stream1"}'
```

Expected `200`:

```json
{
  "running": true,
  "camera_id": "cam1",
  "hls": "/hls/live/index.m3u8"
}
```

Errors:

- `401` missing/invalid API key
- `422` invalid payload

### `POST /stream/stop`

```bash
curl -s -X POST "$BASE_URL/stream/stop" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{ "running": false }
```

Errors:

- `401` missing/invalid API key

### `GET /stream/status`

```bash
curl -s "$BASE_URL/stream/status" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "running": true,
  "camera_id": "cam1",
  "hls": "/hls/live/index.m3u8",
  "attendance_session_id": "sess_20260225_160500_cam1",
  "gpu_enabled": false
}
```

Errors:

- `401` missing/invalid API key

## 4) Attendance

### `POST /attendance/start`

Starts a 1-hour session. Requires an active stream for the same camera.

```bash
curl -s -X POST "$BASE_URL/attendance/start" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"camera_id":"cam1"}'
```

Expected `200`:

```json
{
  "session_id": "sess_20260225_160500_cam1",
  "status": "active",
  "auto_stop_at": "2026-02-25T17:05:00+00:00"
}
```

Errors:

- `401` missing/invalid API key
- `409` no running stream for this `camera_id`
- `409` another session already active
- `422` invalid payload

### `POST /attendance/stop`

```bash
curl -s -X POST "$BASE_URL/attendance/stop" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"session_id":"sess_20260225_160500_cam1"}'
```

Expected `200`:

```json
{
  "session_id": "sess_20260225_160500_cam1",
  "status": "ended",
  "ended_at": "2026-02-25T16:30:00+00:00"
}
```

Errors:

- `401` missing/invalid API key
- `404` session not found
- `422` invalid payload

### `GET /attendance/sessions`

```bash
curl -s "$BASE_URL/attendance/sessions" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
[
  {
    "session_id": "sess_20260225_160500_cam1",
    "camera_id": "cam1",
    "started_at": "2026-02-25T16:05:00+00:00",
    "ended_at": "2026-02-25T16:30:00+00:00",
    "status": "ended"
  }
]
```

Errors:

- `401` missing/invalid API key

### `GET /attendance/sessions/{session_id}`

```bash
curl -s "$BASE_URL/attendance/sessions/sess_20260225_160500_cam1" \
  -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "session_id": "sess_20260225_160500_cam1",
  "camera_id": "cam1",
  "rtsp_url": "rtsp://***:***@camera/stream1",
  "started_at": "2026-02-25T16:05:00+00:00",
  "ended_at": "2026-02-25T16:30:00+00:00",
  "status": "ended",
  "auto_stop_at": "2026-02-25T17:05:00+00:00",
  "seen": {
    "S001": {
      "id": "S001",
      "name": "Alice",
      "first_seen": "2026-02-25T16:06:15+00:00",
      "last_seen": "2026-02-25T16:25:22+00:00",
      "count": 14,
      "best_similarity": 0.71
    }
  },
  "events": [
    {
      "ts": "2026-02-25T16:06:15+00:00",
      "type": "recognized",
      "id": "S001",
      "name": "Alice",
      "similarity": 0.66
    }
  ]
}
```

Errors:

- `401` missing/invalid API key
- `404` session not found

## 5) HLS Playback

Once stream is running:

```bash
curl -i "$BASE_URL/hls/live/index.m3u8"
```

Expected:

- `200` + playlist content, if worker already produced segments
- `404` with:

```json
{ "detail": "HLS playlist not available yet" }
```

## Notes

- RTSP credentials are masked in session read responses.
- Attendance recognition events are written only after stable recognition (`min_stable_ms`).
- Worker auto-stops attendance session after 1 hour.
