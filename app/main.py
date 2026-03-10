from __future__ import annotations

import imghdr

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .schemas import AttendanceStopRequest, IdentityUpsert
from .security import api_key_dependency
from .storage import Storage
from .worker import StreamManager

cfg = load_config()
storage = Storage(root="data")
manager = StreamManager(storage=storage, config=cfg.raw)
auth = api_key_dependency(cfg.api.get("api_key", ""))

app = FastAPI(title="Face Attendance RTSP->HLS", version="2.0.0")
app.mount("/hls", StaticFiles(directory="data/hls"), name="hls")
app.mount("/recognitions", StaticFiles(directory="data/attendance/recognitions"), name="recognitions")


@app.on_event("startup")
def on_startup() -> None:
    manager.start_background()


@app.on_event("shutdown")
def on_shutdown() -> None:
    manager.shutdown()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "configured_streams": len(cfg.raw.get("streams", []))}


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

    image = cv2.imdecode(np.frombuffer(content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise HTTPException(status_code=400, detail="Could not decode image")

    embeddings = None
    model_name = "opencv-hist-fallback"
    for worker in manager.workers.values():
        embeddings = worker.face_engine.extract_embeddings_from_image(image)
        model_name = worker.face_engine.model_name
        break
    if embeddings is None:
        raise HTTPException(status_code=500, detail="No configured streams available to initialize face engine")
    if not embeddings:
        raise HTTPException(status_code=400, detail="No face detected in image")

    saved = storage.save_photo(identity_id, content, "jpg" if ext == "jpeg" else ext)
    latest = None
    for emb in embeddings:
        latest = storage.append_embedding(identity_id, emb, model_name)
    return {
        "identity_id": identity_id,
        "photo_file": saved.name,
        "faces_detected": len(embeddings),
        "embedding_model": model_name,
        "embeddings_total": len(latest["embeddings"]) if latest else 0,
    }


@app.get("/identities", dependencies=[Depends(auth)])
def list_identities() -> list[dict]:
    return storage.list_identities()


@app.get("/identities/{identity_id}", dependencies=[Depends(auth)])
def get_identity(identity_id: str) -> dict:
    identity = storage.get_identity(identity_id)
    if not identity:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found")
    return identity


@app.get("/streams", dependencies=[Depends(auth)])
def list_streams() -> list[dict]:
    return manager.list_streams()


@app.get("/streams/{slug}/status", dependencies=[Depends(auth)])
def stream_status(slug: str) -> dict:
    try:
        status = manager.get_status(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Stream slug {slug} not found") from None
    status["rtsp_url"] = storage.sanitize_rtsp(status.get("rtsp_url", ""))
    return status


@app.post("/attendance/activate/{slug}", dependencies=[Depends(auth)])
def activate_attendance(slug: str) -> dict:
    try:
        session = manager.activate_attendance(slug)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Stream slug {slug} not found") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "session_id": session["session_id"],
        "slug": session["slug"],
        "status": session["status"],
        "auto_stop_at": session["auto_stop_at"],
        "hls": session["hls"],
    }


@app.post("/attendance/stop", dependencies=[Depends(auth)])
def stop_attendance(payload: AttendanceStopRequest) -> dict:
    session = manager.stop_attendance(payload.session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {payload.session_id} not found")
    return {
        "session_id": session["session_id"],
        "slug": session["slug"],
        "status": session["status"],
        "ended_at": session.get("ended_at"),
    }


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
    return {
        "service": "face-attendance",
        "docs": "/docs",
        "streams_endpoint": "/streams",
        "hls_example": "/hls/<slug>/index.m3u8",
        "recognitions_example": "/recognitions/<slug>/<person_id>/<file>.jpg",
    }
