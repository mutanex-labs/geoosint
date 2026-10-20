from pathlib import Path

import pytest
from pydantic import ValidationError

from geoosint.core.models import Bounds, Point
from geoosint.core.scope import (
    BoundaryRegistry,
    ScopeArea,
    SearchScope,
    bounds_allowed,
    membership,
    parse_geometry,
    scope_fields,
    unwrap,
)

REGISTRY = Path(__file__).parents[1] / "data-sources/osm-boundaries/registry.json"


@pytest.fixture(scope="module")
def registry():
    return BoundaryRegistry(REGISTRY)


@pytest.mark.parametrize("mode", ["STRICT", "PREFERRED"])
def test_real_moscow_not_oblast(registry, mode):
    document = scope_fields(
        SearchScope(mode=mode, areas=[ScopeArea(kind="CITY", name="Москва")]), registry
    )
    assert membership(document, Point(latitude=55.752, longitude=37.628)) == "INSIDE"
    assert membership(document, Point(latitude=55.91, longitude=37.74)) == "OUTSIDE"
    assert bounds_allowed(document, Bounds(south=55.748, west=37.619, north=55.753, east=37.633))
    assert not bounds_allowed(document, Bounds(south=55.90, west=37.73, north=55.92, east=37.75))


def test_real_oblast_excludes_moscow_and_russia_provenance(registry):
    document = scope_fields(
        SearchScope(
            mode="STRICT",
            areas=[ScopeArea(kind="REGION", name="Московская область")],
            excluded_areas=[ScopeArea(kind="CITY", name="Москва")],
        ),
        registry,
    )
    assert membership(document, Point(latitude=55.91, longitude=37.74)) == "INSIDE"
    assert membership(document, Point(latitude=55.752, longitude=37.628)) == "OUTSIDE"
    country = registry.resolve(ScopeArea(kind="COUNTRY", name="Россия"))
    assert country["object_id"] == "osm:relation/60189"
    assert country["license"] == "ODbL-1.0"
    assert country["geometry_processing"] == []
    assert country["retrieved_at"] and country["source"] and country["sha256"]
    with pytest.raises(ValidationError):
        parse_geometry(country["geometry"])


def test_unwrap_preserves_exact_non_dateline_vertices():
    ring = [(52.6974782, 45.8938648), (52.7, 46), (52.75, 46), (52.6974782, 45.8938648)]
    assert unwrap(ring) == ring
