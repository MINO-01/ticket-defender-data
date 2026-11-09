import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from api.routes import graph_routes, vlm_routes
from core import dependencies
from core.vlm_errors import VLMBodyTooLarge, VLMProblem, problem_response
from core.track1_security import Track1AuthGate
from core.vlm_security import VLMPreBodyGate, VLM_PATHS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("google.genai").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if dependencies._graph_service_instance:
        await dependencies._graph_service_instance.close()
    if dependencies._vlm_service_instance:
        await dependencies._vlm_service_instance.close()

app = FastAPI(
    title="Ticket Fraud Analytics SaaS API", 
    version="2.0.0",
    lifespan=lifespan
)


TRACK1_V2_PATH = "/api/v2/clusters/analyze"
_TRACK1_V2_CONTRACT_DETAILS = {
    "DUPLICATE_PERFORMANCE_SCOPE": "Each performance scope must be unique",
    "TICKET_SCOPE_MISMATCH": "Each ticket must reference exactly one matching performance scope",
    "POLICY_TIME_INVALID": "effectiveUntil must be after effectiveFrom",
    "INVALID_POLICY": "Policy does not match the contract",
}


def _track1_v2_contract_error(errors) -> dict | None:
    """계약에 정의된 범위와 정책 오류만 ContractError로 돌려줍니다. 입력값은 담지 않습니다."""

    types = {error["type"] for error in errors}
    for code, detail in _TRACK1_V2_CONTRACT_DETAILS.items():
        if code in types:
            return {"code": code, "detail": detail}
    if any("policy" in error["loc"] for error in errors):
        return {"code": "INVALID_POLICY", "detail": _TRACK1_V2_CONTRACT_DETAILS["INVALID_POLICY"]}
    return None


@app.exception_handler(RequestValidationError)
async def track1_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path in VLM_PATHS:
        too_large = any(
            error["type"] == "string_too_long" and error["loc"][-1] in {"image_base64", "imageBase64"}
            for error in exc.errors()
        )
        problem = (
            VLMProblem(413, "IMAGE_TOO_LARGE", "Image too large", "Image exceeds the encoded limit")
            if too_large else
            VLMProblem(422, "INVALID_REQUEST", "Invalid request", "Request does not match the contract")
        )
        return problem_response(problem, request.state.request_id)
    if request.url.path not in {"/api/v1/clusters/analyze", TRACK1_V2_PATH}:
        return await request_validation_exception_handler(request, exc)
    if request.url.path == TRACK1_V2_PATH:
        contract_error = _track1_v2_contract_error(exc.errors())
        if contract_error is not None:
            return JSONResponse(status_code=422, content=contract_error)

    token_fields = {"paymentHash", "addressHash", "deviceIdHash", "ipHash"}
    identifier_fields = {"accountId", "reservationNo", "eventId"}
    codes = set()
    for error in exc.errors():
        error_type = error["type"]
        field = error["loc"][-1]
        if error_type == "json_invalid":
            codes.add("INVALID_JSON")
        elif error_type in {"duplicate_ticket", "missing_connection_token", "invalid_identifier"}:
            codes.add(error_type.upper())
        elif field == "quantityInputType":
            codes.add("UNSUPPORTED_QUANTITY_INPUT_TYPE")
        elif field == "requestId":
            codes.add("INVALID_UUID")
        elif field in token_fields:
            codes.add("INVALID_CONNECTION_TOKEN")
        elif field in identifier_fields:
            codes.add("INVALID_IDENTIFIER")
        else:
            codes.add("INVALID_REQUEST")
    return JSONResponse(status_code=422, content={"detail": [{"code": code} for code in sorted(codes)]})


@app.exception_handler(VLMProblem)
async def vlm_problem_error(request: Request, exc: VLMProblem):
    return problem_response(exc, request.state.request_id)


@app.exception_handler(VLMBodyTooLarge)
async def vlm_body_too_large_error(request: Request, exc: VLMBodyTooLarge):
    return problem_response(
        VLMProblem(413, "REQUEST_TOO_LARGE", "Request too large", "Request exceeds the body limit"),
        request.state.request_id,
    )

allowed_origins = os.getenv("FRONTEND_URL", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(VLMPreBodyGate)
# Track 1 인증은 VLM 게이트와 경로가 겹치지 않으며 가장 바깥에서 본문 수신 전에 실행됩니다.
app.add_middleware(Track1AuthGate)

app.include_router(graph_routes.router, prefix="/api/v1")
app.include_router(graph_routes.v2_router, prefix="/api/v2")
app.include_router(vlm_routes.router, prefix="/api/v1")
