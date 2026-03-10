from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cv2

from .utils import atomic_write_json, now_iso, read_json


class Storage:
    def __init__(self, root: str | Path = "data"):
        self.root = Path(root)
        self.identities_root = self.root / "identities"
        self.attendance_root = self.root / "attendance"
        self.sessions_root = self.attendance_root / "sessions"
        self.recognitions_root = self.attendance_root / "recognitions"
        self.streams_root = self.root / "streams"
        self.hls_root = self.root / "hls"
        self.logs_root = self.root / "logs"
        self.ensure_layout()

    def ensure_layout(self) -> None:
        for path in [
            self.identities_root,
            self.attendance_root,
            self.sessions_root,
            self.recognitions_root,
            self.streams_root,
            self.hls_root,
            self.logs_root,
        ]:
            path.mkdir(parents=True, exist_ok=True)
        index_path = self.attendance_root / "index.json"
        if not index_path.exists():
            atomic_write_json(index_path, {"latest_session_id": None, "sessions": []})
        streams_path = self.streams_root / "active.json"
        if not streams_path.exists():
            atomic_write_json(streams_path, {"streams": {}})

    def identity_dir(self, identity_id: str) -> Path:
        return self.identities_root / identity_id

    def upsert_identity(self, identity_id: str, name: str) -> dict[str, Any]:
        person_dir = self.identity_dir(identity_id)
        photos_dir = person_dir / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        meta_path = person_dir / "meta.json"
        existing = read_json(meta_path, {})
        created_at = existing.get("created_at") or now_iso()
        payload = {
            "id": identity_id,
            "name": name,
            "created_at": created_at,
            "updated_at": now_iso(),
        }
        atomic_write_json(meta_path, payload)
        return payload

    def list_identities(self) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for child in sorted(self.identities_root.glob("*")):
            meta = read_json(child / "meta.json")
            if meta:
                out.append({"id": meta["id"], "name": meta["name"]})
        return out

    def get_identity(self, identity_id: str) -> dict[str, Any] | None:
        person_dir = self.identity_dir(identity_id)
        meta = read_json(person_dir / "meta.json")
        if not meta:
            return None
        emb = read_json(person_dir / "embeddings.json", default={"embeddings": [], "avg_embedding": []})
        photos = sorted(p.name for p in (person_dir / "photos").glob("*") if p.is_file())
        return {"meta": meta, "photos": photos, "embeddings": emb}

    def save_photo(self, identity_id: str, image_bytes: bytes, ext: str) -> Path:
        person_dir = self.identity_dir(identity_id)
        photos_dir = person_dir / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        next_idx = len(list(photos_dir.glob("*"))) + 1
        filename = f"{next_idx:03d}.{ext}"
        dest = photos_dir / filename
        dest.write_bytes(image_bytes)
        return dest

    def load_embeddings(self, identity_id: str) -> dict[str, Any]:
        return read_json(
            self.identity_dir(identity_id) / "embeddings.json",
            default={"model": "hist-fallback", "updated_at": now_iso(), "embeddings": [], "avg_embedding": []},
        )

    def save_embeddings(self, identity_id: str, payload: dict[str, Any]) -> None:
        atomic_write_json(self.identity_dir(identity_id) / "embeddings.json", payload)

    def all_identity_embeddings(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for person in self.identities_root.glob("*"):
            meta = read_json(person / "meta.json")
            emb = read_json(person / "embeddings.json")
            if not meta or not emb:
                continue
            avg = emb.get("avg_embedding") or []
            if not avg:
                continue
            out.append({"id": meta["id"], "name": meta["name"], "avg_embedding": avg})
        return out

    def hls_dir(self, slug: str) -> Path:
        path = self.hls_root / slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save_stream_status(self, slug: str, payload: dict[str, Any]) -> None:
        path = self.streams_root / "active.json"
        current = read_json(path, default={"streams": {}})
        current.setdefault("streams", {})
        current["streams"][slug] = payload
        atomic_write_json(path, current)

    def clear_stream_status(self, slug: str) -> None:
        self.save_stream_status(slug, {"running": False, "slug": slug, "updated_at": now_iso()})

    def list_stream_statuses(self) -> dict[str, Any]:
        data = read_json(self.streams_root / "active.json", default={"streams": {}})
        return data.get("streams", {})

    def read_stream_status(self, slug: str) -> dict[str, Any]:
        return self.list_stream_statuses().get(slug, {"running": False, "slug": slug})

    def create_session(self, slug: str, camera_id: str, rtsp_url: str, duration_sec: int, hls_path: str) -> dict[str, Any]:
        ts = datetime.now(timezone.utc)
        session_id = f"sess_{ts.strftime('%Y%m%d_%H%M%S')}_{slug}"
        started_at = now_iso()
        auto_stop_at = (ts + timedelta(seconds=duration_sec)).isoformat()
        session = {
            "session_id": session_id,
            "slug": slug,
            "camera_id": camera_id,
            "rtsp_url": rtsp_url,
            "hls": hls_path,
            "started_at": started_at,
            "ended_at": None,
            "status": "active",
            "auto_stop_at": auto_stop_at,
            "seen": {},
            "events": [],
        }
        atomic_write_json(self.sessions_root / f"{session_id}.json", session)

        index_path = self.attendance_root / "index.json"
        index = read_json(index_path, default={"latest_session_id": None, "sessions": []})
        index["latest_session_id"] = session_id
        index["sessions"].append(
            {
                "session_id": session_id,
                "slug": slug,
                "camera_id": camera_id,
                "started_at": started_at,
                "ended_at": None,
                "status": "active",
            }
        )
        atomic_write_json(index_path, index)
        return session

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        return read_json(self.sessions_root / f"{session_id}.json")

    def list_sessions(self) -> list[dict[str, Any]]:
        index = read_json(self.attendance_root / "index.json", default={"sessions": []})
        return index.get("sessions", [])

    def update_session(self, session_id: str, session: dict[str, Any]) -> None:
        atomic_write_json(self.sessions_root / f"{session_id}.json", session)
        index_path = self.attendance_root / "index.json"
        index = read_json(index_path, default={"latest_session_id": None, "sessions": []})
        for row in index.get("sessions", []):
            if row["session_id"] == session_id:
                row["status"] = session["status"]
                row["ended_at"] = session.get("ended_at")
        atomic_write_json(index_path, index)

    def add_session_event(self, session_id: str, event: dict[str, Any]) -> None:
        session = self.get_session(session_id)
        if not session:
            return
        session["events"].append(event)
        self.update_session(session_id, session)

    def save_recognition_snapshot(self, slug: str, identity_id: str, image_bgr: Any) -> str | None:
        if image_bgr is None or getattr(image_bgr, "size", 0) == 0:
            return None
        person_dir = self.recognitions_root / slug / identity_id
        person_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.jpg"
        out_path = person_dir / filename
        ok = cv2.imwrite(str(out_path), image_bgr)
        if not ok:
            return None
        return f"/recognitions/{slug}/{identity_id}/{filename}"

    def append_embedding(self, identity_id: str, embedding: list[float], model_name: str) -> dict[str, Any]:
        data = self.load_embeddings(identity_id)
        embeddings = data.get("embeddings", [])
        embeddings.append(embedding)
        dim = len(embedding)
        avg = [0.0] * dim
        for vec in embeddings:
            for i in range(dim):
                avg[i] += float(vec[i])
        avg = [v / len(embeddings) for v in avg]
        payload = {
            "model": model_name,
            "updated_at": now_iso(),
            "embeddings": embeddings,
            "avg_embedding": avg,
        }
        self.save_embeddings(identity_id, payload)
        return payload

    @staticmethod
    def sanitize_rtsp(url: str) -> str:
        if "@" not in url:
            return url
        left, right = url.split("@", 1)
        if "://" in left:
            proto, _creds = left.split("://", 1)
            return f"{proto}://***:***@{right}"
        return f"***:***@{right}"

    @staticmethod
    def image_bytes_to_b64(data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")
