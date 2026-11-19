from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.schemas.intents import ConversationState
from app.services.backend_client import BackendClient


@dataclass
class ConversationRecord:
    conversation_id: str
    user_id: str
    state: ConversationState
    context: dict[str, Any]
    expires_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def is_expired(self) -> bool:
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) >= exp


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _from_dict(data: dict[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=data["conversation_id"],
        user_id=data["user_id"],
        state=ConversationState(data["state"]),
        context=dict(data.get("context") or {}),
        expires_at=_parse_dt(data["expires_at"]),
        created_at=_parse_dt(data["created_at"]),
        updated_at=_parse_dt(data["updated_at"]),
    )


class ConversationRepository:
    """
    Persists conversation state through main_backend's authenticated internal APIs.
    Keeps ai_booking_service completely stateless and avoids direct DB access.
    """

    def __init__(self, backend_client: BackendClient, ttl_minutes: int | None = None):
        self.backend_client = backend_client
        self.ttl_minutes = ttl_minutes or get_settings().conversation_ttl_minutes

    async def create(
        self,
        authorization: str,
        initial_context: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> ConversationRecord:
        data = await self.backend_client.create_conversation(
            state=ConversationState.INITIAL.value,
            context=initial_context or {},
            ttl_minutes=self.ttl_minutes,
            authorization=authorization,
            request_id=request_id,
        )
        return _from_dict(data)

    async def get(
        self,
        conversation_id: str,
        authorization: str,
        request_id: str | None = None,
    ) -> ConversationRecord:
        data = await self.backend_client.get_conversation(
            conversation_id=conversation_id,
            authorization=authorization,
            request_id=request_id,
        )
        return _from_dict(data)

    async def save(
        self,
        record: ConversationRecord,
        authorization: str,
        request_id: str | None = None,
        refresh_ttl: bool = True,
    ) -> ConversationRecord:
        data = await self.backend_client.update_conversation(
            conversation_id=record.conversation_id,
            state=record.state.value,
            context=record.context,
            authorization=authorization,
            request_id=request_id,
            ttl_minutes=self.ttl_minutes if refresh_ttl else None,
        )
        return _from_dict(data)
