"""Two-stage container restart test; only generated disposable case and image."""

import io
import json
import sys

from PIL import Image, TiffImagePlugin

from geoosint.cases.service import ObservationInput
from geoosint.config import Settings
from geoosint.core.models import Bounds, CandidateInput
from geoosint.core.scope import ScopeArea, ScopePolygon, SearchScope
from geoosint.mcp.server import OWNER, Runtime


def main():
    runtime = Runtime(Settings.from_env())
    marker = runtime.settings.data_dir / "verification-case.json"
    try:
        if sys.argv[1] == "prepare":
            if marker.exists():
                raise RuntimeError("A previous test marker already exists")
            scope = SearchScope(
                mode="PREFERRED",
                areas=[
                    ScopeArea(
                        kind="POLYGON",
                        geometry=ScopePolygon(
                            coordinates=[[(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)]]
                        ),
                    )
                ],
            )
            case = runtime.service.create(
                OWNER, "Generated persistence self-test", "persistence-test", search_scope=scope
            )
            cid = case["id"]
            exif = Image.Exif()
            r = TiffImagePlugin.IFDRational
            exif[34853] = {1: "N", 2: (r(1), r(0), r(0)), 3: "E", 4: (r(1), r(0), r(0))}
            content = io.BytesIO()
            Image.new("RGB", (8, 8)).save(content, format="JPEG", exif=exif.tobytes())
            imported = runtime.artifacts.import_bytes(OWNER, cid, 1, "image", content.getvalue())
            aid = imported["result"]["artifact_id"]
            runtime.service.candidates(
                OWNER,
                cid,
                2,
                "candidates",
                [
                    CandidateInput(
                        label="Synthetic candidate",
                        level="AREA",
                        area=Bounds(south=0.5, west=0.5, north=1.5, east=1.5),
                    )
                ],
            )
            runtime.service.expand(OWNER, cid, 3, "expansion", "USER_AUTHORIZED", [], True)
            runtime.service.observation(
                OWNER,
                cid,
                4,
                "scene-memory",
                ObservationInput(
                    category="OTHER",
                    text="Synthetic persistent scene",
                    source="self-test",
                    dependency_group="synthetic-image",
                    scene_graph={
                        "artifact_id": aid,
                        "passes_completed": ["fixture"],
                        "nodes": [
                            {
                                "id": "detail",
                                "description": "Synthetic detail",
                                "observation_type": "arbitrary-domain",
                            }
                        ],
                    },
                ),
            )
            runtime.service.consume_external(OWNER, cid)
            runtime.service.provider_failure(
                OWNER,
                cid,
                "UPSTREAM_UNAVAILABLE",
                {
                    "bounds": {"south": 0, "west": 0, "north": 0.01, "east": 0.01},
                    "category": "roads",
                },
            )
            marker.write_text(json.dumps({"case_id": cid, "artifact_id": aid}), encoding="utf-8")
            print(json.dumps({"status": "OK", "phase": "prepared"}))
        elif sys.argv[1] == "verify-delete":
            saved = json.loads(marker.read_text())
            cid, aid = saved["case_id"], saved["artifact_id"]
            document = runtime.service.read(OWNER, cid)
            assert document["scope_mode"] == "PREFERRED"
            assert document["expansion_level"] == 1
            assert len(document["expansion_history"]) == 1
            assert len(document["candidates"]) == 1
            assert document["observations"][-1]["scene_graph"]["artifact_id"] == aid
            graph = runtime.service.export(OWNER, cid)["evidence_graph"]
            assert any(node["kind"] == "SCENE_CLUE" for node in graph["nodes"])
            assert runtime.service.coverage_status(OWNER, cid) == "PROVIDER_UNAVAILABLE"
            assert runtime.service.coverage_events(OWNER, cid)[-1]["negative_evidence"] is False
            assert document["scope_checked"] == []
            assert (
                runtime.service.budget_status(OWNER, cid)["budget_used"]["external_requests"] == 1
            )
            path, _ = runtime.artifacts.get(OWNER, cid, aid)
            assert runtime.artifacts.inspect_isolated(path)["latitude"] == 1
            assert runtime.db.integrity(OWNER)["status"] == "OK"
            runtime.service.delete(OWNER, cid, document["revision"])
            assert not path.exists()
            assert not runtime.db.connection.execute(
                "SELECT id FROM artifacts WHERE case_id=?", (cid,)
            ).fetchone()
            marker.unlink()
            print(json.dumps({"status": "OK", "phase": "restart-verified-and-deleted"}))
        else:
            raise ValueError("Expected prepare or verify-delete")
    finally:
        runtime.close()


if __name__ == "__main__":
    main()
