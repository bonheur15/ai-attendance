from __future__ import annotations

from fastapi import Header, HTTPException, status


def api_key_dependency(expected_api_key: str):
    def verify(x_api_key: str | None = Header(default=None)) -> None:
        if not expected_api_key:
            return
        if x_api_key != expected_api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing X-API-Key header",
            )

    return verify
