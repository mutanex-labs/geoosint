"""Run inside the isolated container; no external service calls."""

import asyncio
import json
import urllib.request
from uuid import uuid4

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    for route in ("healthz", "readyz"):
        with urllib.request.urlopen(f"http://127.0.0.1:8787/{route}", timeout=3) as response:
            assert response.status == 200
    async with streamable_http_client("http://127.0.0.1:8787/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            created = await session.call_tool(
                "geo_case_create",
                {"title": "Container smoke test", "idempotency_key": str(uuid4())},
            )
            case_id = created.structuredContent["data"]["id"]
            try:
                result = await session.call_tool(
                    "geo_geometry",
                    {
                        "case_id": case_id,
                        "expected_revision": 1,
                        "idempotency_key": str(uuid4()),
                        "calculation": {
                            "operation": "inverse",
                            "start": {"latitude": 0, "longitude": 0},
                            "end": {"latitude": 0, "longitude": 1},
                        },
                    },
                )
                assert result.structuredContent["status"] == "OK", result
                distance = result.structuredContent["data"]["result"]["result"]["distance_m"]
                assert abs(distance - 111319.490793) < 0.001
                observed = await session.call_tool(
                    "geo_observation_add",
                    {
                        "case_id": case_id,
                        "expected_revision": 2,
                        "idempotency_key": str(uuid4()),
                        "observation": {
                            "category": "OTHER",
                            "text": "Synthetic architecture fixture",
                            "source": "container self-test",
                            "dependency_group": "fixture",
                            "scene_graph": {
                                "client_image_ref": "fixture:synthetic-not-a-real-photo",
                                "passes_completed": ["fixture"],
                                "nodes": [
                                    {
                                        "id": "detail",
                                        "description": "Synthetic detail",
                                        "observation_type": "arbitrary-domain",
                                    }
                                ],
                            },
                            "research_actions": [
                                {
                                    "id": "geometry-next",
                                    "question": "Check a spatial relation",
                                    "strategy": "GEOMETRY",
                                    "execution": "MCP",
                                    "mcp_tool": "geo_geometry",
                                    "expected_information": 2,
                                    "discrimination": 2,
                                    "source_reliability": 3,
                                    "independence": 1,
                                    "uninformative_risk": 1,
                                    "estimated_requests": 0,
                                    "estimated_seconds": 1,
                                }
                            ],
                        },
                    },
                )
                assert observed.structuredContent["status"] == "OK", observed
                planned = await session.call_tool("geo_case_next", {"case_id": case_id})
                assert planned.structuredContent["data"]["action"] == "RESEARCH", planned
                exported = await session.call_tool("geo_case_export", {"case_id": case_id})
                graph = exported.structuredContent["data"]["evidence_graph"]
                assert graph["nodes"] and graph["scene_verified"] is False
                print(
                    json.dumps(
                        {
                            "status": "OK",
                            "protocol": initialized.protocolVersion,
                            "tools": len(tools.tools),
                            "distance_m": distance,
                            "investigation": "SCENE_GRAPH_RESEARCH_PLAN_EVIDENCE_GRAPH",
                        }
                    )
                )
            finally:
                current = await session.call_tool("geo_case_get", {"case_id": case_id})
                deleted = await session.call_tool(
                    "geo_case_delete",
                    {
                        "case_id": case_id,
                        "expected_revision": current.structuredContent["data"]["revision"],
                    },
                )
                assert deleted.structuredContent["status"] == "OK"


asyncio.run(main())
