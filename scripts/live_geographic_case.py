"""Настоящее MCP-расследование публичного места. Без mock и без исходных фотографий.

Выполнение внутри отдельного acceptance-контейнера: python - prepare|verify.
Ошибка источника сохраняется как BLOCKED; успешная выдача не объявляет сцену VERIFIED.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from geoosint.core.geometry import inverse
from geoosint.core.models import Point
from geoosint.serialization import now

ROOT = Path("/data")


async def main():
    async with streamable_http_client("http://127.0.0.1:8787/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if sys.argv[1] == "verify":
                marker = json.loads((ROOT / "live-case-marker.json").read_text())
                result = (
                    await session.call_tool("geo_case_export", {"case_id": marker["case_id"]})
                ).structuredContent
                assert result["status"] == "OK"
                report = result["data"]
                assert report["case"]["scope_mode"] == "PREFERRED"
                assert report["case"]["result"]["scene_verified"] is False
                assert len(report["case"]["candidates"]) == marker["candidate_count"]
                assert len(report["request_traces"]) >= marker["trace_count"]
                (ROOT / "live-case-after-restart.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "status": "OK",
                            "phase": "restart-verified",
                            "case_id": marker["case_id"],
                            "source_status": marker["source_status"],
                        }
                    )
                )
                return
            if (ROOT / "live-case-marker.json").exists():
                raise RuntimeError("Existing acceptance case: use verify, do not overwrite")
            revision = 1
            cid = None

            async def mutate(name, **arguments):
                nonlocal revision
                response = (
                    await session.call_tool(
                        name,
                        {
                            "case_id": cid,
                            "expected_revision": revision,
                            "idempotency_key": f"live-{revision}-{name}",
                            **arguments,
                        },
                    )
                ).structuredContent
                if response["status"] == "OK":
                    revision = response["data"]["revision"]
                return response

            created = (
                await session.call_tool(
                    "geo_case_create",
                    {
                        "title": "Known public geographic engine test: Zaryadye, not blind photo geolocation",
                        "idempotency_key": "known-public-geography",
                        "search_scope": {
                            "mode": "PREFERRED",
                            "areas": [{"kind": "CITY", "object_id": "osm:relation/2555133"}],
                            "budget": {
                                "max_external_requests": 6,
                                "max_provider_requests": 4,
                                "max_tool_calls": 80,
                            },
                        },
                    },
                )
            ).structuredContent
            assert created["status"] == "OK", created
            cid = created["data"]["id"]
            observation = await mutate(
                "geo_observation_add",
                observation={
                    "category": "INFRASTRUCTURE",
                    "text": "Known public test hypothesis: bridge near historic central landmark. Expected source-point distance 400 +/- 150 m; this is not analysis of a supplied photograph.",
                    "source": "PREDECLARED_PUBLIC_TEST",
                    "dependency_group": "known-test",
                    "spatial_constraint": {
                        "relation": "DISTANCE",
                        "expected": 400,
                        "tolerance": 150,
                    },
                },
            )
            bounds = {"south": 55.745, "west": 37.616, "north": 55.756, "east": 37.641}
            bridges = await mutate("geo_osm_features", category="bridges", bounds=bounds, limit=100)
            status = "BLOCKED"
            final_arguments = {}
            if bridges["status"] == "OK":
                bridge_op = bridges["data"]["result"]
                landmarks = await mutate(
                    "geo_osm_features", category="landmarks", bounds=bounds, limit=100
                )
                if landmarks["status"] == "OK":
                    landmark_op = landmarks["data"]["result"]
                    anchors = [
                        f
                        for f in landmark_op["result"]["features"]
                        if any(
                            word in f["untrusted_tags"].get("name", "").lower()
                            for word in ("василия", "покров")
                        )
                        and f.get("point")
                    ]
                    choices = [
                        f
                        for f in bridge_op["result"]["features"]
                        if f.get("point") and f.get("geometry")
                    ]
                    assert anchors and choices, (
                        "Known public objects absent: review actual coverage; do not invent them"
                    )
                    assert (
                        bridge_op["result"]["attribution"]
                        and bridge_op["result"]["license"] == "ODbL-1.0"
                    )
                    anchor = anchors[0]
                    choices = sorted(
                        choices,
                        key=lambda f: (
                            "парящ" not in f["untrusted_tags"].get("name", "").lower(),
                            f["source_id"],
                        ),
                    )[:3]
                    candidate_inputs = []
                    for feature in choices:
                        point = feature["point"]
                        lat, lon = point["latitude"], point["longitude"]
                        candidate_inputs.append(
                            {
                                "label": feature["untrusted_tags"].get(
                                    "name", feature["source_id"]
                                ),
                                "level": "POINT",
                                "point": point,
                                "area": {
                                    "south": lat - 0.00001,
                                    "west": lon - 0.00001,
                                    "north": lat + 0.00001,
                                    "east": lon + 0.00001,
                                },
                                "source_operation_id": bridge_op["id"],
                                "source_feature_id": feature["source_id"],
                            }
                        )
                    added = await mutate("geo_candidates_add", candidates=candidate_inputs)
                    assert added["status"] == "OK", added
                    for candidate, feature in zip(added["data"]["result"], choices, strict=True):
                        checked = await mutate(
                            "geo_spatial_check",
                            candidate_id=candidate["id"],
                            observation_id=observation["data"]["result"]["id"],
                            first={
                                "operation_id": bridge_op["id"],
                                "source_id": feature["source_id"],
                            },
                            second={
                                "operation_id": landmark_op["id"],
                                "source_id": anchor["source_id"],
                            },
                        )
                        assert checked["status"] == "OK", checked
                        calculation = checked["data"]["result"]
                        expected = inverse(
                            Point.model_validate(feature["point"]),
                            Point.model_validate(anchor["point"]),
                        )["distance_m"]
                        assert abs(calculation["result"]["value"] - expected) < 0.001
                        await mutate(
                            "geo_evidence_add",
                            evidence={
                                "candidate_id": candidate["id"],
                                "operation_id": calculation["id"],
                                "effect": "SUPPORTS"
                                if calculation["result"]["matches"]
                                else "CONTRADICTS",
                                "description": "Deterministic source point distance; client image correspondence unverified",
                                "observation_ids": [observation["data"]["result"]["id"]],
                            },
                        )
                    final_arguments = {
                        "level": "POINT",
                        "assessment": "PRELIMINARY",
                        "candidate_id": added["data"]["result"][0]["id"],
                    }
                    status = "REAL_GEODATA_PASS"
            finalized = await mutate("geo_case_finalize", **final_arguments)
            assert finalized["status"] == "OK", finalized
            exported = (
                await session.call_tool("geo_case_export", {"case_id": cid})
            ).structuredContent
            report = exported["data"]
            assert report["case"]["result"]["scene_verified"] is False
            traces = report["request_traces"]
            attempts = [event for event in traces if event["kind"] == "PROVIDER_ATTEMPT"]
            report["acceptance"] = {
                "status": status,
                "recorded_at": now(),
                "not_blind_geolocation": True,
                "provider_attempts": len(attempts),
                "errors": [event for event in traces if event.get("outcome") == "ERROR"],
            }
            (ROOT / "live-case-report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (ROOT / "live-case-marker.json").write_text(
                json.dumps(
                    {
                        "case_id": cid,
                        "candidate_count": len(report["case"]["candidates"]),
                        "trace_count": len(traces),
                        "source_status": status,
                    }
                ),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "status": status,
                        "case_id": cid,
                        "provider_attempts": len(attempts),
                        "candidates": len(report["case"]["candidates"]),
                    }
                )
            )


asyncio.run(main())
