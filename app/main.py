from __future__ import annotations

import imghdr
from pathlib import Path

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .schemas import AttendanceStartRequest, AttendanceStopRequest, IdentityUpsert, StreamStartRequest
from .security import api_key_dependency
from .storage import Storage
from .worker import StreamWorker

cfg = load_config()
storage = Storage(root="data")
worker = StreamWorker(storage=storage, config=cfg.raw)
auth = api_key_dependency(cfg.api.get("api_key", ""))

app = FastAPI(title="Face Attendance RTSP->HLS", version="1.0.0")
app.mount("/hls", StaticFiles(directory="data/hls"), name="hls")


@app.on_event("startup")
def on_startup() -> None:
    worker.start_background()


@app.on_event("shutdown")
def on_shutdown() -> None:
    worker.shutdown()


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/identities", dependencies=[Depends(auth)])
def upsert_identity(payload: IdentityUpsert) -> dict:
    return storage.upsert_identity(payload.id, payload.name)


@app.post("/identities/{identity_id}/photos", dependencies=[Depends(auth)])
async def upload_identity_photo(identity_id: str, photo: UploadFile = File(...)) -> dict:
    identity = storage.get_identity(identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
    content = await photo.read()
    if len(content) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image exceeds 8MB limit")
    ext = imghdr.what(None, h=content)
    if ext not in {"jpeg", "png"}:
        raise HTTPException(status_code=400, detail="Only JPEG and PNG images are allowed")

    saved = storage.save_photo(identity_id, content, "jpg" if ext == "jpeg" else ext)
    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Could not decode image")
    embeddings = worker.face_engine.extract_embeddings_from_image(image)
    if not embeddings:
        raise HTTPException(status_code=400, detail="No face detected in image")
    latest = None
    for emb in embeddings:
        latest = storage.append_embedding(identity_id, emb, worker.face_engine.model_name)
    return {
        "identity_id": identity_id,
        "photo_file": saved.name,
        "faces_detected": len(embeddings),
        "embedding_model": worker.face_engine.model_name,
        "embeddings_total": len(latest["embeddings"]) if latest else 0,
    }


@app.get("/identities", dependencies=[Depends(auth)])
def list_identities() -> list[dict]:
    return storage.list_identities()


@app.get("/identities/{identity_id}", dependencies=[Depends(auth)])
def get_identity(identity_id: str) -> dict:
    out = storage.get_identity(identity_id)
    if not out:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
    return out


@app.post("/stream/start", dependencies=[Depends(auth)])
def start_stream(payload: StreamStartRequest) -> dict:
    worker.enqueue("start_stream", {"camera_id": payload.camera_id, "rtsp_url": payload.rtsp_url})
    return {"running": True, "camera_id": payload.camera_id, "hls": "/hls/live/index.m3u8"}


@app.post("/stream/stop", dependencies=[Depends(auth)])
def stop_stream() -> dict:
    worker.enqueue("stop_stream", {})
    return {"running": False}


@app.get("/stream/status", dependencies=[Depends(auth)])
def stream_status() -> dict:
    return worker.get_status()


@app.post("/attendance/start", dependencies=[Depends(auth)])
def start_attendance(payload: AttendanceStartRequest) -> dict:
    status_payload = worker.get_status()
    if not status_payload["running"]:
        active = storage.read_active_stream()
        if not active.get("running") or active.get("camera_id") != payload.camera_id:
            raise HTTPException(
                status_code=409,
                detail="No running stream for this camera_id. Start stream first.",
            )
        worker.enqueue(
            "start_stream",
            {
                "camera_id": active["camera_id"],
                "rtsp_url": active["rtsp_url"],
            },
        )
    if status_payload.get("attendance_session_id"):
        raise HTTPException(status_code=409, detail="Another attendance session is already active")
    stream = storage.read_active_stream()
    if not stream.get("running"):
        raise HTTPException(status_code=409, detail="No active stream metadata found")
    session = storage.create_session(camera_id=payload.camera_id, rtsp_url=stream.get("rtsp_url", ""))
    worker.enqueue("start_attendance", {"session_id": session["session_id"]})
    return {"session_id": session["session_id"], "status": "active", "auto_stop_at": session["auto_stop_at"]}


@app.post("/attendance/stop", dependencies=[Depends(auth)])
def stop_attendance(payload: AttendanceStopRequest) -> dict:
    session = storage.get_session(payload.session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {payload.session_id} not found")
    if session["status"] != "active":
        return {"session_id": payload.session_id, "status": session["status"], "ended_at": session.get("ended_at")}
    session["status"] = "ended"
    from .utils import now_iso

    session["ended_at"] = now_iso()
    storage.update_session(payload.session_id, session)
    worker.enqueue("stop_attendance", {"session_id": payload.session_id})
    return {"session_id": payload.session_id, "status": "ended", "ended_at": session["ended_at"]}


@app.get("/attendance/sessions", dependencies=[Depends(auth)])
def list_sessions() -> list[dict]:
    return storage.list_sessions()


@app.get("/attendance/sessions/{session_id}", dependencies=[Depends(auth)])
def get_session(session_id: str) -> dict:
    session = storage.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    session["rtsp_url"] = storage.sanitize_rtsp(session.get("rtsp_url", ""))
    return session


@app.get("/")
def root() -> dict:
    return {"service": "face-attendance", "docs": "/docs", "hls_example": "/hls/live/index.m3u8"}


@app.get("/hls/live/index.m3u8")
def get_hls_index():
    playlist = Path("data/hls/live/index.m3u8")
    if not playlist.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="HLS playlist not available yet")
    return FileResponse(path=playlist, media_type="application/vnd.apple.mpegurl")
