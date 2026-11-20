from fastapi import APIRouter, Header, HTTPException, Request, status

from app.config import get_settings
from app.schemas.requests import AssistantMessageRequest
from app.schemas.responses import AssistantResponse, ConversationDetailResponse
from app.services.backend_client import BackendClientError
from app.utils.validators import validate_message_length

router = APIRouter(prefix="/v1/assistant", tags=["Conversational Assistant"])


def _require_bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization Bearer token.",
        )
    return authorization


@router.post("/message", response_model=AssistantResponse)
async def post_assistant_message(
    payload: AssistantMessageRequest,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> AssistantResponse:
    auth_header = _require_bearer(authorization)
    settings = get_settings()
    try:
        validate_message_length(payload.message, settings.max_message_length)
    except ValueError as exc:
        return AssistantResponse(
            success=False,
            conversation_id=payload.conversation_id,
            status="INVALID_REQUEST",
            message=str(exc),
            error_code="INVALID_INPUT",
        )

    manager = request.app.state.conversation_manager
    request_id = getattr(request.state, "request_id", None)

    try:
        return await manager.handle_message(
            message=payload.message,
            authorization=auth_header,
            conversation_id=payload.conversation_id,
            latitude=payload.latitude,
            longitude=payload.longitude,
            location_label=payload.location_label,
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


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(
    conversation_id: str,
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> ConversationDetailResponse:
    auth_header = _require_bearer(authorization)
    repo = request.app.state.conversation_repository
    request_id = getattr(request.state, "request_id", None)

    try:
        record = await repo.get(
            conversation_id=conversation_id,
            authorization=auth_header,
            request_id=request_id,
        )
    except BackendClientError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    return ConversationDetailResponse(
        success=True,
        conversation_id=record.conversation_id,
        user_id=record.user_id,
        status=record.state,
        context=record.context,
        expires_at=record.expires_at,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )
