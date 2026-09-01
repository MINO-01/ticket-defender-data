import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router as cluster_router
from core import dependencies

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """서버 라이프사이클 관리: 시작 및 종료 시 자원 할당/반환을 제어합니다."""
    yield
    # 서버 종료 시 DB 커넥션 안전 반환
    if dependencies._detector_instance:
        dependencies._detector_instance.close()
        logger.info("Neo4j 데이터베이스 커넥션이 안전하게 종료되었습니다.")

app = FastAPI(
    title="Ticket Fraud Analytics API", 
    version="1.0.0",
    lifespan=lifespan
)

allowed_origins = os.getenv("FRONTEND_URL", "http://localhost:3000").split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins, 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(cluster_router, prefix="/api/v1")