"""Проверка заданных клиентом отношений между реально полученными OSM-точками."""

from typing import Any, Literal

from pydantic import Field, model_validator

from geoosint.core.geometry import angle_distance, inverse
from geoosint.core.models import Point, StrictModel
from geoosint.errors import GeoError


class SpatialConstraint(StrictModel):
    relation: Literal["DISTANCE", "BEARING"]
    expected: float = Field(ge=0, le=20_000_000)
    tolerance: float = Field(ge=0, le=20_000_000)

    @model_validator(mode="after")
    def angular_limits(self) -> "SpatialConstraint":
        if self.relation == "BEARING" and (self.expected >= 360 or self.tolerance > 180):
            raise ValueError("Азимут: [0,360), допуск: [0,180]")
        return self


class FeatureRef(StrictModel):
    operation_id: str = Field(min_length=1, max_length=64)
    source_id: str = Field(min_length=1, max_length=64)


def resolve_feature(document: dict[str, Any], reference: FeatureRef) -> dict[str, Any]:
    operation = next(
        (
            item
            for item in document["operations"]
            if item["id"] == reference.operation_id and item["kind"] == "osm"
        ),
        None,
    )
    if operation is None:
        raise GeoError("NOT_FOUND", "Нет OSM-операции этого дела")
    feature = next(
        (
            item
            for item in operation["result"]["features"]
            if item["source_id"] == reference.source_id
        ),
        None,
    )
    if feature is None or not feature.get("point"):
        raise GeoError("NOT_FOUND", "Нет географической точки объекта в сохранённой операции")
    Point.model_validate(feature["point"])
    return dict(feature)


def compare_features(
    first: dict[str, Any], second: dict[str, Any], constraint: SpatialConstraint
) -> dict[str, Any]:
    calculation = inverse(
        Point.model_validate(first["point"]), Point.model_validate(second["point"])
    )
    value = (
        calculation["distance_m"]
        if constraint.relation == "DISTANCE"
        else calculation["initial_azimuth_deg"]
    )
    error = (
        None
        if value is None
        else abs(value - constraint.expected)
        if constraint.relation == "DISTANCE"
        else angle_distance(value, constraint.expected)
    )
    return {
        "algorithm": "geographiclib-wgs84-source-points-v1",
        "relation": constraint.relation,
        "value": value,
        "unit": "m" if constraint.relation == "DISTANCE" else "deg",
        "expected": constraint.expected,
        "tolerance": constraint.tolerance,
        "matches": None if error is None else error <= constraint.tolerance,
        "coordinate_methods": [
            first.get("point_method", "UNSPECIFIED"),
            second.get("point_method", "UNSPECIFIED"),
        ],
        "scene_verified": False,
        "limitations": [
            "Проверены точки объектов карты; центр bbox не равен ближайшей точке объекта или положению камеры",
            "Ожидаемое отношение передано клиентом; визуальное происхождение не проверено сервером",
        ],
    }
