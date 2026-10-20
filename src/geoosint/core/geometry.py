import math
from typing import Any

from geographiclib.geodesic import Geodesic

from .models import (
    AngleInput,
    BoundingInput,
    DirectInput,
    GeometryInput,
    IntersectionInput,
    InverseInput,
    Point,
)

ALGORITHM = "geographiclib-wgs84-v1"
WGS84 = Geodesic.WGS84


def normalize(angle: float) -> float:
    return angle % 360


def angle_distance(first: float, second: float) -> float:
    return abs((first - second + 180) % 360 - 180)


def inverse(start: Point, end: Point) -> dict[str, Any]:
    result = WGS84.Inverse(start.latitude, start.longitude, end.latitude, end.longitude)
    return {
        "distance_m": result["s12"],
        "initial_azimuth_deg": normalize(result["azi1"]) if result["s12"] > 1e-8 else None,
        "reverse_azimuth_deg": normalize(result["azi2"] + 180) if result["s12"] > 1e-8 else None,
        "status": "COINCIDENT" if result["s12"] < 1e-8 else "OK",
    }


def direct(start: Point, azimuth: float, distance: float) -> Point:
    result = WGS84.Direct(start.latitude, start.longitude, azimuth, distance)
    return Point(latitude=result["lat2"], longitude=result["lon2"])


def intersection(args: IntersectionInput) -> dict[str, Any]:
    baseline = inverse(args.first, args.second)
    if baseline["distance_m"] < 0.01 or baseline["distance_m"] > args.max_distance_m:
        return {"status": "DEGENERATE", "point": None}
    # Локальная касательная плоскость даёт начальное приближение, затем невязка
    # уточняется по эллипсоидальным азимутам; область ограничена 100 км.
    az = math.radians(baseline["initial_azimuth_deg"])
    bx, by = baseline["distance_m"] * math.sin(az), baseline["distance_m"] * math.cos(az)
    a, b = math.radians(args.first_azimuth_deg), math.radians(args.second_azimuth_deg)
    determinant = math.sin(a - b)
    if abs(determinant) < math.sin(math.radians(1)):
        return {"status": "ILL_CONDITIONED", "point": None}
    t = (bx * math.cos(b) - by * math.sin(b)) / determinant
    u = (bx * math.cos(a) - by * math.sin(a)) / determinant
    if t < 0 or u < 0 or max(t, u) > args.max_distance_m:
        return {"status": "OUTSIDE_FORWARD_RAYS", "point": None}
    for _ in range(20):
        point = direct(args.first, args.first_azimuth_deg, t)
        bearing = inverse(args.second, point)["initial_azimuth_deg"]
        if bearing is None:
            return {"status": "DEGENERATE", "point": None}
        residual = (bearing - args.second_azimuth_deg + 180) % 360 - 180
        if abs(residual) < 1e-8:
            break
        ahead = direct(args.first, args.first_azimuth_deg, t + 1)
        next_bearing = inverse(args.second, ahead)["initial_azimuth_deg"]
        slope = (next_bearing - bearing + 180) % 360 - 180
        if abs(slope) < 1e-10:
            return {"status": "ILL_CONDITIONED", "point": None}
        t -= residual / slope
        if not 0 <= t <= args.max_distance_m:
            return {"status": "NO_STABLE_SOLUTION", "point": None}
    else:
        return {"status": "NO_STABLE_SOLUTION", "point": None}
    return {
        "status": "OK",
        "point": point.model_dump(),
        "radius_m": None,
        "limitations": ["Направления считаются точными; ошибка измерения не оценена"],
    }


def geometry(args: GeometryInput) -> dict[str, Any]:
    result: dict[str, Any]
    if isinstance(args, InverseInput):
        result = inverse(args.start, args.end)
    elif isinstance(args, DirectInput):
        result = {"point": direct(args.start, args.azimuth_deg, args.distance_m).model_dump()}
    elif isinstance(args, AngleInput):
        result = {
            "first_deg": normalize(args.first_deg),
            "angular_distance_deg": angle_distance(args.first_deg, args.second_deg),
        }
    elif isinstance(args, IntersectionInput):
        result = intersection(args)
    elif isinstance(args, BoundingInput):
        # Консервативная сферическая оболочка: 6 330 км меньше радиуса кривизны WGS84.
        delta = args.radius_m / 6_330_000
        lat = math.radians(args.center.latitude)
        south = max(-90.0, args.center.latitude - math.degrees(delta))
        north = min(90.0, args.center.latitude + math.degrees(delta))
        if south == -90 or north == 90:
            west, east = -180.0, 180.0
        else:
            dlon = math.degrees(math.asin(min(1.0, math.sin(delta) / math.cos(lat))))
            west = (args.center.longitude - dlon + 180) % 360 - 180
            east = (args.center.longitude + dlon + 180) % 360 - 180
        result = {
            "south": south,
            "west": west,
            "north": north,
            "east": east,
            "limitations": ["Консервативная оболочка, не точная граница окружности"],
        }
    else:
        raise ValueError("Неизвестная операция")
    return {"algorithm": ALGORITHM, "coordinate_order": "latitude/longitude", **result}
