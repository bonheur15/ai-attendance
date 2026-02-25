from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
    def __init__(self, storage: Storage, config: dict[str, Any]):
        self.storage = storage
        self.config = config
        self.face_engine = FaceEngine()
        self._state_lock = threading.RLock()
        self._command_queue: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.camera_id: str | None = None
        self.rtsp_url: str | None = None
        self.current_session_id: str | None = None
        self.gpu_enabled = self._detect_gpu()

    def _detect_gpu(self) -> bool:
        nvidia_ok = False
        try:
            res = subprocess.run(
                ["nvidia-smi"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
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
        self._thread = threading.Thread(target=self._run, daemon=True, name="stream-worker")
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)

    def enqueue(self, cmd: str, payload: dict[str, Any] | None = None) -> None:
        self._command_queue.put((cmd, payload or {}))

    def get_status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "running": self.running,
                "camera_id": self.camera_id,
                "hls": "/hls/live/index.m3u8" if self.running else None,
                "attendance_session_id": self.current_session_id,
                "gpu_enabled": self.gpu_enabled,
            }

    def _handle_command(self, cmd: str, payload: dict[str, Any]) -> None:
        with self._state_lock:
            if cmd == "start_stream":
                self.running = True
                self.camera_id = payload["camera_id"]
                self.rtsp_url = payload["rtsp_url"]
                self.storage.write_active_stream(
                    {"running": True, "camera_id": self.camera_id, "rtsp_url": self.rtsp_url, "started_at": now_iso()}
                )
            elif cmd == "stop_stream":
                self.running = False
                self.camera_id = None
                self.rtsp_url = None
                self.current_session_id = None
                self.storage.clear_active_stream()
            elif cmd == "start_attendance":
                self.current_session_id = payload["session_id"]
            elif cmd == "stop_attendance":
                if self.current_session_id == payload["session_id"]:
                    self.current_session_id = None

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
        detect_every = (
            int(pipe_conf["detect_every_n_frames_gpu"])
            if self.gpu_enabled
            else int(pipe_conf["detect_every_n_frames_cpu"])
        )
        while not self._stop_event.is_set():
            try:
                cmd, payload = self._command_queue.get_nowait()
                self._handle_command(cmd, payload)
            except queue.Empty:
                pass

            with self._state_lock:
                running = self.running
                rtsp_url = self.rtsp_url
                camera_id = self.camera_id
                session_id = self.current_session_id

            if not running or not rtsp_url:
                if cap is not None:
                    cap.release()
                    cap = None
                if ffmpeg_proc is not None:
                    self._close_ffmpeg(ffmpeg_proc)
                    ffmpeg_proc = None
                tracks = {}
                frame_count = 0
                time.sleep(0.1)
                continue

            now = time.time()
            if cap is None and now - last_open_ts >= stream_conf["reconnect_backoff_ms"] / 1000.0:
                cap = self._open_capture(rtsp_url)
                last_open_ts = now
                failures = 0
                if cap is None:
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
                    self._draw_text(last_frame, "Reconnecting...", (30, 40), (0, 0, 255))
                    self._push_frame(ffmpeg_proc, last_frame)
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

            # Keep only recently seen tracks to avoid stale overlays.
            cutoff = time.time() - 2.0
            tracks = {tid: tr for tid, tr in tracks.items() if tr.last_seen_ts >= cutoff}

            known = self.storage.all_identity_embeddings()
            for tr in tracks.values():
                self._recognize_track(frame, tr, known, pipe_conf)
                self._draw_track(frame, tr)
                if session_id:
                    self._write_attendance_if_stable(session_id, tr, pipe_conf)

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
        output_dir = Path(self.config["hls"]["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
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
        now = time.time()
        assigned: set[int] = set()
        for det in detections:
            best_tid = None
            best_iou = 0.0
            for tid, tr in tracks.items():
                if tid in assigned:
                    continue
                v = self._iou(det, tr.bbox)
                if v > best_iou:
                    best_tid = tid
                    best_iou = v
            if best_tid is not None and best_iou >= 0.3:
                tr = tracks[best_tid]
                tr.bbox = det
                tr.last_seen_ts = now
                assigned.add(best_tid)
            else:
                tracks[next_track_id] = Track(track_id=next_track_id, bbox=det, last_seen_ts=now, last_check_ts=0.0)
                assigned.add(next_track_id)
                next_track_id += 1
        return tracks, next_track_id

    def _recognize_track(self, frame: np.ndarray, tr: Track, known: list[dict[str, Any]], conf: dict[str, Any]) -> None:
        now_ts = time.time()
        cooldown = conf["recognize_cooldown_ms"] / 1000.0
        if now_ts - tr.last_check_ts < cooldown:
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
            if tr.identity_id == result.identity_id and tr.recognized_since_ts is not None:
                pass
            else:
                tr.recognized_since_ts = now_ts
            tr.identity_id = result.identity_id
            tr.name = result.name
        else:
            tr.identity_id = None
            tr.name = "Unknown"
            tr.recognized_since_ts = None

    def _write_attendance_if_stable(self, session_id: str, tr: Track, conf: dict[str, Any]) -> None:
        if not tr.identity_id or tr.recognized_since_ts is None:
            return
        now_ts = time.time()
        stable_ms = (now_ts - tr.recognized_since_ts) * 1000.0
        if stable_ms < conf["min_stable_ms"]:
            return
        if tr.attendance_marked_ts and now_ts - tr.attendance_marked_ts < 1.0:
            return
        session = self.storage.get_session(session_id)
        if not session or session["status"] != "active":
            return
        sid = tr.identity_id
        seen = session["seen"].get(sid)
        if not seen:
            session["seen"][sid] = {
                "id": sid,
                "name": tr.name,
                "first_seen": now_iso(),
                "last_seen": now_iso(),
                "count": 1,
                "best_similarity": float(tr.similarity),
            }
        else:
            seen["last_seen"] = now_iso()
            seen["count"] = int(seen.get("count", 0)) + 1
            seen["best_similarity"] = float(max(float(seen.get("best_similarity", -1.0)), tr.similarity))
        session["events"].append(
            {
                "ts": now_iso(),
                "type": "recognized",
                "id": sid,
                "name": tr.name,
                "similarity": float(tr.similarity),
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
            session["status"] = "ended"
            session["ended_at"] = now_iso()
            self.storage.update_session(session_id, session)
            self.enqueue("stop_attendance", {"session_id": session_id})

    @staticmethod
    def _draw_text(frame: np.ndarray, text: str, origin: tuple[int, int], color: tuple[int, int, int]) -> None:
        cv2.putText(frame, text, origin, cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2, cv2.LINE_AA)

    def _draw_track(self, frame: np.ndarray, tr: Track) -> None:
        x, y, w, h = tr.bbox
        color = (0, 180, 0) if tr.identity_id else (0, 0, 255)
        cv2.rectangle(frame, (x, y), (x + w, y + h), color, 2)
        label = f"{tr.identity_id or 'UNK'} | {tr.name} | {tr.similarity:.2f}"
        self._draw_text(frame, label, (x, max(20, y - 8)), color)
