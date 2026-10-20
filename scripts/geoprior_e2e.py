"""Known public urban case through MCP; actual client web sources, never a blind score.

Run in the separate integration container: python - prepare|verify.
No private image upload. Web reads were performed by the client before this replayable ledger.
"""

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

ROOT = Path("/data")
MARKER = ROOT / "geoprior-e2e-marker.json"
REPORT = ROOT / "geoprior-e2e-report.json"
IMAGE = "https://images.unsplash.com/photo-1752698456462-96a97b649c22?auto=format&fit=crop&fm=jpg&q=80&w=1800"


async def main():
    async with streamable_http_client("http://127.0.0.1:8787/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(name, **arguments):
                response = (await session.call_tool(name, arguments)).structuredContent
                assert response["status"] == "OK", response
                return response["data"]

            if sys.argv[1] == "verify":
                marker = json.loads(MARKER.read_text())
                report = await call("geo_case_export", case_id=marker["case_id"])
                assert report["case"]["result"]["scene_verified"] is False
                assert report["case"]["state"] == "REVIEW_REQUIRED"
                assert report["case"]["result"]["best_candidate"]["model_geo_prior_ids"]
                assert report["case"]["result"]["point"] is None
                assert report["case"]["candidates"][0]["id"] == marker["candidate_id"]
                budget = await call("geo_case_next", case_id=marker["case_id"])
                attempts = [
                    t for t in report["request_traces"] if t.get("kind") == "PROVIDER_ATTEMPT"
                ]
                assert budget["budget_used"]["external_requests"] == len(attempts)
                report["restart_budget_used"] = budget["budget_used"]
                (ROOT / "geoprior-e2e-after-restart.json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    json.dumps(
                        {
                            "status": "OK",
                            "phase": "restart-verified",
                            **marker,
                            "external_requests": budget["budget_used"]["external_requests"],
                            "provider_attempts": len(attempts),
                        }
                    )
                )
                return
            assert not MARKER.exists(), "Do not overwrite an existing recorded case"
            cid = (
                await call(
                    "geo_case_create",
                    title="Known public Osaka GeoPrior E2E, not blind",
                    idempotency_key=str(uuid4()),
                    search_scope={
                        "mode": "AUTO",
                        "budget": {
                            "max_tool_calls": 80,
                            "max_external_requests": 3,
                            "max_provider_requests": 2,
                            "max_duration_seconds": 1800,
                        },
                    },
                )
            )["id"]
            revision = 1

            async def mutate(name, **arguments):
                nonlocal revision
                response = await call(
                    name,
                    case_id=cid,
                    expected_revision=revision,
                    idempotency_key=str(uuid4()),
                    **arguments,
                )
                revision = response["revision"]
                return response["result"]

            def source(url, title, relevance, access="PAGE_OPENED"):
                return dict(
                    url=url,
                    title=title,
                    relevance=relevance,
                    access=access,
                    retrieved_at=datetime.now(UTC).isoformat(),
                    dependency_group=url,
                )

            async def observe(text, **fields):
                return await mutate(
                    "geo_observation_add",
                    observation=dict(
                        category="OTHER",
                        text=text,
                        source="Codex multimodal client + native web",
                        dependency_group="public-urban-image",
                        **fields,
                    ),
                )

            prior = await observe(
                "MODEL prior: Japanese commercial street; visible 新世界 and YOLO BASE 320m. "
                "Osaka/Shinsekai is a supported hypothesis; Japanese script alone is widespread. "
                "Known regression example, not blind. Exact camera location unknown.",
                observation_type="MODEL_GEO_PRIOR",
                scene_graph=dict(
                    client_image_ref=IMAGE,
                    passes_completed=["global", "small-sign-detail"],
                    nodes=[
                        dict(
                            id="sign",
                            description="新世界, legible on vertical sign",
                            observation_type="text",
                            recognition="HIGH",
                            geographic_information="MEDIUM",
                            alternatives=["A reused commercial name elsewhere"],
                        ),
                        dict(
                            id="anchor",
                            description="YOLO BASE 320m direction sign",
                            observation_type="direction-sign",
                            recognition="HIGH",
                            geographic_information="HIGH",
                        ),
                    ],
                ),
                research=dict(
                    kind="VISUAL_REFERENCE",
                    client_tool="cua_repl.getScreenshot",
                    tool_calls=1,
                    capabilities={
                        "BROWSER_AVAILABLE": True,
                        "WEB_SEARCH_AVAILABLE": True,
                        "WEB_PAGES_AVAILABLE": True,
                        "MAP_VIEW_AVAILABLE": False,
                        "REVERSE_IMAGE_SEARCH_AVAILABLE": False,
                    },
                    sources=[
                        source(
                            IMAGE,
                            "Public Unsplash image",
                            "Actually viewed and enlarged by the client",
                            "IMAGE_VIEWED",
                        )
                    ],
                ),
            )
            await observe(
                "Actual native web search, not independently verified from snippets.",
                research=dict(
                    kind="SEARCH_RESULT",
                    client_tool="web.run",
                    tool_calls=1,
                    queries=['"YOLO BASE" "新世界" 住所', '"新世界" "射的" 商店街 大阪'],
                ),
            )
            pages = await observe(
                "Opened primary pages place Shinsekai and YOLO BASE in Osaka. "
                "Official tourism page embeds a map query q=34.65233,135.50618. "
                "That is a district reference marker, not a camera coordinate. "
                "The map itself was inaccessible. A generic Tokyo label is not evidence.",
                research=dict(
                    kind="PAGE_CONTENT",
                    client_tool="web.run",
                    tool_calls=2,
                    queries=["site.osaka-info.jp 新世界 アクセス 通天閣"],
                    sources=[
                        source(
                            "https://www.yolo-base.com/access/",
                            "YOLO BASE access",
                            "Official address Osaka, Ebisu-nishi 3-13-24",
                        ),
                        source(
                            "https://osaka-info.jp/spot/shinsekai/",
                            "Osaka tourism Shinsekai",
                            "District address and explicit map query coordinate",
                        ),
                        source(
                            "https://www.city.osaka.lg.jp/naniwa/page/0000632322.html",
                            "Osaka city district history",
                            "Independent municipal district context",
                        ),
                    ],
                ),
            )
            await observe(
                "Google map fetch failed; no map pixels or panorama were compared. "
                "YOLO map embed returned no readable content; its viewport is not an object coordinate.",
                research=dict(
                    kind="GEO_SOURCE",
                    client_tool="web.run",
                    tool_calls=2,
                    outcome="UNAVAILABLE",
                    sources=[
                        source(
                            "https://maps.google.com/maps?output=embed&q=34.65233%2C135.50618",
                            "Official linked map unavailable",
                            "No visual/geometric confirmation",
                            "UNAVAILABLE",
                        )
                    ],
                ),
            )
            # Window derived from the explicit public district marker, not a boundary or error radius.
            bounds = dict(south=34.64233, west=135.49618, north=34.66233, east=135.51618)
            candidate = (
                await mutate(
                    "geo_candidates_add",
                    candidates=[
                        dict(
                            label="Japan, Osaka, Shinsekai district — preliminary area hypothesis",
                            level="AREA",
                            area=bounds,
                            observation_ids=[prior["id"], pages["id"]],
                        )
                    ],
                )
            )[0]
            overpass = (
                await session.call_tool(
                    "geo_osm_features",
                    dict(
                        case_id=cid,
                        expected_revision=revision,
                        idempotency_key=str(uuid4()),
                        category="names",
                        name_query="通天閣",
                        bounds=bounds,
                        limit=10,
                    ),
                )
            ).structuredContent
            if overpass["status"] == "OK":
                revision = overpass["data"]["revision"]
            await mutate(
                "geo_geometry",
                calculation=dict(
                    operation="bounds",
                    center=dict(latitude=34.65233, longitude=135.50618),
                    radius_m=1000,
                ),
            )
            final = await mutate(
                "geo_case_finalize",
                level="AREA",
                assessment="HYPOTHESIS",
                candidate_id=candidate["id"],
            )
            assert final["scene_verified"] is False and final["point"] is None
            assert final["radius_m"] is None
            report = await call("geo_case_export", case_id=cid)
            report["exercise"] = dict(
                blind=False,
                source="public known regression case",
                overpass_status=overpass["status"],
                overpass_errors=overpass["errors"],
                result="Osaka/Shinsekai AREA HYPOTHESIS, camera position unverified",
            )
            REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            marker = dict(
                case_id=cid, candidate_id=candidate["id"], overpass_status=overpass["status"]
            )
            MARKER.write_text(json.dumps(marker), encoding="utf-8")
            print(
                json.dumps(
                    dict(
                        status="OK", phase="prepared", **marker, scene_verified=False, level="AREA"
                    )
                )
            )


asyncio.run(main())
