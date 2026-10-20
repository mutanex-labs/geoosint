"""Ограниченные структуры исследования; оценки агента не являются вероятностями."""

from typing import Any, Literal

from pydantic import Field, model_validator

from geoosint.core.models import Bounds, StrictModel

Capability = Literal[
    "WEB_SEARCH_AVAILABLE",
    "WEB_PAGES_AVAILABLE",
    "IMAGE_SEARCH_AVAILABLE",
    "BROWSER_AVAILABLE",
    "MAP_VIEW_AVAILABLE",
    "REVERSE_IMAGE_SEARCH_AVAILABLE",
]


class ImageRegion(StrictModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)

    @model_validator(mode="after")
    def within_image(self) -> "ImageRegion":
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Фрагмент выходит за изображение")
        return self


class SceneNode(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1000)
    observation_type: str = Field(min_length=1, max_length=100)
    region: ImageRegion | None = None
    recognition: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"
    geographic_information: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"] = "UNKNOWN"
    prevalence: str = Field(default="UNKNOWN", max_length=300)
    temporal_stability: str = Field(default="UNKNOWN", max_length=300)
    external_check: str | None = Field(default=None, max_length=500)
    alternatives: list[str] = Field(default_factory=list, max_length=8)
    depends_on: list[str] = Field(default_factory=list, max_length=16)


class SceneRelation(StrictModel):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    relation: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=500)


class SceneGraph(StrictModel):
    artifact_id: str | None = Field(default=None, min_length=1, max_length=64)
    client_image_ref: str | None = Field(default=None, min_length=1, max_length=500)
    passes_completed: list[str] = Field(min_length=1, max_length=6)
    nodes: list[SceneNode] = Field(min_length=1, max_length=32)
    relations: list[SceneRelation] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def consistent(self) -> "SceneGraph":
        if not self.artifact_id and not self.client_image_ref:
            raise ValueError("Нужен исходный артефакт либо ссылка клиента на изображение")
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("Идентификаторы узлов должны быть уникальны")
        dependencies = {node.id: node.depends_on for node in self.nodes}
        if any(set(values) - ids for values in dependencies.values()):
            raise ValueError("Неизвестная зависимость наблюдения")
        done: set[str] = set()

        def visit(identifier: str, active: set[str]) -> None:
            if identifier in active:
                raise ValueError("Циклическая зависимость наблюдений")
            if identifier not in done:
                for dependency in dependencies[identifier]:
                    visit(dependency, active | {identifier})
                done.add(identifier)

        for identifier in ids:
            visit(identifier, set())
        if any(edge.source not in ids or edge.target not in ids for edge in self.relations):
            raise ValueError("Связь с неизвестным узлом")
        if any(len(value) > 100 for value in self.passes_completed):
            raise ValueError("Название прохода слишком длинное")
        if any(len(value) > 500 for node in self.nodes for value in node.alternatives):
            raise ValueError("Альтернативная интерпретация слишком длинная")
        return self


class ResearchAction(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=1000)
    strategy: str = Field(min_length=1, max_length=100)
    candidate_ids: list[str] = Field(default_factory=list, max_length=10)
    observation_ids: list[str] = Field(default_factory=list, max_length=20)
    required_capability: Capability | None = None
    execution: Literal["CLIENT", "MCP"]
    mcp_tool: (
        Literal[
            "geo_geometry",
            "geo_spatial_check",
            "geo_osm_features",
            "geo_sun_position",
        ]
        | None
    ) = None
    area: Bounds | None = None
    expected_information: int = Field(ge=0, le=3)
    discrimination: int = Field(ge=0, le=3)
    source_reliability: int = Field(ge=0, le=3)
    independence: int = Field(ge=0, le=3)
    uninformative_risk: int = Field(ge=0, le=3)
    estimated_requests: int = Field(ge=0, le=100)
    estimated_seconds: int = Field(ge=0, le=3600)

    @model_validator(mode="after")
    def execution_channel(self) -> "ResearchAction":
        if (self.execution == "MCP") != (self.mcp_tool is not None):
            raise ValueError("Для MCP требуется реальный mcp_tool; клиент не исполняет MCP")
        return self


class ActionOutcome(StrictModel):
    action_id: str = Field(min_length=1, max_length=64)
    status: Literal["COMPLETED", "UNAVAILABLE", "NO_USEFUL_RESULTS", "CANCELLED"]
    explanation: str = Field(min_length=1, max_length=1000)
    operation_ids: list[str] = Field(default_factory=list, max_length=10)


def research_state(doc: dict[str, Any]) -> dict[str, Any]:
    actions: dict[str, Any] = {}
    outcomes: dict[str, Any] = {}
    capabilities: dict[str, Any] = {}
    for observation in doc["observations"]:
        for action in observation.get("research_actions", []):
            actions[action["id"]] = {**action, "proposed_by": observation["id"]}
        for outcome in observation.get("action_outcomes", []):
            outcomes[outcome["action_id"]] = {**outcome, "recorded_by": observation["id"]}
        research = observation.get("research")
        if research and research.get("capabilities"):
            capabilities.update(research["capabilities"])
    return {"actions": actions, "outcomes": outcomes, "capabilities": capabilities}


def prioritize(
    doc: dict[str, Any],
    remaining: dict[str, Any],
    allowed_ids: set[str],
    allowed_areas: set[str],
    osm_enabled: bool = True,
) -> dict[str, Any]:
    state = research_state(doc)
    ranked: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for identifier, action in state["actions"].items():
        if identifier in state["outcomes"]:
            continue
        reasons = []
        if action.get("mcp_tool") == "geo_osm_features" and not osm_enabled:
            reasons.append("PROVIDER_DISABLED")
        if action.get("mcp_tool") == "geo_osm_features" and remaining["provider_requests"] <= 0:
            reasons.append("NETWORK_BUDGET")
        if (
            doc["scope_mode"] == "PREFERRED"
            and not doc["expansion_level"]
            and not (action["candidate_ids"] or action.get("area"))
        ):
            reasons.append("SCOPE_NOT_SPECIFIED")
        if set(action["candidate_ids"]) - allowed_ids or identifier not in allowed_areas:
            reasons.append("SCOPE_RESTRICTED")
        capability = action["required_capability"]
        if capability and state["capabilities"].get(capability) is not True:
            reasons.append("CAPABILITY_NOT_CONFIRMED")
        if action["execution"] == "CLIENT" and not capability:
            reasons.append("CAPABILITY_NOT_CONFIRMED")
        if action["estimated_seconds"] > remaining["duration_seconds"]:
            reasons.append("DURATION_BUDGET")
        if remaining["tool_calls"] <= 0:
            reasons.append("TOOL_BUDGET")
        if action["execution"] == "MCP" and action["estimated_requests"] > min(
            remaining["external_requests"], remaining["provider_requests"]
        ):
            reasons.append("NETWORK_BUDGET")
        # Порядковая эвристика: оценки клиента не калиброваны и не доказывают гипотезу.
        score = (
            3 * action["discrimination"]
            + 2 * action["expected_information"]
            + action["source_reliability"]
            + action["independence"]
            - 2 * action["uninformative_risk"]
            - action["estimated_requests"]
            - action["estimated_seconds"] / 60
        )
        item = {**action, "priority_score": round(score, 3), "blocked_reasons": reasons}
        (blocked if reasons else ranked).append(item)
    ranked.sort(key=lambda item: (-item["priority_score"], item["id"]))
    return {
        "recommended": ranked[:3],
        "blocked": blocked,
        "completed_action_ids": list(state["outcomes"]),
        "estimates_provenance": "CLIENT_REPORTED_HEURISTIC_NOT_PROBABILITY",
        "automatically_executed": False,
    }


def evidence_graph(doc: dict[str, Any]) -> dict[str, Any]:
    """Проекция сохранённого журнала; не отдельное изменяемое хранилище истины."""
    nodes: dict[str, Any] = {}
    edges: list[dict[str, str]] = []

    def node(identifier: str, kind: str, **attributes: Any) -> str:
        nodes[identifier] = {"id": identifier, "kind": kind, **attributes}
        return identifier

    def edge(source: str, target: str, relation: str) -> None:
        edges.append({"source": source, "target": target, "relation": relation})

    for candidate in doc["candidates"]:
        node(candidate["id"], "CANDIDATE")
        for observation_id in candidate.get("observation_ids", []):
            edge(observation_id, candidate["id"], "MOTIVATES_HYPOTHESIS")
        if candidate.get("parent_id"):
            edge(candidate["parent_id"], candidate["id"], "REFINED_INTO")
    for observation in doc["observations"]:
        oid = node(observation["id"], "OBSERVATION", verification="CLIENT_REPORTED")
        scene = observation.get("scene_graph")
        if scene:
            image = node("image:" + (scene["artifact_id"] or scene["client_image_ref"]), "IMAGE")
            edge(image, oid, "OBSERVED_FROM")
            for clue in scene["nodes"]:
                nid = node(oid + ":" + clue["id"], "SCENE_CLUE")
                edge(oid, nid, "CONTAINS")
                for dependency in clue["depends_on"]:
                    edge(oid + ":" + dependency, nid, "DEPENDS_ON")
            for relation in scene["relations"]:
                edge(
                    oid + ":" + relation["source"],
                    oid + ":" + relation["target"],
                    relation["relation"],
                )
        research = observation.get("research")
        if research:
            for index, source in enumerate(research["sources"]):
                sid = node(f"{oid}:source:{index}", "SOURCE", **source)
                edge(oid, sid, research["kind"])
        for action in observation.get("research_actions", []):
            aid = node("action:" + action["id"], "RESEARCH_ACTION")
            edge(oid, aid, "PROPOSED")
            for observation_id in action["observation_ids"]:
                edge(observation_id, aid, "MOTIVATES")
            for candidate_id in action["candidate_ids"]:
                edge(aid, candidate_id, "INVESTIGATES")
        for outcome in observation.get("action_outcomes", []):
            edge("action:" + outcome["action_id"], oid, "OUTCOME_RECORDED")
            for operation_id in outcome["operation_ids"]:
                edge(operation_id, oid, "RESULT_OF")
    for operation in doc["operations"]:
        node(operation["id"], "OPERATION", operation_kind=operation["kind"])
    for evidence in doc["evidence"]:
        eid = node(evidence["id"], "EVIDENCE")
        if evidence.get("operation_id"):
            edge(evidence["operation_id"], eid, "PRODUCED")
        if evidence.get("artifact_id"):
            image = node("image:" + evidence["artifact_id"], "IMAGE")
            edge(image, eid, "METADATA_FROM")
        if evidence.get("candidate_id"):
            edge(eid, evidence["candidate_id"], evidence.get("effect", "CONTEXT"))
        for observation_id in evidence.get("observation_ids", []):
            edge(observation_id, eid, "INTERPRETED_USING")
    return {"nodes": list(nodes.values()), "edges": edges, "scene_verified": False}


def hypothesis_ledger(doc: dict[str, Any]) -> list[dict[str, Any]]:
    actions = research_state(doc)["actions"]
    return [
        {
            "candidate_id": candidate["id"],
            "level": candidate["level"],
            "parent_id": candidate.get("parent_id"),
            "children": [
                c["id"] for c in doc["candidates"] if c.get("parent_id") == candidate["id"]
            ],
            "observation_ids": candidate.get("observation_ids", []),
            "supporting_evidence_ids": [
                e["id"]
                for e in doc["evidence"]
                if e.get("candidate_id") == candidate["id"] and e.get("effect") == "SUPPORTS"
            ],
            "contradicting_evidence_ids": [
                e["id"]
                for e in doc["evidence"]
                if e.get("candidate_id") == candidate["id"] and e.get("effect") == "CONTRADICTS"
            ],
            "research_action_ids": [
                identifier
                for identifier, action in actions.items()
                if candidate["id"] in action["candidate_ids"]
            ],
            "priority_history": [
                h for h in doc["ranking_history"] if h["candidate_id"] == candidate["id"]
            ],
            "status": candidate["status"],
            "coverage_status": "SEE_SCOPE_EVENTS_NOT_INFERRED_FROM_RANK",
        }
        for candidate in doc["candidates"]
    ]
