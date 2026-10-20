from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_mcp import running_server


async def test_unavailable_provider_persists_unperformed_territory(tmp_path):
    parameters = {
        "category": "roads",
        "bounds": {"south": 1, "west": 1, "north": 1.01, "east": 1.01},
    }
    with running_server(tmp_path) as (url, runtime):
        async with streamable_http_client(url + "/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                created = await session.call_tool(
                    "geo_case_create", {"title": "Coverage", "idempotency_key": "create"}
                )
                cid = created.structuredContent["data"]["id"]
                failed = await session.call_tool(
                    "geo_osm_features",
                    {
                        "case_id": cid,
                        "expected_revision": 1,
                        "idempotency_key": "failure",
                        **parameters,
                    },
                )
                assert failed.structuredContent["errors"][0]["code"] == "UPSTREAM_UNAVAILABLE"
                assert failed.structuredContent["budget_used"]["provider_requests"] == 1
                assert failed.structuredContent["budget_used"]["external_requests"] == 0
                assert runtime.service.read("local-operator", cid)["revision"] == 1
    with running_server(tmp_path) as (url, runtime):
        service = runtime.service
        report = service.export("local-operator", cid)
        assert report["coverage_status"] == "PROVIDER_UNAVAILABLE"
        assert report["case"]["scope_checked"] == []
        assert report["case"]["evidence"] == []
        assert report["case"]["expansion_history"] == []
        event = report["scope_events"][-1]
        assert event["parameters"]["bounds"] == parameters["bounds"]
        assert event["parameters"]["category"] == "roads"
        assert event["negative_evidence"] is False
        assert event["operation_ids"] == []
        assert event["scope_revision"] == 1


async def test_scope_success_and_strict_network_gate_http(tmp_path):
    with running_server(tmp_path) as (url, runtime):
        async with streamable_http_client(url + "/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                scope = {
                    "mode": "STRICT",
                    "areas": [
                        {
                            "kind": "POLYGON",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
                            },
                        }
                    ],
                }
                created = await session.call_tool(
                    "geo_case_create",
                    {"title": "Scope HTTP", "idempotency_key": "create", "search_scope": scope},
                )
                assert created.structuredContent["status"] == "OK"
                cid = created.structuredContent["data"]["id"]
                denied = await session.call_tool(
                    "geo_osm_features",
                    {
                        "case_id": cid,
                        "expected_revision": 1,
                        "idempotency_key": "denied",
                        "category": "roads",
                        "bounds": {"south": 4, "west": 4, "north": 4.01, "east": 4.01},
                    },
                )
                assert denied.structuredContent["errors"][0]["code"] == "SCOPE_VIOLATION"
                assert runtime.osm.requests == 0
                assert denied.structuredContent["budget_used"]["provider_requests"] == 0
                preferred = {**scope, "mode": "PREFERRED"}
                changed = await session.call_tool(
                    "geo_case_scope_set",
                    {
                        "case_id": cid,
                        "expected_revision": 1,
                        "idempotency_key": "change",
                        "search_scope": preferred,
                    },
                )
                assert changed.structuredContent["status"] == "OK"
                expanded = await session.call_tool(
                    "geo_case_scope_expand",
                    {
                        "case_id": cid,
                        "expected_revision": 2,
                        "idempotency_key": "expand",
                        "reason": "USER_AUTHORIZED",
                        "user_authorized": True,
                    },
                )
                assert expanded.structuredContent["status"] == "OK"
                assert (
                    expanded.structuredContent["expansion_reasons"][0]["reason"]
                    == "USER_AUTHORIZED"
                )
                report = await session.call_tool("geo_case_export", {"case_id": cid})
                assert report.structuredContent["data"]["case"]["scope_revision"] == 2
                assert report.structuredContent["data"]["case"]["expansion_level"] == 1
