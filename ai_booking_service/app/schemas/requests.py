from pydantic import BaseModel, Field


class AssistantMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    conversation_id: str | None = None
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)


class AssistantConfirmRequest(BaseModel):
    conversation_id: str = Field(..., min_length=1, max_length=64)
    confirm: bool = True


class AssistantCancelRequest(BaseModel):
    conversation_id: str | None = None
    booking_id: str | None = None
    reason: str | None = Field(default=None, max_length=300)
