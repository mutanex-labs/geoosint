from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_mcp import running_server


async def test_mcp_spatial_support_contradiction_preliminary_and_restart(tmp_path, monkeypatch):
    with running_server(
        tmp_path, osm_enabled=True, osm_user_agent="GeoOSINT/0.1 (offline test)"
    ) as (url, runtime):

        async def fetch(query, bounds, charge_external, attempt_observer=None):
            charge_external()
            return {
                "features": [
                    {
                        "source_id": f"node/{i}",
                        "point": {"latitude": 55.75, "longitude": lon},
                        "point_method": "OSM_NODE",
                        "untrusted_tags": {"name": f"Synthetic {i}"},
                    }
                    for i, lon in [(1, 37.62), (2, 37.621), (3, 37.629)]
                ],
                "retrieved_at": "2026-10-20T00:00:00+00:00",
                "truncated": False,
            }

        monkeypatch.setattr(runtime.osm, "_fetch", fetch)
        async with streamable_http_client(url + "/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **arguments):
                    result = (await session.call_tool(name, arguments)).structuredContent
                    assert result["status"] == "OK", result
                    return result["data"]

                cid = (await call("geo_case_create", title="Spatial checks", idempotency_key="c"))[
                    "id"
                ]
                revision = 1

                async def mutate(name, **arguments):
                    nonlocal revision
                    response = await call(
                        name,
                        case_id=cid,
                        expected_revision=revision,
                        idempotency_key=f"step-{revision}",
                        **arguments,
                    )
                    revision = response["revision"]
                    return response["result"]

                obs = await mutate(
                    "geo_observation_add",
                    observation={
                        "category": "INFRASTRUCTURE",
                        "text": "Two visible features roughly 60 m apart",
                        "source": "synthetic client fixture",
                        "dependency_group": "image",
                        "spatial_constraint": {
                            "relation": "DISTANCE",
                            "expected": 60,
                            "tolerance": 10,
                        },
                    },
                )
                op = await mutate(
                    "geo_osm_features",
                    bounds={"south": 55.74, "west": 37.61, "north": 55.76, "east": 37.64},
                    category="landmarks",
                )
                candidates = await mutate(
                    "geo_candidates_add",
                    candidates=[
                        {
                            "label": str(i),
                            "level": "POINT",
                            "point": {"latitude": 55.75, "longitude": lon},
                            "area": {
                                "south": 55.749,
                                "west": lon - 0.0001,
                                "north": 55.751,
                                "east": lon + 0.0001,
                            },
                            "source_operation_id": op["id"],
                            "source_feature_id": f"node/{i}",
                        }
                        for i, lon in [(1, 37.62), (3, 37.629)]
                    ],
                )
                for candidate, expected in zip(candidates, [True, False], strict=True):
                    checked = await mutate(
                        "geo_spatial_check",
                        candidate_id=candidate["id"],
                        observation_id=obs["id"],
                        first={
                            "operation_id": op["id"],
                            "source_id": candidate["source_feature_id"],
                        },
                        second={"operation_id": op["id"], "source_id": "node/2"},
                    )
                    assert checked["result"]["matches"] is expected
                    await mutate(
                        "geo_evidence_add",
                        evidence={
                            "candidate_id": candidate["id"],
                            "operation_id": checked["id"],
                            "effect": "SUPPORTS" if expected else "CONTRADICTS",
                            "description": "Source point relation",
                            "observation_ids": [obs["id"]],
                        },
                    )
                result = await mutate(
                    "geo_case_finalize",
                    level="POINT",
                    assessment="FACT_SUPPORTED",
                    candidate_id=candidates[0]["id"],
                )
                assert result["scene_verified"] is False
                assert result["location_status"] == "FACT_SUPPORTED"
                assert result["alternatives"][0]["location_status"] == "NEEDS_REVIEW"
                assert result["radius_m"] is None
                assert runtime.service.read("local-operator", cid)["state"] == "REVIEW_REQUIRED"
                failed = await session.call_tool(
                    "geo_case_finalize",
                    {
                        "case_id": cid,
                        "expected_revision": revision,
                        "idempotency_key": "verified",
                        "level": "POINT",
                        "assessment": "LOCATION_VERIFIED",
                        "candidate_id": candidates[0]["id"],
                    },
                )
                assert failed.isError
    with running_server(tmp_path) as (_, runtime):
        report = runtime.service.export("local-operator", cid)
        assert report["case"]["result"]["location_status"] == "FACT_SUPPORTED"
        assert report["case"]["state"] != "RESOLVED"
        assert len(report["candidate_assessments"]) == 2
