import json

import pytest

from geoosint.cases.service import EvidenceInput, Service
from geoosint.core.models import Bounds, CandidateInput, InverseInput, Point
from geoosint.core.scope import (
    BoundaryRegistry,
    ScopeArea,
    ScopePolygon,
    SearchScope,
    bounds_allowed,
    membership,
    scope_fields,
)
from geoosint.errors import GeoError
from geoosint.serialization import digest
from geoosint.storage.database import Database


def polygon(west=0, south=0, east=2, north=2):
    return ScopePolygon(
        coordinates=[[(west, south), (east, south), (east, north), (west, north), (west, south)]]
    )


def scope(mode="PREFERRED", **kwargs):
    return SearchScope(
        mode=mode,
        areas=[ScopeArea(kind="POLYGON", geometry=polygon(), name="Synthetic Moscow")],
        **kwargs,
    )


def item(label, longitude, score=0):
    return CandidateInput(
        label=label,
        level="POINT",
        point=Point(latitude=1, longitude=longitude),
        area=Bounds(south=0.9, west=longitude - 0.1, north=1.1, east=longitude + 0.1),
    )


def test_preferred_differs_from_auto_and_low_score_retained(runtime):
    svc = runtime.service
    preferred = svc.create("a", "Preferred", "p", search_scope=scope())["id"]
    auto = svc.create("a", "Auto", "a")["id"]
    for cid in (preferred, auto):
        candidates = svc.candidates("a", cid, 1, cid, [item("inside", 1), item("outside", 4)])[
            "result"
        ]
        svc.rank("a", cid, 2, cid + "rank", candidates[0]["id"], -99, "initial visual score")
    pplan = svc.next("a", preferred)
    aplan = svc.next("a", auto)
    assert svc.read("a", preferred)["candidates"][0]["id"] == pplan["candidate_ids"][0]
    assert svc.read("a", auto)["candidates"][1]["id"] == aplan["candidate_ids"][0]
    assert len(pplan["candidate_ids"]) == 2


@pytest.mark.parametrize("provider_unavailable", [False, True])
def test_preferred_checked_expansion_preserves_candidates(runtime, provider_unavailable):
    svc = runtime.service
    cid = svc.create("a", "Preferred", "c", search_scope=scope())["id"]
    candidates = svc.candidates("a", cid, 1, "cs", [item("inside", 1), item("outside", 4)])[
        "result"
    ]
    operation = svc.compute(
        "a",
        cid,
        2,
        "calc",
        InverseInput(
            operation="inverse",
            start=Point(latitude=0, longitude=0),
            end=Point(latitude=1, longitude=1),
        ),
    )["result"]
    svc.evidence(
        "a",
        cid,
        3,
        "ev",
        EvidenceInput(
            candidate_id=candidates[0]["id"],
            operation_id=operation["id"],
            description="A local calculation; applicability unverified",
        ),
    )
    if provider_unavailable:
        svc.provider_failure(
            "a",
            cid,
            "UPSTREAM_UNAVAILABLE",
            {"bounds": {"south": 0, "west": 0, "north": 0.01, "east": 0.01}, "category": "roads"},
        )
    plan = svc.next("a", cid)
    doc = svc.read("a", cid)
    if provider_unavailable:
        assert doc["expansion_level"] == 0
        assert len(doc["candidates"]) == 2
        assert plan["coverage_status"] == "PROVIDER_UNAVAILABLE"
        return
    assert doc["expansion_level"] == 1
    assert doc["expansion_history"][0]["reason"] == "NO_VERIFIED_MATCH"
    assert len(doc["candidates"]) == 2
    assert doc["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert plan["expansion_reasons"]
    assert (
        runtime.db.connection.execute(
            "SELECT count(*) FROM scope_events WHERE case_id=?", (cid,)
        ).fetchone()[0]
        == 1
    )


def test_strict_never_expands_or_queries_outside(runtime):
    svc = runtime.service
    cid = svc.create("a", "Strict", "c", search_scope=scope("STRICT"))["id"]
    with pytest.raises(GeoError) as error:
        svc.candidates("a", cid, 1, "outside", [item("outside", 4)])
    assert error.value.code == "SCOPE_VIOLATION"
    with pytest.raises(GeoError):
        svc.expand("a", cid, 1, "expand", "USER_AUTHORIZED", [], True)
    with pytest.raises(GeoError):
        svc.preflight(
            "a",
            cid,
            1,
            "query",
            "osm",
            {"bounds": Bounds(south=1, west=1, north=3, east=3).model_dump()},
        )
    assert svc.read("a", cid)["revision"] == 1
    assert svc.budget_status("a", cid)["budget_used"]["provider_requests"] == 0


def test_exclusions_and_bbox_not_membership(runtime):
    svc = runtime.service
    search = scope(
        "STRICT", excluded_areas=[ScopeArea(kind="POLYGON", geometry=polygon(0.5, 0.5, 1.5, 1.5))]
    )
    cid = svc.create("a", "Region excluding city", "c", search_scope=search)["id"]
    with pytest.raises(GeoError):
        svc.candidates("a", cid, 1, "bad", [item("excluded", 1)])
    # Треугольник: bbox включает точку вне реального полигона.
    triangle = ScopePolygon(coordinates=[[(0, 0), (2, 0), (0, 2), (0, 0)]])
    fields = scope_fields(
        SearchScope(mode="STRICT", areas=[ScopeArea(kind="POLYGON", geometry=triangle)])
    )
    assert membership(fields, Point(latitude=1.8, longitude=1.8)) == "OUTSIDE"
    assert not bounds_allowed(fields, Bounds(south=1.7, west=1.7, north=1.9, east=1.9))


def test_ambiguous_names_and_hierarchy(tmp_path):
    entries = []
    for i in (1, 2):
        geometry = polygon().model_dump(mode="json")
        entries.append(
            {
                "object_id": f"fixture:city:{i}",
                "kind": "CITY",
                "name": "Test City",
                "parent_ids": [f"fixture:region:{i}"],
                "source": "TEST_FIXTURE_NOT_REAL_BOUNDARIES",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "license": "CC0",
                "geometry": geometry,
                "content_hash": digest(geometry),
            }
        )
    path = tmp_path / "boundaries.json"
    path.write_text(json.dumps({"areas": entries}), encoding="utf-8")
    registry = BoundaryRegistry(path)
    with pytest.raises(GeoError) as error:
        registry.resolve(ScopeArea(kind="CITY", name="Test City"))
    assert error.value.code == "SCOPE_AMBIGUOUS"
    resolved = registry.resolve(
        ScopeArea(kind="CITY", name="Test City", parent_id="fixture:region:2")
    )
    assert resolved["object_id"] == "fixture:city:2"
    assert (
        registry.resolve(ScopeArea(kind="REGION", name="Test City"))["boundary_status"]
        == "BOUNDARY_UNAVAILABLE"
    )


def test_unavailable_boundary_blocks_strict(runtime):
    svc = runtime.service
    cid = svc.create(
        "a",
        "Unknown boundary",
        "c",
        search_scope=SearchScope(mode="STRICT", areas=[ScopeArea(kind="CITY", name="Москва")]),
    )["id"]
    assert svc.next("a", cid)["action"] == "RESOLVE_BOUNDARY"
    with pytest.raises(GeoError):
        svc.candidates("a", cid, 1, "candidate", [item("unknown", 1)])


def test_budget_exhaustion_no_data_loss_and_restart(runtime):
    svc = runtime.service
    cid = svc.create("a", "Budget", "c", search_scope=SearchScope(budget={"max_tool_calls": 2}))[
        "id"
    ]
    svc.candidates("a", cid, 1, "cs", [item("candidate", 1)])
    with pytest.raises(GeoError) as error:
        svc.rank("a", cid, 2, "rank", svc.read("a", cid)["candidates"][0]["id"], -99, "test")
    assert error.value.code == "BUDGET_EXHAUSTED"
    assert svc.next("a", cid)["action"] == "BUDGET_EXHAUSTED"
    assert len(svc.read("a", cid)["candidates"]) == 1
    reopened = Database(runtime.db.path)
    restored = Service(reopened, runtime.settings)
    assert restored.budget_status("a", cid)["budget_used"]["tool_calls"] == 3
    assert restored.read("a", cid)["scope_mode"] == "AUTO"
    reopened.close()
    # Экспорт/удаление/честное завершение доступны после исчерпания поискового бюджета.
    svc.finalize("a", cid, 2, "final")
    assert svc.export("a", cid)["case"]["result"]["scope"]["prior_is_evidence"] is False


def test_scope_change_owner_revision_and_preservation(runtime):
    svc = runtime.service
    cid = svc.create("a", "Scope", "c")["id"]
    svc.candidates("a", cid, 1, "cs", [item("saved", 1)])
    with pytest.raises(GeoError):
        svc.scope_update("b", cid, 2, "foreign", scope("STRICT"))
    with pytest.raises(GeoError):
        svc.scope_update("a", cid, 1, "stale", scope("STRICT"))
    result = svc.scope_update("a", cid, 2, "good", scope("STRICT"))
    assert result["result"]["scope_revision"] == 2
    assert svc.read("a", cid)["candidates"][0]["status"] == "NEEDS_REVIEW"
    assert runtime.db.integrity("a")["status"] == "OK"


def test_antimeridian_polygon_and_circle():
    fields = scope_fields(
        SearchScope(
            mode="STRICT", areas=[ScopeArea(kind="POLYGON", geometry=polygon(179, -2, -179, 2))]
        )
    )
    assert membership(fields, Point(latitude=0, longitude=179.5)) == "INSIDE"
    assert membership(fields, Point(latitude=0, longitude=-179.5)) == "INSIDE"
    assert membership(fields, Point(latitude=0, longitude=0)) == "OUTSIDE"
    assert bounds_allowed(fields, Bounds(south=-1, west=179.5, north=1, east=-179.5))
    circle = scope_fields(
        SearchScope(
            mode="STRICT",
            areas=[
                ScopeArea(kind="CIRCLE", center=Point(latitude=0, longitude=179.9), radius_m=20000)
            ],
        )
    )
    assert membership(circle, Point(latitude=0, longitude=-179.99)) == "INSIDE"
    assert membership(circle, Point(latitude=0, longitude=178)) == "OUTSIDE"


def test_multiple_polygons_and_holes():
    from geoosint.core.scope import ScopeMultiPolygon

    geometry = ScopeMultiPolygon(
        coordinates=[polygon().coordinates, polygon(10, 10, 12, 12).coordinates]
    )
    fields = scope_fields(
        SearchScope(mode="STRICT", areas=[ScopeArea(kind="POLYGON", geometry=geometry)])
    )
    assert membership(fields, Point(latitude=11, longitude=11)) == "INSIDE"
    assert membership(fields, Point(latitude=5, longitude=5)) == "OUTSIDE"
    hole = ScopePolygon(coordinates=polygon().coordinates + polygon(0.5, 0.5, 1.5, 1.5).coordinates)
    fields = scope_fields(
        SearchScope(mode="STRICT", areas=[ScopeArea(kind="POLYGON", geometry=hole)])
    )
    assert membership(fields, Point(latitude=1, longitude=1)) == "OUTSIDE"


def test_create_idempotency_before_scope_contract(runtime):
    svc = runtime.service
    created = svc.create("a", "Legacy create", "legacy")
    # До Search Scope create fingerprint содержал ровно три поля.
    runtime.db.connection.execute(
        "UPDATE idempotency SET request_hash=? WHERE owner=? AND key=?",
        (digest(["create", "Legacy create", False]), "a", "legacy"),
    )
    assert svc.create("a", "Legacy create", "legacy") == created
