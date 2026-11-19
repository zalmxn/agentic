import json
import logging
from datetime import datetime, timezone


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "ai_booking_service",
            "logger": record.name,
            "message": record.getMessage(),
        }
        for attr in ("request_id", "method", "path", "status_code", "duration_ms", "conversation_id"):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr] = val
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level_name: str = "INFO") -> None:
    root = logging.getLogger("ai_booking_service")
    root.setLevel(getattr(logging, level_name.upper(), logging.INFO))
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonLogFormatter())
        root.addHandler(handler)
    root.propagate = False
