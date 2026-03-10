from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np

from .face_engine import FaceEngine
from .storage import Storage
from .utils import now_iso


@dataclass
class Track:
    track_id: int
    bbox: tuple[int, int, int, int]
    last_seen_ts: float
    last_check_ts: float
    identity_id: str | None = None
    name: str = "Unknown"
    similarity: float = -1.0
    recognized_since_ts: float | None = None
    attendance_marked_ts: float | None = None


class StreamWorker:
    def __init__(self, storage: Storage, config: dict[str, Any], slug: str, camera_id: str, rtsp_url: str):
        self.storage = storage
        self.config = config
        self.slug = slug
        self.camera_id = camera_id
        self.rtsp_url = rtsp_url
        self.face_engine = FaceEngine()
        self._state_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.current_session_id: str | None = None
        self.gpu_enabled = self._detect_gpu()

    def _detect_gpu(self) -> bool:
        nvidia_ok = False
        try:
            res = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=2, check=False)
            nvidia_ok = res.returncode == 0
        except Exception:
            nvidia_ok = False
        ffmpeg_nvenc = False
        try:
            res = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            ffmpeg_nvenc = "h264_nvenc" in (res.stdout or "") + (res.stderr or "")
        except Exception:
            ffmpeg_nvenc = False
        return nvidia_ok and ffmpeg_nvenc

    def start_background(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.storage.save_stream_status(
            self.slug,
            {
                "running": True,
                "slug": self.slug,
                "camera_id": self.camera_id,
                "rtsp_url": self.rtsp_url,
                "hls": f"/hls/{self.slug}/index.m3u8",
                "attendance_session_id": self.current_session_id,
                "gpu_enabled": self.gpu_enabled,
                "updated_at": now_iso(),
            },
        )
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"stream-worker-{self.slug}")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
        self.storage.clear_stream_status(self.slug)

    def activate_attendance(self, duration_sec: int) -> dict[str, Any]:
        with self._state_lock:
            if self.current_session_id:
                session = self.storage.get_session(self.current_session_id)
                if session and session.get("status") == "active":
                    raise ValueError("Attendance session already active for this slug")
            session = self.storage.create_session(
                slug=self.slug,
                camera_id=self.camera_id,
                rtsp_url=self.rtsp_url,
                duration_sec=duration_sec,
                hls_path=f"/hls/{self.slug}/index.m3u8",
            )
            self.current_session_id = session["session_id"]
            self._save_status()
            return session

    def stop_attendance(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self._state_lock:
            active_session_id = session_id or self.current_session_id
            if not active_session_id:
                return None
            session = self.storage.get_session(active_session_id)
            if not session:
                self.current_session_id = None
                self._save_status()
                return None
            if session["status"] == "active":
                session["status"] = "ended"
                session["ended_at"] = now_iso()
                self.storage.update_session(active_session_id, session)
            if self.current_session_id == active_session_id:
                self.current_session_id = None
            self._save_status()
            return session

    def get_status(self) -> dict[str, Any]:
        with self._state_lock:
            status = self.storage.read_stream_status(self.slug)
            status.setdefault("slug", self.slug)
            status.setdefault("camera_id", self.camera_id)
            status.setdefault("hls", f"/hls/{self.slug}/index.m3u8")
            status.setdefault("attendance_session_id", self.current_session_id)
            status.setdefault("gpu_enabled", self.gpu_enabled)
            return status

    def _save_status(self) -> None:
        self.storage.save_stream_status(
            self.slug,
            {
                "running": True,
                "slug": self.slug,
                "camera_id": self.camera_id,
                "rtsp_url": self.rtsp_url,
                "hls": f"/hls/{self.slug}/index.m3u8",
                "attendance_session_id": self.current_session_id,
                "gpu_enabled": self.gpu_enabled,
                "updated_at": now_iso(),
            },
        )

    def _run(self) -> None:
        cap: cv2.VideoCapture | None = None
        ffmpeg_proc: subprocess.Popen | None = None
        frame_count = 0
        tracks: dict[int, Track] = {}
        next_track_id = 1
        failures = 0
        last_frame: np.ndarray | None = None
        last_open_ts = 0.0
        stream_conf = self.config["rtsp"]
        pipe_conf = self.config["pipeline"]
        detect_every = int(pipe_conf["detect_every_n_frames_gpu"] if self.gpu_enabled else pipe_conf["detect_every_n_frames_cpu"])

        while not self._stop_event.is_set():
            if cap is None and time.time() - last_open_ts >= stream_conf["reconnect_backoff_ms"] / 1000.0:
                cap = self._open_capture(self.rtsp_url)
                last_open_ts = time.time()
                failures = 0
                if cap is None:
                    self._save_status()
                    time.sleep(0.2)
                    continue

            if cap is None:
                time.sleep(0.2)
                continue

            ok, frame = cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= int(stream_conf["max_consecutive_failures"]):
                    cap.release()
                    cap = None
                    failures = 0
                if last_frame is not None and ffmpeg_proc is not None:
                    reconnect_frame = last_frame.copy()
                    self._draw_text(reconnect_frame, "Reconnecting...", (30, 40), (0, 0, 255))
                    self._push_frame(ffmpeg_proc, reconnect_frame)
                time.sleep(0.05)
                continue

            failures = 0
            last_frame = frame.copy()
            frame_count += 1
            frame_h, frame_w = frame.shape[:2]
            if ffmpeg_proc is None:
                ffmpeg_proc = self._start_ffmpeg(frame_w, frame_h)

            if frame_count % max(1, detect_every) == 0:
                detected = self.face_engine.detect_faces(frame)
                tracks, next_track_id = self._update_tracks(tracks, detected, next_track_id)

            cutoff = time.time() - 2.0
            tracks = {tid: tr for tid, tr in tracks.items() if tr.last_seen_ts >= cutoff}
            known = self.storage.all_identity_embeddings()

            with self._state_lock:
                session_id = self.current_session_id

            for tr in tracks.values():
                self._recognize_track(frame, tr, known, pipe_conf)
                self._draw_track(frame, tr)
                if session_id:
                    self._write_attendance_if_stable(session_id, tr, frame, pipe_conf)

            if session_id:
                self._auto_stop_session_if_needed(session_id)

            if ffmpeg_proc is not None:
                self._push_frame(ffmpeg_proc, frame)

        if cap is not None:
            cap.release()
        if ffmpeg_proc is not None:
            self._close_ffmpeg(ffmpeg_proc)

    def _open_capture(self, rtsp_url: str) -> cv2.VideoCapture | None:
        os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
        cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(rtsp_url)
        if not cap.isOpened():
            cap.release()
            return None
        return cap

    def _start_ffmpeg(self, width: int, height: int) -> subprocess.Popen | None:
        output_dir = self.storage.hls_dir(self.slug)
        playlist = output_dir / "index.m3u8"
        segment = output_dir / "seg_%05d.ts"
        codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"]
        if self.gpu_enabled:
            codec_args = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll"]
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-r",
            "25",
            "-i",
            "-",
            *codec_args,
            "-f",
            "hls",
            "-hls_time",
            str(self.config["hls"]["segment_time_sec"]),
            "-hls_list_size",
            str(self.config["hls"]["list_size"]),
            "-hls_flags",
            "delete_segments+append_list+omit_endlist",
            "-hls_segment_filename",
            str(segment),
            str(playlist),
        ]
        try:
            return subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except Exception:
            return None

    @staticmethod
    def _close_ffmpeg(proc: subprocess.Popen) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()

    @staticmethod
    def _push_frame(proc: subprocess.Popen, frame: np.ndarray) -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(frame.tobytes())
        except Exception:
            pass

    @staticmethod
    def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ax2, ay2 = ax + aw, ay + ah
        bx2, by2 = bx + bw, by + bh
        inter_x1 = max(ax, bx)
        inter_y1 = max(ay, by)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)
        if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
            return 0.0
        inter = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
        area_a = aw * ah
        area_b = bw * bh
        return inter / max(1.0, float(area_a + area_b - inter))

    def _update_tracks(
        self,
        tracks: dict[int, Track],
        detections: list[tuple[int, int, int, int]],
        next_track_id: int,
    ) -> tuple[dict[int, Track], int]:
        now_ts = time.time()
        assigned: set[int] = set()
        for det in detections:
            best_tid = None
            best_iou = 0.0
            for tid, tr in tracks.items():
                if tid in assigned:
                    continue
                overlap = self._iou(det, tr.bbox)
                if overlap > best_iou:
                    best_tid = tid
                    best_iou = overlap
            if best_tid is not None and best_iou >= 0.3:
                tracks[best_tid].bbox = det
                tracks[best_tid].last_seen_ts = now_ts
                assigned.add(best_tid)
            else:
                tracks[next_track_id] = Track(track_id=next_track_id, bbox=det, last_seen_ts=now_ts, last_check_ts=0.0)
                assigned.add(next_track_id)
                next_track_id += 1
        return tracks, next_track_id

    def _recognize_track(self, frame: np.ndarray, tr: Track, known: list[dict[str, Any]], conf: dict[str, Any]) -> None:
        now_ts = time.time()
        if now_ts - tr.last_check_ts < conf["recognize_cooldown_ms"] / 1000.0:
            return
        x, y, w, h = tr.bbox
        crop = frame[max(0, y) : y + h, max(0, x) : x + w]
        emb = self.face_engine.extract_embedding_from_crop(crop)
        tr.last_check_ts = now_ts
        if emb is None:
            tr.identity_id = None
            tr.name = "Unknown"
            tr.similarity = -1.0
            tr.recognized_since_ts = None
            return
        result = self.face_engine.match(emb, known, conf["similarity_threshold"])
        tr.similarity = result.similarity
        if result.identity_id:
            if tr.identity_id != result.identity_id or tr.recognized_since_ts is None:
                tr.recognized_since_ts = now_ts
            tr.identity_id = result.identity_id
            tr.name = result.name
        else:
            tr.identity_id = None
            tr.name = "Unknown"
            tr.recognized_since_ts = None

    def _write_attendance_if_stable(
        self,
        session_id: str,
        tr: Track,
        frame: np.ndarray,
        conf: dict[str, Any],
    ) -> None:
        if not tr.identity_id or tr.recognized_since_ts is None:
            return
        now_ts = time.time()
        if (now_ts - tr.recognized_since_ts) * 1000.0 < conf["min_stable_ms"]:
            return
        if tr.attendance_marked_ts and now_ts - tr.attendance_marked_ts < 1.0:
            return
        session = self.storage.get_session(session_id)
        if not session or session.get("status") != "active":
            return
        x, y, w, h = tr.bbox
        crop = frame[max(0, y) : y + h, max(0, x) : x + w].copy()
        snapshot_path = self.storage.save_recognition_snapshot(self.slug, tr.identity_id, crop)
        seen = session["seen"].get(tr.identity_id)
        if not seen:
            session["seen"][tr.identity_id] = {
                "id": tr.identity_id,
                "name": tr.name,
                "first_seen": now_iso(),
                "last_seen": now_iso(),
                "count": 1,
                "best_similarity": float(tr.similarity),
                "latest_snapshot": snapshot_path,
            }
        else:
            seen["last_seen"] = now_iso()
            seen["count"] = int(seen.get("count", 0)) + 1
            seen["best_similarity"] = float(max(float(seen.get("best_similarity", -1.0)), tr.similarity))
            if snapshot_path:
                seen["latest_snapshot"] = snapshot_path
        session["events"].append(
            {
                "ts": now_iso(),
                "type": "recognized",
                "id": tr.identity_id,
                "name": tr.name,
                "similarity": float(tr.similarity),
                "snapshot": snapshot_path,
            }
        )
        self.storage.update_session(session_id, session)
        tr.attendance_marked_ts = now_ts

    def _auto_stop_session_if_needed(self, session_id: str) -> None:
        session = self.storage.get_session(session_id)
        if not session or session.get("status") != "active":
            return
        auto_stop_at = datetime.fromisoformat(session["auto_stop_at"])
        if datetime.now(timezone.utc) >= auto_stop_at:
            self.stop_attendance(session_id)

    @staticmethod
    def _draw_text(frame: np.ndarray, text: str, origin: tuple[int, int], color: tuple[int, int, int]) -> None:
        cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

    def _draw_track(self, frame: np.ndarray, tr: Track) -> None:
        x, y, w, h = tr.bbox
        color = (0, 180, 0) if tr.identity_id else (0, 0, 255)
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        label = f"{tr.identity_id or 'UNK'} | {tr.name} | {tr.similarity:.2f}"
        self._draw_text(frame, label, (x, max(20, y - 8)), color)


class StreamManager:
    def __init__(self, storage: Storage, config: dict[str, Any]):
        self.storage = storage
        self.config = config
        self.workers: dict[str, StreamWorker] = {}
        for item in config.get("streams", []):
            slug = item["slug"]
            self.workers[slug] = StreamWorker(
                storage=storage,
                config=config,
                slug=slug,
                camera_id=item.get("camera_id", slug),
                rtsp_url=item["rtsp_url"],
            )

    def start_background(self) -> None:
        for worker in self.workers.values():
            worker.start_background()

    def shutdown(self) -> None:
        for worker in self.workers.values():
            worker.shutdown()

    def list_streams(self) -> list[dict[str, Any]]:
        configured: list[dict[str, Any]] = []
        for item in self.config.get("streams", []):
            status = self.storage.read_stream_status(item["slug"])
            configured.append(
                {
                    "slug": item["slug"],
                    "camera_id": item.get("camera_id", item["slug"]),
                    "rtsp_url": self.storage.sanitize_rtsp(item["rtsp_url"]),
                    "running": status.get("running", False),
                    "attendance_session_id": status.get("attendance_session_id"),
                    "hls": status.get("hls", f"/hls/{item['slug']}/index.m3u8"),
                    "gpu_enabled": status.get("gpu_enabled", False),
                }
            )
        return configured

    def get_stream(self, slug: str) -> StreamWorker:
        worker = self.workers.get(slug)
        if worker is None:
            raise KeyError(slug)
        return worker

    def get_status(self, slug: str) -> dict[str, Any]:
        return self.get_stream(slug).get_status()

    def activate_attendance(self, slug: str) -> dict[str, Any]:
        duration = int(self.config["pipeline"].get("attendance_duration_sec", 60))
        return self.get_stream(slug).activate_attendance(duration_sec=duration)

    def stop_attendance(self, session_id: str) -> dict[str, Any] | None:
        session = self.storage.get_session(session_id)
        if not session:
            return None
        return self.get_stream(session["slug"]).stop_attendance(session_id)
