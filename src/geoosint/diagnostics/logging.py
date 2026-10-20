import json
import logging
from datetime import UTC, datetime


class PrivateLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        if record.name == "geoosint":
            return record.getMessage()
        # Библиотечные сообщения могут содержать URL с кодами OAuth и входные значения.
        # Сохраняем тип события и severity; исходные args/traceback не выводим.
        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "severity": record.levelname,
                "component": record.name,
                "operation": "library_event",
                "request_id": None,
                "duration_ms": None,
                "result": "ERROR" if record.levelno >= 40 else "INFO",
                "error_code": "LIBRARY_ERROR" if record.levelno >= 40 else None,
            }
        )


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(PrivateLogFormatter())
    for name in ("geoosint", "mcp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
        logger.setLevel(logging.INFO if name == "geoosint" else logging.WARNING)
