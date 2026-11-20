import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.assistant import router as assistant_router
from app.api.confirmation import router as confirmation_router
from app.api.health import router as health_router
from app.config import get_settings
from app.core.intent_parser import IntentParser
from app.core.ranking_engine import RankingEngine
from app.core.service_dictionary import get_service_dictionary
from app.logging_config import configure_logging
from app.middleware import request_context_and_rate_limit_middleware
from app.repositories.conversation_repository import ConversationRepository
from app.services.backend_client import BackendClient
from app.services.booking_coordinator import BookingCoordinator
from app.services.conversation_manager import ConversationManager
from app.services.search_coordinator import SearchCoordinator

settings = get_settings()
configure_logging(settings.log_level)
logger = logging.getLogger("ai_booking_service.main")


def create_app(backend_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        backend_client = BackendClient(transport=backend_transport)
        repo = ConversationRepository(backend_client=backend_client)
        search_coord = SearchCoordinator(
            backend_client=backend_client,
            ranking_engine=RankingEngine(),
        )
        booking_coord = BookingCoordinator(backend_client=backend_client)
        manager = ConversationManager(
            repository=repo,
            search_coordinator=search_coord,
            booking_coordinator=booking_coord,
            intent_parser=IntentParser(timezone_name=settings.default_timezone),
            service_dict=get_service_dictionary(),
        )

        application.state.backend_client = backend_client
        application.state.conversation_repository = repo
        application.state.conversation_manager = manager
        logger.info("AI Booking Microservice started (rule-based NLU, zero LLM).")
        yield
        await backend_client.close()

    application = FastAPI(
        title="Rule-Based AI Service Booking Microservice",
        version="1.0.0",
        description=(
            "Production-ready deterministic rule-based conversational service booking engine. "
            "No LLMs, no external AI APIs, no GPU dependencies."
        ),
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.middleware("http")(request_context_and_rate_limit_middleware)

    @application.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "success": False,
                "conversation_id": None,
                "status": "ERROR",
                "message": str(exc.detail),
                "error_code": f"HTTP_{exc.status_code}",
                "requires_confirmation": False,
                "missing_fields": [],
            },
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception("unhandled_exception", extra={"request_id": request_id})
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "conversation_id": None,
                "status": "ERROR",
                "message": "An unexpected error occurred while processing your request.",
                "error_code": "INTERNAL_ERROR",
                "requires_confirmation": False,
                "missing_fields": [],
            },
        )

    application.include_router(health_router)
    application.include_router(assistant_router)
    application.include_router(confirmation_router)
    return application


app = create_app()


