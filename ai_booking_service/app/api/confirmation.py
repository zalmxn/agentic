from fastapi import APIRouter, Header, HTTPException, Request, status

from app.schemas.requests import AssistantCancelRequest, AssistantConfirmRequest
from app.schemas.responses import AssistantResponse
from app.services.backend_client import BackendClientError

router = APIRouter(prefix="/v1/assistant", tags=["Booking Confirmation & Cancellation"])


def _require_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer token.",
        )
    return authorization


@router.post("/confirm", response_model=AssistantResponse)
async def confirm_booking_endpoint(
    payload: AssistantConfirmRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AssistantResponse:
    auth_header = _require_bearer(authorization)
    manager = request.app.state.conversation_manager
    request_id = getattr(request.state, "request_id", None)

    try:
        return await manager.confirm_booking(
            conversation_id=payload.conversation_id,
            confirm=payload.confirm,
            authorization=auth_header,
            request_id=request_id,
        )
    except BackendClientError as exc:
        if exc.status_code in (401, 403, 404):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return AssistantResponse(
            success=False,
            conversation_id=payload.conversation_id,
            status="ERROR",
            message=exc.message,
            error_code=exc.code,
        )


@router.post("/cancel", response_model=AssistantResponse)
async def cancel_booking_endpoint(
    payload: AssistantCancelRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AssistantResponse:
    auth_header = _require_bearer(authorization)
    manager = request.app.state.conversation_manager
    request_id = getattr(request.state, "request_id", None)

    try:
        return await manager.cancel_booking(
            conversation_id=payload.conversation_id,
            booking_id=payload.booking_id,
            reason=payload.reason,
            authorization=auth_header,
            request_id=request_id,
        )
    except BackendClientError as exc:
        if exc.status_code in (401, 403, 404):
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        return AssistantResponse(
            success=False,
            conversation_id=payload.conversation_id,
            status="ERROR",
            message=exc.message,
            error_code=exc.code,
        )
