# API Test Guide

Base values:

```bash
BASE_URL="http://localhost:8001"
API_KEY="change-me"
```

Common auth header:

```bash
-H "X-API-Key: $API_KEY"
```

## Health

### `GET /health`

```bash
curl -s "$BASE_URL/health"
```

Expected `200`:

```json
{
  "ok": true,
  "configured_streams": 1
}
```

## Identities

### Seed two sample identities

```bash
python3 scripts/seed_sample_identities.py
```

Expected terminal output:

```text
seeded S001 -> Alice Demo
seeded S002 -> Bob Demo
```

### `POST /identities`

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
  "created_at": "2026-03-10T09:00:00+00:00",
  "updated_at": "2026-03-10T09:00:00+00:00"
}
```

Errors:

- `401` invalid or missing `X-API-Key`
- `422` invalid body

### `POST /identities/{id}/photos`

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

- `400` unsupported file type
- `400` decode failure
- `400` no face detected
- `404` identity not found
- `413` image larger than 8 MB
- `500` no configured streams available

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
    "created_at": "2026-03-10T09:00:00+00:00",
    "updated_at": "2026-03-10T09:02:00+00:00"
  },
  "photos": ["001.jpg"],
  "embeddings": {
    "model": "opencv-hist-fallback",
    "updated_at": "2026-03-10T09:02:00+00:00",
    "embeddings": [[0.01, 0.02]],
    "avg_embedding": [0.01, 0.02]
  }
}
```

Errors:

- `404` identity not found

## Streams

Streams are loaded from `config.json` and start automatically when the server boots.

### `GET /streams`

```bash
curl -s "$BASE_URL/streams" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
[
  {
    "slug": "cam1",
    "camera_id": "cam1",
    "rtsp_url": "rtsp://***:***@camera-1/stream1",
    "running": true,
    "attendance_session_id": null,
    "hls": "/hls/cam1/index.m3u8",
    "gpu_enabled": false
  }
]
```

### `GET /streams/{slug}/status`

```bash
curl -s "$BASE_URL/streams/cam1/status" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "running": true,
  "slug": "cam1",
  "camera_id": "cam1",
  "rtsp_url": "rtsp://***:***@camera-1/stream1",
  "hls": "/hls/cam1/index.m3u8",
  "attendance_session_id": null,
  "gpu_enabled": false,
  "updated_at": "2026-03-10T09:03:00+00:00"
}
```

Errors:

- `404` stream slug not found

## Attendance

Attendance sessions are activated per slug and auto-stop after `pipeline.attendance_duration_sec`. The current default is `60` seconds.

### `POST /attendance/activate/{slug}`

```bash
curl -s -X POST "$BASE_URL/attendance/activate/cam1" \
  -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "session_id": "sess_20260310_090500_cam1",
  "slug": "cam1",
  "status": "active",
  "auto_stop_at": "2026-03-10T09:06:00+00:00",
  "hls": "/hls/cam1/index.m3u8"
}
```

Errors:

- `404` stream slug not found
- `409` attendance already active for this slug
- `401` invalid or missing `X-API-Key`

### `POST /attendance/stop`

```bash
curl -s -X POST "$BASE_URL/attendance/stop" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"session_id":"sess_20260310_090500_cam1"}'
```

Expected `200`:

```json
{
  "session_id": "sess_20260310_090500_cam1",
  "slug": "cam1",
  "status": "ended",
  "ended_at": "2026-03-10T09:05:25+00:00"
}
```

Errors:

- `404` session not found
- `422` invalid body

### `GET /attendance/sessions`

```bash
curl -s "$BASE_URL/attendance/sessions" -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
[
  {
    "session_id": "sess_20260310_090500_cam1",
    "slug": "cam1",
    "camera_id": "cam1",
    "started_at": "2026-03-10T09:05:00+00:00",
    "ended_at": "2026-03-10T09:06:00+00:00",
    "status": "ended"
  }
]
```

### `GET /attendance/sessions/{session_id}`

```bash
curl -s "$BASE_URL/attendance/sessions/sess_20260310_090500_cam1" \
  -H "X-API-Key: $API_KEY"
```

Expected `200`:

```json
{
  "session_id": "sess_20260310_090500_cam1",
  "slug": "cam1",
  "camera_id": "cam1",
  "rtsp_url": "rtsp://***:***@camera-1/stream1",
  "hls": "/hls/cam1/index.m3u8",
  "started_at": "2026-03-10T09:05:00+00:00",
  "ended_at": "2026-03-10T09:06:00+00:00",
  "status": "ended",
  "auto_stop_at": "2026-03-10T09:06:00+00:00",
  "seen": {
    "S001": {
      "id": "S001",
      "name": "Alice",
      "first_seen": "2026-03-10T09:05:07+00:00",
      "last_seen": "2026-03-10T09:05:41+00:00",
      "count": 4,
      "best_similarity": 0.71,
      "latest_snapshot": "/recognitions/cam1/S001/20260310T090541000000Z.jpg"
    }
  },
  "events": [
    {
      "ts": "2026-03-10T09:05:07+00:00",
      "type": "recognized",
      "id": "S001",
      "name": "Alice",
      "similarity": 0.66,
      "snapshot": "/recognitions/cam1/S001/20260310T090507000000Z.jpg"
    }
  ]
}
```

Errors:

- `404` session not found

## HLS and Recognition Images

The Python server serves both media types directly.

### HLS playlist

```bash
curl -i "$BASE_URL/hls/cam1/index.m3u8"
```

Expected:

- `200` with the HLS playlist when FFmpeg has produced segments
- `404` until the first playlist exists

### Recognition image

Use a `snapshot` or `latest_snapshot` path returned by a session:

```bash
curl -O "$BASE_URL/recognitions/cam1/S001/20260310T090507000000Z.jpg"
```

Expected:

- `200` with the JPEG file
- `404` if the file does not exist
