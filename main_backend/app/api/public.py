import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, get_db
from app.models import Provider, Service, User
from app.schemas import (
    BookingResponse,
    ChatCancelProxyRequest,
    ChatConfirmProxyRequest,
    ChatMessageProxyRequest,
    ProviderSummary,
    ServiceResponse,
)
from app.security import get_current_user, resolve_user_from_bearer
from app.services.booking_service import list_bookings_for_user

router = APIRouter(prefix="/api/v1", tags=["Public Customer APIs"])


@router.get("/services", response_model=list[ServiceResponse])
def list_services(db: Session = Depends(get_db)) -> list[ServiceResponse]:
    services = db.query(Service).order_by(Service.name.asc()).all()
    return [
        ServiceResponse(
            id=s.id,
            code=s.code,
            name=s.name,
            category=s.category,
            description=s.description,
            default_duration_minutes=s.default_duration_minutes,
        )
        for s in services
    ]


@router.get("/providers", response_model=list[ProviderSummary])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderSummary]:
    providers = db.query(Provider).filter(Provider.is_active.is_(True)).order_by(Provider.name.asc()).all()
    return [
        ProviderSummary(
            provider_id=p.id,
            provider_name=p.name,
            location_name=p.location_name,
            address=p.address,
            latitude=p.latitude,
            longitude=p.longitude,
            rating=p.rating,
            review_count=p.review_count,
            work_start_time=p.work_start_time,
            work_end_time=p.work_end_time,
        )
        for p in providers
    ]


@router.get("/bookings", response_model=list[BookingResponse])
def list_my_bookings(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[BookingResponse]:
    return list_bookings_for_user(db, current_user.id)


# ---------- Gateway Proxy to AI Booking Microservice ----------
async def _forward_to_ai_service(
    request: Request,
    method: str,
    path: str,
    authorization: str | None,
    json_body: dict | None = None,
) -> JSONResponse:
    settings = get_settings()
    headers = {}
    if authorization:
        headers["Authorization"] = authorization
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        headers["X-Request-ID"] = request_id

    url = f"{settings.ai_service_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=settings.ai_request_timeout) as client:
            resp = await client.request(method, url, headers=headers, json=json_body)
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Booking assistant service timed out.",
        ) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Booking assistant service is currently unavailable.",
        ) from exc

    try:
        data = resp.json()
    except ValueError:
        data = {"success": False, "message": "Unexpected response from booking assistant."}
    return JSONResponse(status_code=resp.status_code, content=data)


def _verify_customer_before_proxy(authorization: str | None) -> None:
    """Validate customer JWT and close DB session immediately before outbound HTTP proxy call."""
    db = SessionLocal()
    try:
        resolve_user_from_bearer(authorization, db)
    finally:
        db.close()


@router.post("/chat/message")
async def proxy_chat_message(
    payload: ChatMessageProxyRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _verify_customer_before_proxy(authorization)
    return await _forward_to_ai_service(
        request,
        "POST",
        "/v1/assistant/message",
        authorization,
        payload.model_dump(exclude_none=True),
    )


@router.post("/chat/confirm")
async def proxy_chat_confirm(
    payload: ChatConfirmProxyRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _verify_customer_before_proxy(authorization)
    return await _forward_to_ai_service(
        request,
        "POST",
        "/v1/assistant/confirm",
        authorization,
        payload.model_dump(exclude_none=True),
    )


@router.post("/chat/cancel")
async def proxy_chat_cancel(
    payload: ChatCancelProxyRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _verify_customer_before_proxy(authorization)
    return await _forward_to_ai_service(
        request,
        "POST",
        "/v1/assistant/cancel",
        authorization,
        payload.model_dump(exclude_none=True),
    )


@router.get("/chat/conversations/{conversation_id}")
async def proxy_get_conversation(
    conversation_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    _verify_customer_before_proxy(authorization)
    return await _forward_to_ai_service(
        request,
        "GET",
        f"/v1/assistant/conversations/{conversation_id}",
        authorization,
    )
