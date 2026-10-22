"""Safe, stable errors for the internal VLM routes."""

from fastapi.responses import JSONResponse


class VLMProblem(Exception):
    def __init__(self, status_code: int, code: str, title: str, detail: str):
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        super().__init__(code)


def problem_response(problem: VLMProblem, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=problem.status_code,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store", "X-Request-Id": request_id},
        content={
            "type": "about:blank",
            "title": problem.title,
            "status": problem.status_code,
            "code": problem.code,
            "detail": problem.detail,
            "requestId": request_id,
            "analysisStatus": "FAILED",
        },
    )
