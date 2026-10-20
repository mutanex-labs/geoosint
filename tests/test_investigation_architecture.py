import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import ValidationError
from test_mcp import running_server
from test_scope import scope

from geoosint.cases.service import ObservationInput
from geoosint.core.investigation import ResearchAction, SceneGraph
from geoosint.core.models import Bounds, CandidateInput
from geoosint.core.research import ClientCapabilities, ResearchRecord
from geoosint.errors import GeoError
from geoosint.mcp.server import Runtime
from geoosint.serialization import digest


def observation(**kwargs):
    return ObservationInput(
        category="OTHER",
        text="Research state",
        source="client",
        dependency_group="original",
        **kwargs,
    )


def action(identifier="differentiate", **kwargs):
    fields = dict(
        id=identifier,
        question="Which candidate fits the observed relation?",
        strategy="FALSIFY",
        execution="CLIENT",
        required_capability="WEB_SEARCH_AVAILABLE",
        expected_information=3,
        discrimination=3,
        source_reliability=2,
        independence=3,
        uninformative_risk=0,
        estimated_requests=1,
        estimated_seconds=15,
    )
    fields.update(kwargs)
    if fields["execution"] == "MCP":
        fields["mcp_tool"] = "geo_geometry"
    return ResearchAction(**fields)


def graph(**kwargs):
    return SceneGraph(
        **(
            dict(
                client_image_ref="client:original",
                passes_completed=["detail"],
                nodes=[
                    dict(
                        id="a",
                        description="Small distinguishing detail",
                        observation_type="agent_selected_domain",
                    )
                ],
            )
            | kwargs
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        dict(nodes=[dict(id="a", description="x", observation_type="x", depends_on=["a"])]),
        dict(nodes=[dict(id="a", description="x", observation_type="x", depends_on=["missing"])]),
        dict(relations=[dict(source="a", target="missing", relation="near", description="x")]),
        dict(
            nodes=[
                dict(
                    id="a",
                    description="x",
                    observation_type="x",
                    region=dict(x=0.9, y=0, width=0.5, height=0.1),
                )
            ]
        ),
        dict(artifact_id=None, client_image_ref=None),
    ],
)
def test_scene_graph_rejects_invalid_structure(changes):
    with pytest.raises(ValidationError):
        graph(**changes)


def test_persistent_plan_graph_and_hypothesis_recovery(runtime):
    service = runtime.service
    cid = service.create("alice", "Architecture", "create")["id"]
    observed = service.observation("alice", cid, 1, "scene", observation(scene_graph=graph()))
    oid = observed["result"]["id"]
    created = service.candidates(
        "alice",
        cid,
        2,
        "candidate",
        [
            CandidateInput(
                label="First hypothesis",
                level="AREA",
                area=Bounds(south=0, west=0, north=1, east=1),
                observation_ids=[oid],
            )
        ],
    )
    candidate_id = created["result"][0]["id"]
    service.observation(
        "alice",
        cid,
        3,
        "plan",
        observation(
            research=ResearchRecord(
                kind="SEARCH_RESULT",
                client_tool="available-search",
                tool_calls=0,
                capabilities=ClientCapabilities(WEB_SEARCH_AVAILABLE=True),
            ),
            research_actions=[
                action(candidate_ids=[candidate_id], observation_ids=[oid]),
                action("expensive", estimated_requests=20, estimated_seconds=600),
            ],
        ),
    )
    plan = service.next("alice", cid)
    assert plan["action"] == "RESEARCH"
    assert plan["research_plan"]["recommended"][0]["id"] == "differentiate"
    assert plan["research_plan"]["automatically_executed"] is False
    service.rank("alice", cid, 4, "down", candidate_id, -80, "Weak early similarity")
    service.rank("alice", cid, 5, "recover", candidate_id, 10, "New independent clue")
    service.observation(
        "alice",
        cid,
        6,
        "completed",
        observation(
            action_outcomes=[
                dict(
                    action_id="differentiate",
                    status="NO_USEFUL_RESULTS",
                    explanation="No discriminating source",
                )
            ]
        ),
    )
    restarted = Runtime(runtime.settings)
    try:
        exported = restarted.service.export("alice", cid)
        assert "differentiate" in exported["research_plan"]["completed_action_ids"]
        assert all(a["id"] != "differentiate" for a in exported["research_plan"]["recommended"])
        ledger = exported["hypothesis_ledger"][0]
        assert ledger["status"] == "ACTIVE" and len(ledger["priority_history"]) == 2
        assert ledger["observation_ids"] == [oid]
        evidence_graph = exported["evidence_graph"]
        ids = {n["id"] for n in evidence_graph["nodes"]}
        assert all(e["source"] in ids and e["target"] in ids for e in evidence_graph["edges"])
        assert evidence_graph["scene_verified"] is False
        with pytest.raises(GeoError):
            restarted.service.export("bob", cid)
    finally:
        restarted.close()


def test_unknown_capability_costs_and_cross_case_refs(runtime):
    service = runtime.service
    cid = service.create("alice", "Architecture", "create")["id"]
    service.observation(
        "alice",
        cid,
        1,
        "plan",
        observation(
            research_actions=[
                action(),
                action(
                    "network", execution="MCP", required_capability=None, estimated_requests=100
                ),
            ]
        ),
    )
    plan = service.next("alice", cid)["research_plan"]
    blocked = {a["id"]: a["blocked_reasons"] for a in plan["blocked"]}
    assert "CAPABILITY_NOT_CONFIRMED" in blocked["differentiate"]
    assert "NETWORK_BUDGET" in blocked["network"]
    with pytest.raises(GeoError):
        service.observation(
            "alice",
            cid,
            2,
            "foreign",
            observation(
                research_actions=[action("foreign", candidate_ids=["other-case-candidate"])]
            ),
        )
    with pytest.raises(GeoError):
        service.observation(
            "alice",
            cid,
            2,
            "artifact",
            observation(scene_graph=graph(artifact_id="other-user-file")),
        )
    with pytest.raises(GeoError):
        service.observation("alice", cid, 2, "duplicate", observation(research_actions=[action()]))


@pytest.mark.parametrize("mode", ["STRICT", "PREFERRED"])
def test_plan_respects_scope_even_for_high_information_action(runtime, mode):
    service = runtime.service
    cid = service.create("alice", "Scoped research", "create", search_scope=scope(mode))["id"]
    outside = action(
        "outside",
        execution="MCP",
        required_capability=None,
        area=Bounds(south=3, west=3, north=4, east=4),
    )
    if mode == "STRICT":
        with pytest.raises(GeoError) as error:
            service.observation("alice", cid, 1, "outside", observation(research_actions=[outside]))
        assert error.value.code == "SCOPE_VIOLATION"
    else:
        service.observation("alice", cid, 1, "outside", observation(research_actions=[outside]))
        plan = service.next("alice", cid)["research_plan"]
        assert not plan["recommended"]
        assert plan["blocked"][0]["blocked_reasons"] == ["SCOPE_RESTRICTED"]
        assert service.read("alice", cid)["expansion_level"] == 0


async def test_additive_investigation_contract_over_mcp_http(tmp_path):
    with running_server(tmp_path) as (url, _):
        async with streamable_http_client(url + "/mcp") as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()

                async def call(name, **arguments):
                    envelope = (await session.call_tool(name, arguments)).structuredContent
                    assert envelope["status"] == "OK", envelope
                    return envelope["data"]

                cid = (
                    await call(
                        "geo_case_create", title="Structured research", idempotency_key="create"
                    )
                )["id"]
                await call(
                    "geo_observation_add",
                    case_id=cid,
                    expected_revision=1,
                    idempotency_key="scene-plan",
                    observation=observation(
                        scene_graph=graph(),
                        research_actions=[
                            action(execution="MCP", required_capability=None, estimated_requests=0)
                        ],
                    ).model_dump(mode="json"),
                )
                result = await call("geo_case_next", case_id=cid)
                assert result["action"] == "RESEARCH"
                assert result["research_plan"]["recommended"][0]["id"] == "differentiate"


def test_old_request_hashes_and_idempotency_are_preserved(runtime):
    service = runtime.service
    cid = service.create("a", "Compatibility", "create")["id"]
    obs = observation()
    result = service.observation("a", cid, 1, "old-observation", obs)
    old_payload = obs.model_dump(
        mode="json", exclude={"scene_graph", "research_actions", "action_outcomes"}
    )
    row = runtime.db.connection.execute(
        "SELECT request_hash FROM idempotency WHERE owner=? AND key=?", ("a", "old-observation")
    ).fetchone()
    assert row[0] == digest([cid, 1, "observation", old_payload])
    assert service.observation("a", cid, 1, "old-observation", obs) == result
    candidate = CandidateInput(
        label="Existing contract", level="AREA", area=Bounds(south=0, west=0, north=1, east=1)
    )
    result = service.candidates("a", cid, 2, "old-candidate", [candidate])
    row = runtime.db.connection.execute(
        "SELECT request_hash FROM idempotency WHERE owner=? AND key=?", ("a", "old-candidate")
    ).fetchone()
    assert row[0] == digest(
        [cid, 2, "candidates", [candidate.model_dump(exclude={"observation_ids"})]]
    )
    assert service.candidates("a", cid, 2, "old-candidate", [candidate]) == result


def test_geoprior_preserves_uncertainty_and_cannot_be_geometry(runtime):
    service = runtime.service
    cid = service.create("a", "GeoPrior", "create")["id"]
    prior = service.observation(
        "a", cid, 1, "prior", observation(observation_type="MODEL_GEO_PRIOR", scene_graph=graph())
    )["result"]
    candidates = service.candidates(
        "a",
        cid,
        2,
        "candidates",
        [
            CandidateInput(
                label="Wide prior, not a camera position",
                level="REGION",
                area=Bounds(south=0, west=0, north=10, east=10),
                observation_ids=[prior["id"]],
            )
        ],
    )["result"]
    result = service.finalize("a", cid, 3, "final", "REGION", "HYPOTHESIS", candidates[0]["id"])[
        "result"
    ]
    assert result["scene_verified"] is False and result["radius_m"] is None
    assert result["point"] is None and result["location_status"] == "HYPOTHESIS"
    assert result["best_candidate"]["model_geo_prior_ids"] == [prior["id"]]
    assert service.read("a", cid)["state"] == "REVIEW_REQUIRED"
    with pytest.raises(GeoError) as error:
        service.observation(
            "a",
            cid,
            4,
            "false-geometry",
            observation(
                observation_type="MODEL_GEO_PRIOR",
                spatial_constraint={"relation": "DISTANCE", "expected": 100, "tolerance": 10},
            ),
        )
    assert error.value.code == "INVALID_INPUT"
