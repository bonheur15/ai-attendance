from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


@dataclass
class RecognitionResult:
    identity_id: str | None
    name: str
    similarity: float


class FaceEngine:
    """Detection + embedding with optional face_recognition backend and OpenCV fallback."""

    def __init__(self):
        cascade_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        self.detector = cv2.CascadeClassifier(cascade_path)
        self.model_name = "opencv-hist-fallback"
        self._fr = None
        try:
            import face_recognition  # type: ignore

            self._fr = face_recognition
            self.model_name = "face_recognition"
        except Exception:
            self._fr = None

    def detect_faces(self, frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = self.detector.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )
        out: list[tuple[int, int, int, int]] = []
        for (x, y, w, h) in faces:
            out.append((int(x), int(y), int(w), int(h)))
        return out

    def extract_embedding_from_crop(self, crop_bgr: np.ndarray) -> list[float] | None:
        if crop_bgr.size == 0:
            return None
        if self._fr is not None:
            rgb = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)
            encs = self._fr.face_encodings(rgb)
            if encs:
                vec = encs[0].astype(np.float32)
                return vec.tolist()
        # Fallback to deterministic normalized histogram embedding.
        hsv = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 8], [0, 180, 0, 256, 0, 256]).flatten()
        norm = np.linalg.norm(hist) + 1e-8
        vec = (hist / norm).astype(np.float32)
        return vec.tolist()

    def extract_embeddings_from_image(self, image_bgr: np.ndarray) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for x, y, w, h in self.detect_faces(image_bgr):
            crop = image_bgr[y : y + h, x : x + w]
            emb = self.extract_embedding_from_crop(crop)
            if emb is not None:
                embeddings.append(emb)
        return embeddings

    @staticmethod
    def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
        if len(vec_a) != len(vec_b) or not vec_a:
            return -1.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        na = math.sqrt(sum(a * a for a in vec_a))
        nb = math.sqrt(sum(b * b for b in vec_b))
        if na == 0 or nb == 0:
            return -1.0
        return dot / (na * nb)

    def match(self, embedding: list[float], known: list[dict[str, Any]], threshold: float) -> RecognitionResult:
        best = RecognitionResult(identity_id=None, name="Unknown", similarity=-1.0)
        for item in known:
            sim = self.cosine_similarity(embedding, item["avg_embedding"])
            if sim > best.similarity:
                best = RecognitionResult(identity_id=item["id"], name=item["name"], similarity=sim)
        if best.similarity >= threshold and best.identity_id:
            return best
        return RecognitionResult(identity_id=None, name="Unknown", similarity=float(best.similarity))
