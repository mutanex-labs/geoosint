from datetime import UTC, datetime
from typing import Any

from astral import Observer
from astral.sun import azimuth, elevation

from .models import Point


def sun_position(point: Point, captured_at: datetime | None) -> dict[str, Any]:
    if captured_at is None:
        return {
            "status": "TIME_REQUIRED",
            "position": None,
            "limitations": ["Время обработки не заменяет время съёмки"],
        }
    if captured_at.tzinfo is None or captured_at.utcoffset() is None:
        raise ValueError("Требуется ISO 8601 с явным UTC offset")
    utc = captured_at.astimezone(UTC)
    observer = Observer(point.latitude, point.longitude, 0)
    bearing = azimuth(observer, utc)
    height = elevation(observer, utc, with_refraction=False)
    return {
        "status": "OK",
        "captured_at_utc": utc.isoformat(),
        "algorithm": "astral-3.2-noaa",
        "azimuth_deg": bearing,
        "elevation_deg": height,
        "shadow_azimuth_deg": (bearing + 180) % 360 if height > 0 else None,
        "limitations": [
            "Геометрическое солнце без рефракции и рельефа",
            "Ориентир точности 1°; не метрологическая гарантия",
        ],
    }
