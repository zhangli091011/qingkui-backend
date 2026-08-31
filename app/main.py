from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db import Base, SessionLocal, engine
from app.rate_limit import RateLimitMiddleware
from app.routers import admin, auth, contributions, credits, feedback, knowledge, learning, mistakes, organizations, qa
from app.schemas import HealthResponse
from app.seed import seed_demo_content
from app.admin_ui import admin_page


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
app.add_middleware(RateLimitMiddleware)
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
    mistakes.router,
    organizations.router,
    contributions.router,
    contributions.admin_router,
    credits.router,
    credits.admin_router,
    feedback.router,
    admin.router,
):
    app.include_router(router, prefix=settings.api_prefix)


@app.get("/admin", include_in_schema=False)
def admin_console():
    return admin_page()


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
        ai_enabled=settings.ai_enabled,
        ai_provider=settings.ai_provider,
        ai_model=settings.deepseek_model if settings.ai_provider == "deepseek" else "grounded-stub",
        ai_ready=settings.ai_ready,
        retrieval_provider=settings.retrieval_provider,
        retrieval_model=(
            settings.dashscope_embedding_model
            if settings.retrieval_provider == "bailian"
            else "lexical"
        ),
        retrieval_ready=settings.retrieval_ready,
    )
