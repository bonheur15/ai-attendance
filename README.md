# Face Attendance RTSP -> HLS (Python, JSON Storage)

Production-style MVP implementation of the system in `.material/design.md`.

## Features

- RTSP ingest and reconnect logic
- Face detect + recognize overlays (`ID + Name + similarity`)
- HLS output at `data/hls/live/index.m3u8`
- Identity management with photo upload and embedding generation
- Attendance sessions with per-student timestamps, counts, and similarity stats
- CPU-first defaults, GPU auto-switch (`h264_nvenc`) when available
- JSON-only persistence (no database)
- API key protection with `X-API-Key`

## Tech Stack

- FastAPI
- OpenCV
- NumPy
- FFmpeg
- Filesystem JSON storage

## Quick Start

1. Create virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Configure API key and runtime settings:

```bash
cp config.json config.local.json
# edit config.local.json and set api.api_key
```

If you use `config.local.json`, set environment variable `CONFIG_PATH` and run with it:

```bash
CONFIG_PATH=config.local.json uvicorn app.main:app --host 0.0.0.0 --port 8000
```

3. Default run:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

4. Open docs:

- Swagger UI: `http://localhost:8000/docs`
- HLS path: `http://localhost:8000/hls/live/index.m3u8`

## Configuration

Edit `config.json`:

- `api.api_key`: required header key
- `pipeline.similarity_threshold`: recognition threshold
- `pipeline.detect_every_n_frames_cpu/gpu`: speed vs accuracy
- `hls.segment_time_sec`, `hls.list_size`: HLS behavior

## Storage Layout

```text
data/
  identities/<id>/{meta.json,photos/*,embeddings.json}
  attendance/{index.json,sessions/<session_id>.json}
  streams/active.json
  hls/live/{index.m3u8,seg_*.ts}
  logs/
```

## API Documentation

Full endpoint docs with test `curl` examples and expected responses/errors:

- [docs/API.md](/home/bonheur/Desktop/Projects/ai/attendance/docs/API.md)
