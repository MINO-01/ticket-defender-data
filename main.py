import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
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