import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.internal import router as internal_router
from app.api.public import router as public_router
from app.config import get_settings
from app.database import Base, SessionLocal, engine
from app.seed import seed_database

settings = get_settings()
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [main_backend] %(message)s",
)
logger = logging.getLogger("main_backend")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    if settings.auto_seed_demo_data:
        db = SessionLocal()
        try:
            seed_database(db)
        finally:
            db.close()
    logger.info("Main Backend initialized with SQLite database.")
    yield


app = FastAPI(
    title="Service Booking Main Backend",
    version="1.0.0",
    description="Core service booking platform backed by SQLite with authenticated internal APIs.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def correlation_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%s",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
    )
    return response


@app.exception_handler(Exception)
async def safe_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.exception("Unhandled error request_id=%s path=%s", request_id, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "detail": "An unexpected internal error occurred.",
            "request_id": request_id,
        },
    )


app.include_router(auth_router)
app.include_router(public_router)
app.include_router(internal_router)


@app.get("/health", tags=["Health"])
def health() -> dict:
    return {"status": "ok", "service": "main_backend"}


@app.get("/ready", tags=["Health"])
def ready():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ready", "database": "sqlite_ok"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "database": "unavailable"},
        )
    finally:
        db.close()


@app.get("/", include_in_schema=False)
def serve_chat_ui():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"message": "Main Backend running. Visit /docs for OpenAPI documentation."}
