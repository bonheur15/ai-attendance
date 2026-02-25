from __future__ import annotations

from pydantic import BaseModel, Field


class IdentityUpsert(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)


class StreamStartRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)
    rtsp_url: str = Field(min_length=1, max_length=2048)


class AttendanceStartRequest(BaseModel):
    camera_id: str = Field(min_length=1, max_length=64)


class AttendanceStopRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
