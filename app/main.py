from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.routers import admin, auth, credits, feedback, knowledge, learning, qa
from app.schemas import HealthResponse
from app.seed import seed_demo_content


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if settings.app_env in ("development", "test"):
        Base.metadata.create_all(bind=engine)
        with SessionLocal() as db:
            seed_demo_content(db)
    yield


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="青葵计划独立 Android 首版后端",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

for router in (
    auth.router,
    knowledge.router,
    qa.router,
    learning.router,
    credits.router,
    credits.admin_router,
    feedback.router,
    admin.router,
):
    app.include_router(router, prefix=settings.api_prefix)


@app.get("/health", response_model=HealthResponse, tags=["系统"])
def health() -> HealthResponse:
    database_status = "ok"
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        database_status = "unavailable"
    return HealthResponse(
        status="ok" if database_status == "ok" else "degraded",
        database=database_status,
        ai_provider=settings.ai_provider,
        ai_model=settings.deepseek_model if settings.ai_provider == "deepseek" else "grounded-stub",
        ai_ready=settings.ai_ready,
    )
