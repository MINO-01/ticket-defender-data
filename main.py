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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logging.getLogger("neo4j").setLevel(logging.WARNING)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    if dependencies._graph_service_instance:
        await dependencies._graph_service_instance.close()

app = FastAPI(
    title="Ticket Fraud Analytics SaaS API", 
    version="2.0.0",
    lifespan=lifespan
)


@app.exception_handler(RequestValidationError)
async def track1_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path != "/api/v1/clusters/analyze":
        return await request_validation_exception_handler(request, exc)

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
        elif field == "requestId":
            codes.add("INVALID_UUID")
        elif field in token_fields:
            codes.add("INVALID_CONNECTION_TOKEN")
        elif field in identifier_fields:
            codes.add("INVALID_IDENTIFIER")
        else:
            codes.add("INVALID_REQUEST")
    return JSONResponse(status_code=422, content={"detail": [{"code": code} for code in sorted(codes)]})

allowed_origins = os.getenv("FRONTEND_URL", "http://localhost:3000").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(graph_routes.router, prefix="/api/v1")
app.include_router(vlm_routes.router, prefix="/api/v1")
