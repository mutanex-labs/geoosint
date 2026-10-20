import json
import math
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationInfo, model_validator
from shapely import affinity
from shapely.geometry import Point as ShapePoint
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from geoosint.core.geometry import inverse
from geoosint.core.models import Bounds, Point, StrictModel
from geoosint.errors import GeoError
from geoosint.serialization import digest, now


class SearchBudget(StrictModel):
    max_tool_calls: int = Field(default=200, ge=1, le=10000)
    max_provider_requests: int = Field(default=30, ge=0, le=1000)
    max_external_requests: int = Field(default=60, ge=0, le=2000)
    max_duration_seconds: float = Field(default=3600, gt=0, le=86400 * 7)
    max_candidates: int = Field(default=500, ge=1, le=10000)
    max_expansion_levels: int = Field(default=3, ge=0, le=10)
    preferred_scope_budget_fraction: float = Field(default=0.6, gt=0, lt=1)


class ScopePolygon(StrictModel):
    type: Literal["Polygon"] = "Polygon"
    coordinates: list[list[tuple[float, float]]] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def valid(self, info: ValidationInfo) -> "ScopePolygon":
        maximum = 200_000 if info.context and info.context.get("registry") else 2000
        if sum(map(len, self.coordinates)) > maximum:
            raise ValueError("Полигон: максимум 2000 вершин")
        for ring in self.coordinates:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Кольцо должно быть замкнуто и иметь минимум 4 вершины")
            if any(
                not math.isfinite(x)
                or not math.isfinite(y)
                or not -180 <= x <= 180
                or not -89 <= y <= 89
                for x, y in ring
            ):
                raise ValueError("Недопустимые координаты; полярные шапки не поддержаны")
        shape = polygon_shape(self)
        if (
            not shape.is_valid
            or shape.is_empty
            or shape.area == 0
            or shape.bounds[2] - shape.bounds[0] >= 360
        ):
            raise ValueError("Невалидная геометрия полигона")
        return self


def unwrap(
    ring: list[tuple[float, float]], center: float | None = None
) -> list[tuple[float, float]]:
    result = [ring[0]]
    for x, y in ring[1:]:
        previous = result[-1][0]
        # Сохраняем исходные дробные координаты: modulo в каждом шаге создавал
        # микросамопересечения в реальных кольцах с повторяющимися вершинами.
        result.append((x + 360 * round((previous - x) / 360), y))
    if center is not None:
        shift = 360 * round((center - result[0][0]) / 360)
        result = [(x + shift, y) for x, y in result]
    return result


class ScopeMultiPolygon(StrictModel):
    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: list[list[list[tuple[float, float]]]] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def valid(self, info: ValidationInfo) -> "ScopeMultiPolygon":
        maximum = 200_000 if info.context and info.context.get("registry") else 2000
        if sum(len(ring) for polygon in self.coordinates for ring in polygon) > maximum:
            raise ValueError("MultiPolygon: максимум 2000 вершин")
        for coordinates in self.coordinates:
            ScopePolygon.model_validate({"coordinates": coordinates}, context=info.context)
        return self


def parse_geometry(
    value: dict[str, Any], *, registry: bool = False
) -> ScopePolygon | ScopeMultiPolygon:
    if value.get("type") == "MultiPolygon":
        return ScopeMultiPolygon.model_validate(value, context={"registry": registry})
    return ScopePolygon.model_validate(value, context={"registry": registry})


def polygon_shape(polygon: ScopePolygon | ScopeMultiPolygon) -> BaseGeometry:
    if isinstance(polygon, ScopeMultiPolygon):
        return unary_union(
            [
                polygon_shape(ScopePolygon.model_construct(coordinates=coordinates))
                for coordinates in polygon.coordinates
            ]
        )
    shell = unwrap(polygon.coordinates[0])
    return Polygon(shell, [unwrap(ring, shell[0][0]) for ring in polygon.coordinates[1:]])


class ScopeArea(StrictModel):
    kind: Literal["COUNTRY", "REGION", "CITY", "DISTRICT", "POLYGON", "CIRCLE"]
    name: str | None = Field(default=None, max_length=200)
    object_id: str | None = Field(default=None, max_length=128)
    parent_id: str | None = Field(default=None, max_length=128)
    geometry: ScopePolygon | ScopeMultiPolygon | None = None
    center: Point | None = None
    radius_m: float | None = Field(default=None, gt=0, le=1_000_000)

    @model_validator(mode="after")
    def valid(self) -> "ScopeArea":
        if self.kind == "POLYGON" and self.geometry is None:
            raise ValueError("POLYGON требует геометрию")
        if self.kind == "CIRCLE" and (self.center is None or self.radius_m is None):
            raise ValueError("CIRCLE требует center и radius_m")
        if self.kind not in ("POLYGON", "CIRCLE") and (not self.name and not self.object_id):
            raise ValueError("Административная область требует name или object_id")
        if self.kind not in ("POLYGON", "CIRCLE") and self.geometry is not None:
            raise ValueError("Границы административного объекта берутся только из реестра")
        return self


class SearchScope(StrictModel):
    mode: Literal["AUTO", "PREFERRED", "STRICT"] = "AUTO"
    areas: list[ScopeArea] = Field(default_factory=list, max_length=10)
    excluded_areas: list[ScopeArea] = Field(default_factory=list, max_length=10)
    budget: SearchBudget = Field(default_factory=SearchBudget)

    @model_validator(mode="after")
    def valid(self) -> "SearchScope":
        if self.mode != "AUTO" and not self.areas:
            raise ValueError("PREFERRED/STRICT требует хотя бы одну область")
        if self.mode == "AUTO" and self.areas:
            raise ValueError("AUTO не содержит пользовательских включённых территорий")
        return self


class BoundaryRegistry:
    def __init__(self, path: Path | None = None) -> None:
        self.entries: list[dict[str, Any]] = []
        if path:
            if path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("Реестр границ превышает 8 MiB")
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data["areas"]:
                geometry = parse_geometry(entry["geometry"], registry=True)
                if digest(geometry.model_dump(mode="json")) != entry["content_hash"]:
                    raise ValueError("Контрольная сумма границы не совпадает")
                if not all(
                    entry.get(k)
                    for k in ("object_id", "kind", "name", "source", "retrieved_at", "license")
                ):
                    raise ValueError("Нет происхождения административной границы")
                self.entries.append(entry)

    def resolve(self, area: ScopeArea) -> dict[str, Any]:
        if area.kind in ("POLYGON", "CIRCLE"):
            return {
                **area.model_dump(mode="json"),
                "boundary_status": "USER_DEFINED",
                "source": "USER_PROVIDED_GEOMETRY",
                "content_hash": digest(area.model_dump(mode="json")),
            }
        matches = [
            e
            for e in self.entries
            if e["kind"] == area.kind
            and (
                (area.object_id and e["object_id"] == area.object_id)
                or (
                    not area.object_id
                    and area.name
                    and e["name"].casefold() == area.name.casefold()
                )
            )
            and (not area.parent_id or area.parent_id in e.get("parent_ids", []))
        ]
        if len(matches) > 1:
            raise GeoError(
                "SCOPE_AMBIGUOUS",
                "Уточните parent_id или object_id; варианты: "
                + "; ".join(e["object_id"] for e in matches[:10]),
            )
        if not matches:
            return {
                **area.model_dump(mode="json"),
                "boundary_status": "BOUNDARY_UNAVAILABLE",
                "source": "USER_PROVIDED_PRIOR",
                "geometry": None,
            }
        return {**matches[0], "boundary_status": "REGISTRY_BOUNDARY"}


def scope_fields(
    scope: SearchScope | None = None, registry: BoundaryRegistry | None = None
) -> dict[str, Any]:
    scope = scope or SearchScope()
    registry = registry or BoundaryRegistry()
    return {
        "search_scope": scope.model_dump(mode="json"),
        "initial_search_scope": scope.model_dump(mode="json"),
        "scope_mode": scope.mode,
        "scope_source": "AUTO" if scope.mode == "AUTO" else "USER_PROVIDED_PRIOR",
        "scope_geometry": [registry.resolve(a) for a in scope.areas],
        "excluded_areas": [registry.resolve(a) for a in scope.excluded_areas],
        "scope_revision": 1,
        "scope_checked": [],
        "expansion_history": [],
        "expansion_level": 0,
        "coverage_status": "INSUFFICIENT_EVIDENCE",
        "budget": scope.budget.model_dump(),
        "search_started_at": now(),
    }


def point_in_area(point: Point, area: dict[str, Any]) -> bool | None:
    if area["kind"] == "CIRCLE":
        return bool(
            inverse(Point.model_validate(area["center"]), point)["distance_m"] <= area["radius_m"]
        )
    if not area.get("geometry"):
        return None
    shape = polygon_shape(parse_geometry(area["geometry"], registry=True))
    return any(
        shape.covers(ShapePoint(point.longitude + shift, point.latitude))
        for shift in (-360, 0, 360)
    )


def membership(doc: dict[str, Any], point: Point) -> Literal["INSIDE", "OUTSIDE", "UNKNOWN"]:
    excluded = [point_in_area(point, a) for a in doc["excluded_areas"]]
    if True in excluded:
        return "OUTSIDE"
    if None in excluded:
        return "UNKNOWN"
    included = [point_in_area(point, a) for a in doc["scope_geometry"]]
    if not included and doc["scope_mode"] == "AUTO":
        return "INSIDE"
    if True in included:
        return "INSIDE"
    return "UNKNOWN" if None in included else "OUTSIDE"


def bounds_allowed(doc: dict[str, Any], bounds: Bounds) -> bool:
    # Разрешаем только целиком покрытый запрос: bbox сам по себе не доказывает членство.
    if any(not a.get("geometry") for a in doc["scope_geometry"] + doc["excluded_areas"]):
        # Для кругов строгий запрос проверяется консервативным шаром вокруг bbox.
        if (
            len(doc["scope_geometry"]) != 1
            or doc["scope_geometry"][0]["kind"] != "CIRCLE"
            or doc["excluded_areas"]
        ):
            return False
        area = doc["scope_geometry"][0]
        width = (bounds.east - bounds.west) % 360
        center = Point(
            latitude=(bounds.south + bounds.north) / 2,
            longitude=(bounds.west + width / 2 + 180) % 360 - 180,
        )
        conservative_radius = 6_400_000 * math.radians((bounds.north - bounds.south + width) / 2)
        return bool(
            inverse(Point.model_validate(area["center"]), center)["distance_m"]
            + conservative_radius
            <= area["radius_m"]
        )
    west = bounds.west
    east = west + (bounds.east - west) % 360
    box = Polygon(
        [(west, bounds.south), (east, bounds.south), (east, bounds.north), (west, bounds.north)]
    )

    def copies(areas: list[dict[str, Any]]) -> BaseGeometry:
        return unary_union(
            [
                affinity.translate(
                    polygon_shape(parse_geometry(a["geometry"], registry=True)), xoff=s
                )
                for a in areas
                for s in (-360, 0, 360)
            ]
        )

    includes = copies(doc["scope_geometry"]) if doc["scope_geometry"] else box
    excludes = copies(doc["excluded_areas"])
    return includes.difference(excludes).covers(box)
