import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from uuid import uuid4

from geoosint.serialization import now

request_trace: ContextVar[str | None] = ContextVar("geoosint_request_trace", default=None)


@asynccontextmanager
async def network_attempt(observer, endpoint, attempt):
    started = time.perf_counter()
    event = {
        "kind": "PROVIDER_ATTEMPT",
        "request_id": request_trace.get(),
        "attempt_id": str(uuid4()),
        "attempt_number": attempt + 1,
        "endpoint": endpoint,
        "started_at": now(),
        "stage": "DNS",
        "outcome": "OK",
        "http_status": None,
        "response_bytes": 0,
    }
    try:
        yield event
    except BaseException as exc:
        event["outcome"] = "CANCELLED" if isinstance(exc, asyncio.CancelledError) else "ERROR"
        event["error_type"] = type(exc).__name__
        event["error_code"] = getattr(exc, "code", None)
        raise
    finally:
        event["finished_at"] = now()
        event["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if observer is not None:
            await asyncio.shield(asyncio.to_thread(observer, event))
