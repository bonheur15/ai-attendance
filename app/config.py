from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppConfig:
    raw: dict

    @property
    def api(self) -> dict:
        return self.raw["api"]

    @property
    def rtsp(self) -> dict:
        return self.raw["rtsp"]

    @property
    def pipeline(self) -> dict:
        return self.raw["pipeline"]

    @property
    def hls(self) -> dict:
        return self.raw["hls"]


def load_config(path: str | Path = "config.json") -> AppConfig:
    config_path = Path(os.getenv("CONFIG_PATH", path))
    with config_path.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    return AppConfig(raw=raw)
