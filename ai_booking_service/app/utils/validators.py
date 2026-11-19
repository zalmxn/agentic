import re

BOOKING_CODE_RE = re.compile(r"\b(bk-\d{8}-[a-z0-9]{4,8})\b", re.IGNORECASE)
UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


def validate_message_length(message: str, max_length: int) -> None:
    if not message or not message.strip():
        raise ValueError("Message cannot be empty.")
    if len(message) > max_length:
        raise ValueError(f"Message exceeds maximum allowed length of {max_length} characters.")


def extract_booking_identifier(text: str) -> str | None:
    code_match = BOOKING_CODE_RE.search(text)
    if code_match:
        return code_match.group(1).upper()
    uuid_match = UUID_RE.search(text)
    if uuid_match:
        return uuid_match.group(1).lower()
    return None
