"""Pre-body admission gate for both internal VLM endpoints."""

import hmac
import os
from uuid import UUID, uuid4

from core.vlm_errors import VLMBodyTooLarge, VLMProblem, problem_response


VLM_PATHS = frozenset({"/api/v1/parse/ticket-image", "/api/v1/parse/listing-context"})
MAX_REQUEST_BODY_BYTES = 7 * 1024 * 1024


def _trace_id(headers: list[tuple[bytes, bytes]]) -> tuple[str, bool]:
    values = [value for name, value in headers if name.lower() == b"x-request-id"]
    if len(values) == 1 and len(values[0]) <= 36:
        try:
            value = values[0].decode("ascii")
            UUID(value)
            return value, True
        except (UnicodeDecodeError, ValueError):
            pass
    return str(uuid4()), False


class VLMPreBodyGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST" or scope.get("path") not in VLM_PATHS:
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        request_id, supplied_trace = _trace_id(headers)
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["supplied_trace"] = supplied_trace

        async def reject(problem: VLMProblem):
            await problem_response(problem, request_id)(scope, receive, send)

        if os.getenv("VLM_ENABLED", "false").lower() != "true":
            await reject(VLMProblem(503, "VLM_DISABLED", "Analysis unavailable", "Analysis is unavailable"))
            return
        secret = os.getenv("VLM_INTERNAL_SHARED_SECRET", "")
        if len(secret.encode("utf-8")) < 32:
            await reject(VLMProblem(503, "VLM_CONFIGURATION_ERROR", "Analysis unavailable", "Analysis is unavailable"))
            return
        credentials = [value for name, value in headers if name.lower() == b"x-td-internal-key"]
        if len(credentials) != 1 or not hmac.compare_digest(credentials[0], secret.encode("utf-8")):
            await reject(VLMProblem(401, "INTERNAL_AUTH_REQUIRED", "Authentication required", "Internal authentication failed"))
            return

        lengths = [value for name, value in headers if name.lower() == b"content-length"]
        if len(lengths) > 1:
            await reject(VLMProblem(400, "INVALID_REQUEST", "Invalid request", "Invalid request headers"))
            return
        if lengths:
            try:
                length = int(lengths[0])
            except ValueError:
                length = -1
            if length < 0:
                await reject(VLMProblem(400, "INVALID_REQUEST", "Invalid request", "Invalid request headers"))
                return
            if length > MAX_REQUEST_BODY_BYTES:
                await reject(VLMProblem(413, "REQUEST_TOO_LARGE", "Request too large", "Request exceeds the body limit"))
                return

        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > MAX_REQUEST_BODY_BYTES:
                    raise VLMBodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)
