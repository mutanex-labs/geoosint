from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Point(StrictModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class Bounds(StrictModel):
    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)

    @model_validator(mode="after")
    def ordered(self) -> "Bounds":
        if self.south >= self.north or self.west == self.east:
            raise ValueError("Нужна непустая область; west > east означает переход даты")
        return self

    def contains(self, point: Point) -> bool:
        lon = point.longitude
        inside = (
            self.west <= lon <= self.east
            if self.west < self.east
            else (lon >= self.west or lon <= self.east)
        )
        return self.south <= point.latitude <= self.north and inside


class InverseInput(StrictModel):
    operation: Literal["inverse"]
    start: Point
    end: Point


class DirectInput(StrictModel):
    operation: Literal["direct"]
    start: Point
    azimuth_deg: float
    distance_m: float = Field(ge=0, le=20_000_000)


class IntersectionInput(StrictModel):
    operation: Literal["intersection"]
    first: Point
    first_azimuth_deg: float
    second: Point
    second_azimuth_deg: float
    max_distance_m: float = Field(default=100_000, gt=0, le=100_000)


class BoundingInput(StrictModel):
    operation: Literal["bounds"]
    center: Point
    radius_m: float = Field(gt=0, le=1_000_000)


class AngleInput(StrictModel):
    operation: Literal["angle"]
    first_deg: float
    second_deg: float


GeometryInput = Annotated[
    InverseInput | DirectInput | IntersectionInput | BoundingInput | AngleInput,
    Field(discriminator="operation"),
]


class CandidateInput(StrictModel):
    label: str = Field(min_length=1, max_length=300)
    level: Literal["COUNTRY", "REGION", "CITY", "AREA", "POINT"]
    area: Bounds
    point: Point | None = None
    parent_id: str | None = Field(default=None, max_length=64)
    source_operation_id: str | None = Field(default=None, max_length=64)
    source_feature_id: str | None = Field(default=None, max_length=64)
    observation_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def point_inside(self) -> "CandidateInput":
        if bool(self.source_operation_id) != bool(self.source_feature_id):
            raise ValueError("Источник кандидата требует operation_id и feature_id")
        if self.point and not self.area.contains(self.point):
            raise ValueError("Представительная точка находится вне области")
        if self.level == "POINT" and self.point is None:
            raise ValueError("POINT требует координаты")
        return self
