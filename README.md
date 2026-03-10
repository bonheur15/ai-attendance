# Face Attendance RTSP -> HLS (Python, JSON Storage)

The server reads a configured list of RTSP streams from `config.json`, starts them automatically on boot, produces HLS per stream slug, and serves both HLS playlists and recognition snapshots directly from FastAPI.

## Features

- Multi-stream startup from `config.json`
- Per-slug HLS output at `/hls/<slug>/index.m3u8`
- Per-person recognition snapshots at `/recognitions/<slug>/<person_id>/<file>.jpg`
- Attendance activation by stream slug
- Automatic session stop after `pipeline.attendance_duration_sec` seconds
- Identity management with photo upload and embedding generation
- CPU-first defaults with GPU encoder auto-detect
- JSON-only persistence, no database
- API key protection with `X-API-Key`

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8001
```

If you want a separate config file:

```bash
CONFIG_PATH=config.local.json uvicorn app.main:app --host 0.0.0.0 --port 8001
```

## Config

`config.json` now contains the stream inventory:

```json
{
  "streams": [
    {
      "slug": "cam1",
      "camera_id": "cam1",
      "rtsp_url": "rtsp://user:pass@camera-1/stream1"
    }
  ]
}
```

Important fields:

- `streams`: list of RTSP inputs started automatically
- `pipeline.attendance_duration_sec`: session lifetime in seconds, default `60`
- `pipeline.similarity_threshold`: recognition threshold
- `hls.segment_time_sec` and `hls.list_size`: HLS behavior
- `api.api_key`: required request header

## Python-served media

- API docs: `http://localhost:8001/docs`
- HLS playlist: `http://localhost:8001/hls/cam1/index.m3u8`
- Recognition images: `http://localhost:8001/recognitions/cam1/S001/<timestamp>.jpg`

## Storage Layout

```text
data/
  identities/<id>/{meta.json,photos/*,embeddings.json}
  attendance/
    index.json
    sessions/<session_id>.json
    recognitions/<slug>/<person_id>/*.jpg
  streams/active.json
  hls/<slug>/{index.m3u8,seg_*.ts}
  logs/
```

Full endpoint documentation, including `curl`, success responses, and error cases, is in [docs/API.md](/home/bonheur/Desktop/Projects/ai/attendance/docs/API.md).
