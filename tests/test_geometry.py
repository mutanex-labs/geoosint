import math
from datetime import datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from geoosint.core.geometry import direct, geometry, inverse
from geoosint.core.models import Bounds, GeometryInput, Point
from geoosint.core.sun import sun_position


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ((0, 0), (0, 1), 111319.490793),
        ((0, 179.9), (0, -179.9), 22263.898159),
        ((0, 0), (0, 180), 20003931.458625),
        ((90, 0), (90, 180), 0),
        ((55, 37), (55, 37), 0),
        ((-90, 0), (0, 0), 10001965.729312),
    ],
)
def test_wgs84_vectors(a, b, expected):
    result = inverse(Point(latitude=a[0], longitude=a[1]), Point(latitude=b[0], longitude=b[1]))
    assert result["distance_m"] == pytest.approx(expected, abs=0.001)
    if expected == 0:
        assert result["initial_azimuth_deg"] is None


@pytest.mark.parametrize(
    "point",
    [
        Point(latitude=89.999, longitude=179.9),
        Point(latitude=-20, longitude=-179.99),
        Point(latitude=0, longitude=0),
    ],
)
def test_roundtrip(point):
    end = direct(point, 45, 10000)
    assert inverse(point, end)["distance_m"] == pytest.approx(10000, abs=1e-6)
    assert inverse(point, end)["initial_azimuth_deg"] == pytest.approx(45, abs=1e-8)


def test_angles_and_intersections():
    calc = TypeAdapter(GeometryInput)
    assert (
        geometry(calc.validate_python({"operation": "angle", "first_deg": 359, "second_deg": 1}))[
            "angular_distance_deg"
        ]
        == 2
    )
    args = {
        "operation": "intersection",
        "first": {"latitude": 0, "longitude": 0},
        "second": {"latitude": 0, "longitude": 0.1},
        "first_azimuth_deg": 45,
        "second_azimuth_deg": 315,
    }
    result = geometry(calc.validate_python(args))
    assert result["status"] == "OK"
    assert result["point"]["longitude"] == pytest.approx(0.05, abs=1e-6)
    args["second_azimuth_deg"] = 45.000001
    assert geometry(calc.validate_python(args))["status"] == "ILL_CONDITIONED"
    args["second"] = args["first"]
    assert geometry(calc.validate_python(args))["status"] == "DEGENERATE"


def test_bounds_dateline_and_poles():
    calc = TypeAdapter(GeometryInput)
    for latitude, longitude in [(0, 179.99), (89.999, 0), (-89.999, 80), (45, 90)]:
        point = Point(latitude=latitude, longitude=longitude)
        result = geometry(
            calc.validate_python(
                {"operation": "bounds", "center": point.model_dump(), "radius_m": 10000}
            )
        )
        bounds = Bounds(**{k: result[k] for k in ("south", "west", "north", "east")})
        for bearing in range(0, 360, 5):
            assert bounds.contains(direct(point, bearing, 10000))


@pytest.mark.parametrize("latitude,longitude", [(91, 0), (0, 181), (math.nan, 0), (0, math.inf)])
def test_invalid_coordinates(latitude, longitude):
    with pytest.raises(ValidationError):
        Point(latitude=latitude, longitude=longitude)


def test_sun_noaa_reference():
    # NOAA Solar Calculator example: Boulder, 2003-10-17 12:30:30 MST.
    result = sun_position(
        Point(latitude=39.742476, longitude=-105.1786),
        datetime.fromisoformat("2003-10-17T12:30:30-07:00"),
    )
    assert result["azimuth_deg"] == pytest.approx(194.34, abs=0.3)
    assert result["elevation_deg"] == pytest.approx(39.87, abs=0.3)
    same = sun_position(
        Point(latitude=39.742476, longitude=-105.1786),
        datetime.fromisoformat("2003-10-17T19:30:30+00:00"),
    )
    assert same == result
    assert sun_position(Point(latitude=0, longitude=0), None)["status"] == "TIME_REQUIRED"
    with pytest.raises(ValueError):
        sun_position(Point(latitude=0, longitude=0), datetime(2020, 1, 1))
