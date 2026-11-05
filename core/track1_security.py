"""Track 1 분석 API 전용 내부 인증 게이트입니다.

요청 본문을 읽거나 그래프 서비스를 만들기 전에 Spring 내부 호출자를 확인합니다.
Track 2 VLM 인증과 비밀키, 헤더, 적용 경로를 공유하지 않습니다.
"""

import hmac
import json
import logging
import os


logger = logging.getLogger(__name__)

TRACK1_AUTH_HEADER = b"x-td-track1-key"
TRACK1_SECRET_ENV = "TRACK1_INTERNAL_SHARED_SECRET"
TRACK2_SECRET_ENV = "VLM_INTERNAL_SHARED_SECRET"
TRACK1_MIN_SECRET_BYTES = 32
TRACK1_PROTECTED_PATHS = frozenset({"/api/v1/clusters/analyze"})


def _is_protected(scope) -> bool:
    """Starlette 라우팅과 같은 기준으로 root_path를 제외한 경로를 비교합니다."""
    path = scope.get("path", "")
    root_path = scope.get("root_path", "")
    candidates = {path}
    if root_path and path.startswith(root_path + "/"):
        candidates.add(path[len(root_path):])
    return any(candidate.rstrip("/") in TRACK1_PROTECTED_PATHS for candidate in candidates)


def _configured_secret() -> bytes | None:
    """32바이트 이상이고 Track 2 비밀키와 다른 경우에만 Track 1 비밀키를 반환합니다."""
    secret = os.getenv(TRACK1_SECRET_ENV, "").encode("utf-8")
    if len(secret) < TRACK1_MIN_SECRET_BYTES:
        return None
    track2_secret = os.getenv(TRACK2_SECRET_ENV, "").encode("utf-8")
    if track2_secret and hmac.compare_digest(secret, track2_secret):
        return None
    return secret


async def _reject(send, status: int, code: str) -> None:
    body = json.dumps({"detail": [{"code": code}]}).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
            (b"cache-control", b"no-store"),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class Track1AuthGate:
    """보호 경로에 한해 본문 수신 전에 단일 내부 공유키 헤더를 검증합니다."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not _is_protected(scope):
            await self.app(scope, receive, send)
            return

        secret = _configured_secret()
        if secret is None:
            # 설정 오류는 값 없이 유형만 기록합니다.
            logger.error("Track 1 internal authentication is not configured")
            await _reject(send, 503, "TRACK1_AUTH_UNCONFIGURED")
            return

        credentials = [value for name, value in scope.get("headers", []) if name.lower() == TRACK1_AUTH_HEADER]
        if len(credentials) != 1 or not hmac.compare_digest(credentials[0], secret):
            logger.warning("Track 1 internal authentication failed")
            await _reject(send, 401, "INTERNAL_AUTH_REQUIRED")
            return

        await self.app(scope, receive, send)
