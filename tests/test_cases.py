from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from geoosint.cases.service import EvidenceInput, ObservationInput
from geoosint.core.models import Bounds, CandidateInput, InverseInput, Point
from geoosint.errors import GeoError
from geoosint.storage.database import Database


def candidate(label="место"):
    return CandidateInput(label=label, level="AREA", area=Bounds(south=0, west=0, north=1, east=1))


def test_idempotency_conflict_owner_and_restart(runtime):
    service = runtime.service
    case = service.create("alice", "Тест", "create")
    assert case == service.create("alice", "Тест", "create")
    with pytest.raises(GeoError, match="другого запроса"):
        service.create("alice", "Другой", "create")
    with pytest.raises(GeoError) as error:
        runtime.db.read("bob", case["id"])
    assert error.value.code == "NOT_FOUND"
    with pytest.raises(GeoError):
        service.candidates("alice", case["id"], 2, "wrong", [candidate()])
    assert runtime.db.read("alice", case["id"])["revision"] == 1
    other = Database(runtime.db.path)
    assert other.read("alice", case["id"]) == case
    other.close()


def test_forged_evidence_and_model_verification(runtime):
    svc = runtime.service
    case = svc.create("a", "Тест", "c")
    cid = case["id"]
    obs = svc.observation(
        "a",
        cid,
        1,
        "o",
        ObservationInput(
            category="TEXT", text="Вывеска", source="модель", dependency_group="sign-1"
        ),
    )
    assert obs["result"]["verification"] == "UNVERIFIED"
    item = svc.candidates("a", cid, 2, "cs", [candidate()])["result"][0]
    with pytest.raises(GeoError) as error:
        svc.evidence(
            "a",
            cid,
            3,
            "bad",
            EvidenceInput(candidate_id=item["id"], operation_id="fake", description="PASS"),
        )
    assert error.value.code == "EVIDENCE_INSUFFICIENT"
    calc = InverseInput(
        operation="inverse",
        start=Point(latitude=0, longitude=0),
        end=Point(latitude=0, longitude=1),
    )
    operation = svc.compute("a", cid, 3, "calc", calc)["result"]
    evidence = EvidenceInput(
        candidate_id=item["id"],
        operation_id=operation["id"],
        description="Направление",
        observation_ids=[obs["result"]["id"]],
    )
    first = svc.evidence("a", cid, 4, "e1", evidence)
    assert first == svc.evidence("a", cid, 4, "e1", evidence)
    svc.evidence("a", cid, 5, "e2", evidence)
    doc = runtime.db.read("a", cid)
    assert doc["evidence"][0]["dependency_groups"] == doc["evidence"][1]["dependency_groups"]
    assert doc["evidence"][0]["applicability"] == "UNVERIFIED"
    with pytest.raises(GeoError) as error:
        svc.finalize("a", cid, 6, "bad-final", "POINT")
    assert error.value.code == "EVIDENCE_INSUFFICIENT"
    assert runtime.db.read("a", cid)["revision"] == 6
    svc.finalize("a", cid, 6, "final")
    assert svc.export("a", cid)["case"]["result"]["radius_m"] is None


def test_regression_low_visual_and_railway_assumption(runtime):
    svc = runtime.service
    cid = svc.create("a", "Рельсы: узкоколейка — только догадка", "c")["id"]
    result = svc.candidates("a", cid, 1, "cs", [candidate("истинный"), candidate("похожий")])
    item = result["result"][0]
    svc.rank("a", cid, 2, "rank", item["id"], -99, "Модель предположила другой тип дороги")
    assert item["id"] in svc.next("a", cid)["candidate_ids"]
    doc = runtime.db.read("a", cid)
    assert len(doc["candidates"]) == 2
    assert doc["candidates"][0]["status"] == "DOWNRANKED"
    assert doc["ranking_history"][0]["previous"] == 0
    # Источник недоступен: доменные данные остаются неизменными.
    assert runtime.osm.capabilities()["availability"] == "DISABLED"
    assert runtime.db.read("a", cid) == doc


def test_regression_cluster_representative_outside():
    with pytest.raises(ValidationError):
        CandidateInput(
            label="сдвинутый кластер",
            level="POINT",
            area=Bounds(south=0, west=0, north=1, east=1),
            point=Point(latitude=2, longitude=2),
        )


def test_concurrent_revision_no_partial_writes(runtime):
    svc = runtime.service
    cid = svc.create("a", "Тест", "c")["id"]

    def add(i):
        try:
            return svc.candidates("a", cid, 1, str(i), [candidate()])["revision"]
        except GeoError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(add, range(4)))
    assert results.count(2) == 1
    assert results.count("REVISION_CONFLICT") == 3
    assert len(runtime.db.read("a", cid)["candidates"]) == 1
    assert runtime.db.integrity("a")["status"] == "OK"


def test_backup_delete_and_integrity(runtime, tmp_path):
    cid = runtime.service.create("a", "Тест", "c")["id"]
    runtime.db.backup(tmp_path / "backup.sqlite")
    restored = Database(tmp_path / "backup.sqlite")
    assert restored.read("a", cid)["id"] == cid
    restored.close()
    with pytest.raises(GeoError):
        runtime.service.delete("b", cid, 1)
    runtime.service.delete("a", cid, 1)
    with pytest.raises(GeoError):
        runtime.db.read("a", cid)
    assert runtime.db.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert runtime.db.connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0


def test_tampered_document_detected(runtime):
    cid = runtime.service.create("a", "Тест", "c")["id"]
    runtime.db.connection.execute("UPDATE cases SET document='{}' WHERE id=?", (cid,))
    assert runtime.db.integrity("a")["status"] == "CORRUPTED"
