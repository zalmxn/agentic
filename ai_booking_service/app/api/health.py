from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "ai_booking_service", "engine": "rule_based_nlu"}


@router.get("/ready")
async def ready(request: Request):
    backend_client = request.app.state.backend_client
    backend_ok = await backend_client.check_health()
    if not backend_ok:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "service": "ai_booking_service",
                "backend_dependency": "unavailable",
            },
        )
    return {
        "status": "ready",
        "service": "ai_booking_service",
        "backend_dependency": "ok",
    }
