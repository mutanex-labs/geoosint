import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field

from geoosint.config import Settings
from geoosint.core.geometry import geometry
from geoosint.core.investigation import (
    ActionOutcome,
    ResearchAction,
    SceneGraph,
    evidence_graph,
    hypothesis_ledger,
    prioritize,
    research_state,
)
from geoosint.core.models import Bounds, CandidateInput, GeometryInput, Point, StrictModel
from geoosint.core.research import ResearchRecord
from geoosint.core.scope import (
    BoundaryRegistry,
    SearchScope,
    bounds_allowed,
    membership,
    scope_fields,
)
from geoosint.core.spatial import FeatureRef, SpatialConstraint, compare_features, resolve_feature
from geoosint.core.sun import sun_position
from geoosint.errors import GeoError
from geoosint.storage.database import Database, canonical, digest, now

STATES = {
    "CREATED": {"COLLECTING", "UNRESOLVED", "ARCHIVED"},
    "COLLECTING": {"INVESTIGATING", "UNRESOLVED", "ARCHIVED"},
    "INVESTIGATING": {"REVIEW_REQUIRED", "UNRESOLVED", "ARCHIVED"},
    "REVIEW_REQUIRED": {"INVESTIGATING", "UNRESOLVED", "ARCHIVED"},
    "UNRESOLVED": {"INVESTIGATING", "ARCHIVED"},
    "RESOLVED": {"REVIEW_REQUIRED", "ARCHIVED"},
    "ARCHIVED": set(),
}


class ObservationInput(StrictModel):
    category: Literal["VISUAL", "TEXT", "TERRAIN", "INFRASTRUCTURE", "OTHER"]
    text: str = Field(min_length=1, max_length=4000)
    source: str = Field(min_length=1, max_length=300)
    fragment_id: str | None = Field(default=None, max_length=128)
    dependency_group: str = Field(min_length=1, max_length=128)
    conflicts_with_artifact_ids: list[str] = Field(default_factory=list, max_length=10)
    observation_type: Literal["MODEL_OBSERVATION", "MODEL_VISUAL_COMPARISON", "MODEL_GEO_PRIOR"] = (
        "MODEL_OBSERVATION"
    )
    compared_image_refs: list[str] = Field(default_factory=list, max_length=10)
    spatial_constraint: SpatialConstraint | None = None
    research: ResearchRecord | None = None
    scene_graph: SceneGraph | None = None
    research_actions: list[ResearchAction] = Field(default_factory=list, max_length=12)
    action_outcomes: list[ActionOutcome] = Field(default_factory=list, max_length=12)

    def request_payload(self) -> dict:
        payload = self.model_dump(mode="json")
        # Старые запросы должны повторяться с прежним idempotency hash после обновления.
        for name in ("scene_graph", "research_actions", "action_outcomes"):
            if not payload[name]:
                del payload[name]
        return payload


class EvidenceInput(StrictModel):
    candidate_id: str = Field(min_length=1, max_length=64)
    operation_id: str = Field(min_length=1, max_length=64)
    effect: Literal["SUPPORTS", "CONTRADICTS", "CONTEXT"] = "CONTEXT"
    description: str = Field(min_length=1, max_length=2000)
    observation_ids: list[str] = Field(default_factory=list, max_length=50)


class Service:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db, self.settings = db, settings
        self.registry = BoundaryRegistry(settings.boundaries_file)

    def read(self, owner: str, case_id: str) -> dict:
        document = self.db.read(owner, case_id)
        if "search_scope" not in document:
            document.update(scope_fields())
            document["search_started_at"] = document["created_at"]
        return document

    def budget_status(self, owner: str, case_id: str) -> dict:
        doc = self.read(owner, case_id)
        with self.db.lock:
            row = self.db.connection.execute(
                "SELECT * FROM search_usage WHERE case_id=? AND owner=?", (case_id, owner)
            ).fetchone()
        used = {
            name: row[name] if row else 0
            for name in ("tool_calls", "provider_requests", "external_requests")
        }
        used["duration_seconds"] = max(
            0,
            (datetime.now(UTC) - datetime.fromisoformat(doc["search_started_at"])).total_seconds(),
        )
        used["candidates"] = len(doc["candidates"])
        used["expansion_levels"] = doc["expansion_level"]
        remaining = {
            key: max(0, doc["budget"]["max_" + key] - value) for key, value in used.items()
        }
        return {
            "budget_used": used,
            "budget_remaining": remaining,
            "scope_checked": doc["scope_checked"],
            "expansion_reasons": doc["expansion_history"],
        }

    def _consume(
        self, cx, owner: str, doc: dict, tools: int = 0, providers: int = 0, external: int = 0
    ) -> None:
        cx.execute(
            "INSERT OR IGNORE INTO search_usage(case_id,owner) VALUES(?,?)", (doc["id"], owner)
        )
        row = cx.execute(
            "SELECT * FROM search_usage WHERE case_id=? AND owner=?", (doc["id"], owner)
        ).fetchone()
        for name, added in (
            ("tool_calls", tools),
            ("provider_requests", providers),
            ("external_requests", external),
        ):
            if row[name] + added > doc["budget"]["max_" + name]:
                raise GeoError("BUDGET_EXHAUSTED", "Исчерпан бюджет " + name)
        elapsed = (
            datetime.now(UTC) - datetime.fromisoformat(doc["search_started_at"])
        ).total_seconds()
        if elapsed > doc["budget"]["max_duration_seconds"]:
            raise GeoError("BUDGET_EXHAUSTED", "Исчерпано время расследования")
        cx.execute(
            "UPDATE search_usage SET tool_calls=tool_calls+?,provider_requests=provider_requests+?,"
            "external_requests=external_requests+? WHERE case_id=? AND owner=?",
            (tools, providers, external, doc["id"], owner),
        )

    def consume_external(self, owner: str, case_id: str) -> None:
        with self.db.transaction() as cx:
            self._consume(cx, owner, self.read(owner, case_id), external=1)

    def record_read(self, owner: str, case_id: str) -> None:
        with self.db.transaction() as cx:
            self.read(owner, case_id)
            self._record_read(cx, owner, case_id)

    @staticmethod
    def _record_read(cx, owner: str, case_id: str) -> None:
        cx.execute(
            "INSERT OR IGNORE INTO search_usage(case_id,owner) VALUES(?,?)", (case_id, owner)
        )
        # Чтение состояния/экспорт/удаление остаются доступны после исчерпания
        # поиска; их вызовы учитываются, но не разрешают новую поисковую работу.
        cx.execute(
            "UPDATE search_usage SET tool_calls=tool_calls+1 WHERE case_id=? AND owner=?",
            (case_id, owner),
        )

    def summary(self, owner: str, case_id: str) -> dict:
        self.record_read(owner, case_id)
        document = self.read(owner, case_id)
        return {k: len(v) if isinstance(v, list) else v for k, v in document.items()}

    def trace_record(self, owner: str, case_id: str, event: dict) -> str:
        with self.db.transaction() as cx:
            self.read(owner, case_id)
            count = cx.execute(
                "SELECT count(*) FROM request_traces WHERE case_id=? AND owner=?", (case_id, owner)
            ).fetchone()[0]
            if count >= 10000:
                return "TRACE_LIMIT_REACHED"
            identifier = str(uuid4())
            cx.execute(
                "INSERT INTO request_traces VALUES(?,?,?,?,?)",
                (identifier, case_id, owner, now(), canonical(event)),
            )
            return identifier

    def traces(self, owner: str, case_id: str) -> list[dict]:
        self.read(owner, case_id)
        with self.db.lock:
            rows = self.db.connection.execute(
                "SELECT content FROM request_traces WHERE case_id=? AND owner=? ORDER BY created_at,id",
                (case_id, owner),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def coverage_events(self, owner: str, case_id: str) -> list[dict]:
        self.read(owner, case_id)
        with self.db.lock:
            rows = self.db.connection.execute(
                "SELECT content,scope_revision FROM scope_events WHERE case_id=? AND owner=? ORDER BY created_at,id",
                (case_id, owner),
            ).fetchall()
        return [{**json.loads(row[0]), "scope_revision": row[1]} for row in rows]

    def provider_failure(self, owner: str, case_id: str, code: str, parameters: dict) -> None:
        with self.db.transaction():
            document = self.read(owner, case_id)
            self._scope_event(
                owner,
                document,
                {
                    "reason": "CHECK_NOT_COMPLETED",
                    "created_at": now(),
                    "error_code": code,
                    "parameters": parameters,
                    "operation_ids": [],
                    "coverage_status": (
                        "PROVIDER_UNAVAILABLE"
                        if code in ("UPSTREAM_UNAVAILABLE", "UPSTREAM_TIMEOUT", "RATE_LIMITED")
                        else "COVERAGE_INCOMPLETE"
                    ),
                    "negative_evidence": False,
                },
            )

    def coverage_status(self, owner: str, case_id: str) -> str:
        document = self.read(owner, case_id)
        events = self.coverage_events(owner, case_id)
        failures = [
            event
            for event in events
            if event.get("reason") == "CHECK_NOT_COMPLETED"
            and event["scope_revision"] == document["scope_revision"]
        ]
        unresolved = []
        for failure in failures:
            parameters = failure["parameters"]
            successes = [
                operation
                for operation in document["operations"]
                if operation["kind"] == "osm"
                and all(
                    operation["parameters"].get(key) == parameters.get(key)
                    for key in ("bounds", "category")
                )
            ]
            last_success = max(
                (operation["result"].get("retrieved_at") or "" for operation in successes),
                default="",
            )
            if failure["created_at"] > last_success:
                unresolved.append(failure["coverage_status"])
        if unresolved:
            return (
                "PROVIDER_UNAVAILABLE"
                if "PROVIDER_UNAVAILABLE" in unresolved
                else "COVERAGE_INCOMPLETE"
            )
        return document["coverage_status"]

    def scope_update(
        self, owner: str, case_id: str, revision: int, key: str, scope: SearchScope
    ) -> dict:
        fields = scope_fields(scope, self.registry)

        def change(doc):
            previous_revision = doc["scope_revision"]
            initial_scope = doc.get("initial_search_scope", doc["search_scope"])
            previous_scope = doc["search_scope"]
            previous_history = doc["expansion_history"]
            previous_start = doc["search_started_at"]
            doc.update(fields)
            doc["initial_search_scope"] = initial_scope
            doc["scope_revision"] = previous_revision + 1
            doc["search_started_at"] = previous_start
            doc["expansion_history"] = previous_history
            for candidate in doc["candidates"]:
                candidate["status"] = "NEEDS_REVIEW"
            event = {
                "reason": "USER_SCOPE_CHANGED",
                "previous_scope": previous_scope,
                "new_scope": doc["search_scope"],
                "created_at": now(),
                "scope_revision": doc["scope_revision"],
                "operation_ids": [],
            }
            self._scope_event(owner, doc, event)
            return {k: doc[k] for k in fields}

        return self.mutate(
            owner, case_id, revision, key, "scope_update", scope.model_dump(mode="json"), change
        )

    def _scope_event(self, owner: str, doc: dict, event: dict) -> None:
        self.db.connection.execute(
            "INSERT INTO scope_events VALUES(?,?,?,?,?,?)",
            (str(uuid4()), doc["id"], owner, now(), doc["scope_revision"], canonical(event)),
        )

    def expand(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        reason: str,
        operation_ids: list[str],
        user_authorized: bool = False,
        charge_tool: bool = True,
    ) -> dict:
        def change(doc):
            if doc["scope_mode"] != "PREFERRED":
                raise GeoError("SCOPE_VIOLATION", "Расширение разрешено только для PREFERRED")
            if doc["state"] in ("RESOLVED", "UNRESOLVED", "ARCHIVED"):
                raise GeoError("SCOPE_VIOLATION", "Завершённое дело не расширяется")
            if doc["expansion_level"] >= doc["budget"]["max_expansion_levels"]:
                raise GeoError("BUDGET_EXHAUSTED", "Исчерпан бюджет расширений")
            ops = {o["id"]: o for o in doc["operations"]}
            if any(i not in ops for i in operation_ids):
                raise GeoError("EVIDENCE_INSUFFICIENT", "Не найдены проверки расширения")
            if not user_authorized:
                # Ни отсутствие OSM, ни повествование модели не доказывают противоречия.
                if reason != "NO_VERIFIED_MATCH" or not operation_ids or not doc["candidates"]:
                    raise GeoError(
                        "EVIDENCE_INSUFFICIENT", "Нет основания для автоматического расширения"
                    )
                if not any(ops[i]["kind"] == "geometry" for i in operation_ids):
                    raise GeoError(
                        "EVIDENCE_INSUFFICIENT", "Нужны локальные вычислительные проверки"
                    )
                if not any(self._candidate_scope(doc, c) == "OUTSIDE" for c in doc["candidates"]):
                    raise GeoError(
                        "EVIDENCE_INSUFFICIENT", "Нет альтернативы с информационной полезностью"
                    )
            self._consume(self.db.connection, owner, doc, tools=1 if charge_tool else 0)
            doc["expansion_level"] += 1
            for candidate in doc["candidates"]:
                if self._candidate_scope(doc, candidate) == "INSIDE":
                    candidate["status"] = "NEEDS_REVIEW"
            event = {
                "reason": "USER_AUTHORIZED" if user_authorized else reason,
                "created_at": now(),
                "operation_ids": operation_ids,
                "level": doc["expansion_level"],
                "coverage": "INSUFFICIENT_EVIDENCE",
                "prior_is_evidence": False,
            }
            doc["expansion_history"].append(event)
            self._scope_event(owner, doc, event)
            return event

        return self.mutate(
            owner,
            case_id,
            revision,
            key,
            "scope_expand",
            [reason, operation_ids, user_authorized],
            change,
        )

    def _candidate_scope(self, doc: dict, candidate: dict) -> str:
        point = candidate.get("point")
        if point:
            return membership(doc, Point.model_validate(point))
        if not doc["scope_geometry"] and not doc["excluded_areas"]:
            return "INSIDE"
        bounds = Bounds.model_validate(candidate["area"])
        if bounds_allowed(doc, bounds):
            return "INSIDE"
        center = Point(
            latitude=(bounds.south + bounds.north) / 2,
            longitude=(bounds.west + (bounds.east - bounds.west) % 360 / 2 + 180) % 360 - 180,
        )
        result = membership(doc, center)
        return "OVERLAP" if result == "INSIDE" else result

    def create(
        self,
        owner: str,
        title: str,
        idempotency_key: str,
        privacy_sensitive: bool = False,
        search_scope: SearchScope | None = None,
    ) -> dict:
        if not title.strip() or len(title) > 300 or not 1 <= len(idempotency_key) <= 128:
            raise GeoError("INVALID_INPUT", "Нужны название до 300 знаков и ключ до 128 знаков")
        payload = ["create", title, privacy_sensitive]
        if search_scope is not None:
            payload.append(search_scope.model_dump(mode="json"))
        request_hash = digest(payload)
        with self.db.transaction() as cx:
            cached = self._replay(cx, owner, idempotency_key, request_hash)
            if cached is not None:
                return cached
            count = cx.execute("SELECT count(*) FROM cases WHERE owner=?", (owner,)).fetchone()[0]
            if count >= self.settings.max_cases:
                raise GeoError("RATE_LIMITED", "Достигнут лимит расследований")
            cx.execute("INSERT OR IGNORE INTO users VALUES(?)", (owner,))
            document = {
                "id": str(uuid4()),
                "title": title,
                "created_at": now(),
                "revision": 1,
                "state": "CREATED",
                "privacy_sensitive": privacy_sensitive,
                "image_received": False,
                "observations": [],
                "candidates": [],
                "evidence": [],
                "operations": [],
                "ranking_history": [],
                "result": None,
                **scope_fields(search_scope, self.registry),
            }
            cx.execute(
                "INSERT INTO cases(id,owner,revision,state,created_at,document) VALUES(?,?,?,?,?,?)",
                (document["id"], owner, 1, "CREATED", document["created_at"], canonical(document)),
            )
            self._consume(cx, owner, document, tools=1)
            self.db.append_event(cx, owner, document, "create")
            self._remember(cx, owner, idempotency_key, request_hash, document["id"], document)
            return document

    def _replay(self, cx, owner: str, key: str, request_hash: str) -> dict | None:
        if not 1 <= len(key) <= 128:
            raise GeoError("INVALID_INPUT", "Некорректный idempotency key")
        row = cx.execute(
            "SELECT request_hash,response FROM idempotency WHERE owner=? AND key=?", (owner, key)
        ).fetchone()
        if row:
            if row[0] != request_hash:
                raise GeoError("REVISION_CONFLICT", "Ключ уже использован для другого запроса")
            return json.loads(row[1])
        return None

    def _remember(
        self, cx, owner: str, key: str, request_hash: str, case_id: str, response: dict
    ) -> None:
        cx.execute(
            "INSERT INTO idempotency VALUES(?,?,?,?,?)",
            (owner, key, request_hash, case_id, canonical(response)),
        )

    def preflight(
        self, owner: str, case_id: str, revision: int, key: str, operation: str, payload: Any
    ) -> dict | None:
        with self.db.transaction() as cx:
            document = self.read(owner, case_id)
            replay = self._replay(cx, owner, key, digest([case_id, revision, operation, payload]))
            if replay is not None:
                return replay
            if document["revision"] != revision:
                raise GeoError("REVISION_CONFLICT", "Ревизия устарела")
            if document["state"] == "ARCHIVED":
                raise GeoError("INVALID_INPUT", "Архивное расследование")
            self._quota(document, "operations", self.settings.max_operations)
            if operation == "osm":
                bounds = Bounds.model_validate(payload["bounds"])
                constrained = document["scope_mode"] == "STRICT" or (
                    document["scope_mode"] == "PREFERRED" and document["expansion_level"] == 0
                )
                scope = (
                    document
                    if constrained
                    else {**document, "scope_mode": "AUTO", "scope_geometry": []}
                )
                if (constrained or document["excluded_areas"]) and not bounds_allowed(
                    scope, bounds
                ):
                    raise GeoError(
                        "SCOPE_VIOLATION", "Запрос выходит за проверенные границы поиска"
                    )
                status = self.budget_status(owner, case_id)
                if (
                    document["scope_mode"] == "PREFERRED"
                    and document["expansion_level"] == 0
                    and (
                        status["budget_used"]["tool_calls"]
                        >= document["budget"]["max_tool_calls"]
                        * document["budget"]["preferred_scope_budget_fraction"]
                    )
                ):
                    raise GeoError(
                        "BUDGET_RESERVED", "Резерв сохранён для альтернатив и точного уточнения"
                    )
                self._consume(cx, owner, document, tools=1, providers=1)
            return None

    def mutate(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        operation: str,
        payload: Any,
        change: Callable[[dict], Any],
    ) -> dict:
        request_hash = digest([case_id, revision, operation, payload])
        with self.db.transaction() as cx:
            document = self.read(owner, case_id)
            replay = self._replay(cx, owner, key, request_hash)
            if replay is not None:
                return replay
            if document["revision"] != revision:
                raise GeoError("REVISION_CONFLICT", "Ревизия устарела; перечитайте расследование")
            if document["state"] == "ARCHIVED" and operation != "delete":
                raise GeoError("INVALID_INPUT", "Архивное расследование неизменяемо")
            if operation not in (
                "osm",
                "scope_expand",
                "scope_update",
                "finalize",
                "transition",
                "artifact_delete",
            ):
                self._consume(cx, owner, document, tools=1)
            data = change(document)
            if operation in ("scope_update", "finalize", "transition", "artifact_delete"):
                self._record_read(cx, owner, case_id)
            document["revision"] += 1
            cx.execute(
                "UPDATE cases SET revision=?,state=?,document=?,scope_revision=? WHERE id=? AND owner=?",
                (
                    document["revision"],
                    document["state"],
                    canonical(document),
                    document["scope_revision"],
                    case_id,
                    owner,
                ),
            )
            self.db.append_event(cx, owner, document, operation)
            response = {"case_id": case_id, "revision": document["revision"], "result": data}
            self._remember(cx, owner, key, request_hash, case_id, response)
            return response

    def observation(
        self, owner: str, case_id: str, revision: int, key: str, observation: ObservationInput
    ) -> dict:
        def change(doc):
            if (
                observation.observation_type == "MODEL_VISUAL_COMPARISON"
                and len(observation.compared_image_refs) < 2
            ):
                raise GeoError(
                    "INVALID_INPUT",
                    "Сравнение требует ссылок на оба фактически доступных клиенту изображения",
                )
            self._quota(doc, "observations", self.settings.max_observations)
            self._validate_investigation(owner, doc, observation)
            for artifact_id in observation.conflicts_with_artifact_ids:
                if not self.db.connection.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND case_id=? AND owner=?",
                    (artifact_id, case_id, owner),
                ).fetchone():
                    raise GeoError("NOT_FOUND", "Артефакт противоречия не найден")
            item = {
                "id": str(uuid4()),
                **observation.model_dump(mode="json"),
                "created_at": now(),
                "type": observation.observation_type,
                "verification": "UNVERIFIED",
            }
            doc["observations"].append(item)
            if doc["state"] == "CREATED":
                doc["state"] = "COLLECTING"
            if observation.conflicts_with_artifact_ids:
                for evidence in doc["evidence"]:
                    if (
                        evidence.get("artifact_id") in observation.conflicts_with_artifact_ids
                        and evidence.get("type") == "EMBEDDED_METADATA"
                    ):
                        evidence["review_status"] = "NEEDS_REVIEW"
                        evidence.setdefault("contradictory_observation_ids", []).append(item["id"])
                doc["state"] = "REVIEW_REQUIRED"
            return item

        return self.mutate(
            owner,
            case_id,
            revision,
            key,
            "observation",
            observation.request_payload(),
            change,
        )

    def _validate_investigation(self, owner: str, doc: dict, observation: ObservationInput) -> None:
        if observation.observation_type == "MODEL_GEO_PRIOR" and observation.spatial_constraint:
            raise GeoError(
                "INVALID_INPUT", "GeoPrior не является наблюдаемым геометрическим ограничением"
            )
        scene = observation.scene_graph
        if (
            scene
            and scene.artifact_id
            and not self.db.connection.execute(
                "SELECT 1 FROM artifacts WHERE id=? AND case_id=? AND owner=?",
                (scene.artifact_id, doc["id"], owner),
            ).fetchone()
        ):
            raise GeoError("NOT_FOUND", "Исходный артефакт не найден")
        state = research_state(doc)
        candidate_ids = {c["id"] for c in doc["candidates"]}
        observation_ids = {o["id"] for o in doc["observations"]}
        operation_ids = {o["id"] for o in doc["operations"]}
        new_ids = [action.id for action in observation.research_actions]
        if len(new_ids) != len(set(new_ids)) or set(new_ids) & state["actions"].keys():
            raise GeoError("INVALID_INPUT", "Идентификатор проверки уже существует")
        for action in observation.research_actions:
            if (
                set(action.candidate_ids) - candidate_ids
                or set(action.observation_ids) - observation_ids
            ):
                raise GeoError("NOT_FOUND", "Гипотеза или наблюдение проверки не найдены")
            if action.execution == "MCP" and action.required_capability:
                raise GeoError("INVALID_INPUT", "Веб-возможности клиента не являются MCP")
            if doc["scope_mode"] == "STRICT" and action.area is None and not action.candidate_ids:
                raise GeoError("SCOPE_VIOLATION", "Для STRICT укажите территорию или кандидата")
            if (
                action.area is not None
                and doc["scope_mode"] == "STRICT"
                and not bounds_allowed(doc, action.area)
            ):
                raise GeoError("SCOPE_VIOLATION", "Проверка выходит за границы STRICT")
        outcome_ids = [outcome.action_id for outcome in observation.action_outcomes]
        if len(outcome_ids) != len(set(outcome_ids)):
            raise GeoError("INVALID_INPUT", "Повтор результата проверки")
        for outcome in observation.action_outcomes:
            if outcome.action_id not in state["actions"]:
                raise GeoError("NOT_FOUND", "Проверка не найдена")
            if outcome.action_id in state["outcomes"]:
                raise GeoError("INVALID_INPUT", "Результат уже записан; создайте новую проверку")
            if set(outcome.operation_ids) - operation_ids:
                raise GeoError("NOT_FOUND", "Операция результата не найдена")
            if (
                outcome.status == "COMPLETED"
                and state["actions"][outcome.action_id]["execution"] == "MCP"
                and not outcome.operation_ids
            ):
                raise GeoError("INVALID_INPUT", "Завершённая MCP-проверка требует operation_id")

    def research_plan(self, doc: dict, remaining: dict) -> dict:
        constrained = doc["scope_mode"] == "STRICT" or (
            doc["scope_mode"] == "PREFERRED" and not doc["expansion_level"]
        )
        allowed = {
            c["id"]
            for c in doc["candidates"]
            if not constrained or self._candidate_scope(doc, c) == "INSIDE"
        }
        allowed_areas = set()
        for identifier, action in research_state(doc)["actions"].items():
            area = action.get("area")
            if (
                not area
                or not (constrained or doc["excluded_areas"])
                or bounds_allowed(
                    doc if constrained else {**doc, "scope_mode": "AUTO", "scope_geometry": []},
                    Bounds.model_validate(area),
                )
            ):
                allowed_areas.add(identifier)
        return prioritize(doc, remaining, allowed, allowed_areas, self.settings.osm_enabled)

    @staticmethod
    def _quota(doc: dict, field: str, limit: int, added: int = 1) -> None:
        if len(doc[field]) + added > limit:
            raise GeoError("RATE_LIMITED", "Достигнут лимит записей")

    def candidates(
        self, owner: str, case_id: str, revision: int, key: str, candidates: list[CandidateInput]
    ) -> dict:
        if not 1 <= len(candidates) <= 50:
            raise GeoError("INVALID_INPUT", "Один пакет: 1–50 кандидатов")

        def change(doc):
            self._quota(
                doc,
                "candidates",
                min(self.settings.max_candidates, doc["budget"]["max_candidates"]),
                len(candidates),
            )
            existing = {c["id"] for c in doc["candidates"]}
            for candidate in candidates:
                if set(candidate.observation_ids) - {o["id"] for o in doc["observations"]}:
                    raise GeoError("NOT_FOUND", "Наблюдение гипотезы не найдено")
                if candidate.source_operation_id:
                    feature = resolve_feature(
                        doc,
                        FeatureRef(
                            operation_id=candidate.source_operation_id,
                            source_id=candidate.source_feature_id,
                        ),
                    )
                    if candidate.point is None or candidate.point.model_dump() != feature["point"]:
                        raise GeoError(
                            "INVALID_INPUT",
                            "Точка кандидата не соответствует указанному объекту OSM",
                        )
                if candidate.parent_id and candidate.parent_id not in existing:
                    raise GeoError("NOT_FOUND", "Родительский кандидат не найден")
                if doc["scope_mode"] == "STRICT" and not bounds_allowed(doc, candidate.area):
                    raise GeoError(
                        "SCOPE_VIOLATION", "Область кандидата выходит за достоверные границы STRICT"
                    )
                if (
                    candidate.point
                    and doc["excluded_areas"]
                    and membership(
                        {**doc, "scope_mode": "AUTO", "scope_geometry": []}, candidate.point
                    )
                    != "INSIDE"
                ):
                    raise GeoError(
                        "SCOPE_VIOLATION",
                        "Кандидат находится в исключённой области или её границы неизвестны",
                    )
            items = [
                {
                    "id": str(uuid4()),
                    **c.model_dump(),
                    "status": "ACTIVE",
                    "score": 0,
                    "created_at": now(),
                }
                for c in candidates
            ]
            doc["candidates"].extend(items)
            if doc["state"] in ("CREATED", "COLLECTING"):
                doc["state"] = "INVESTIGATING"
            return items

        return self.mutate(
            owner,
            case_id,
            revision,
            key,
            "candidates",
            [
                c.model_dump(exclude={"observation_ids"} if not c.observation_ids else set())
                for c in candidates
            ],
            change,
        )

    def compute(
        self, owner: str, case_id: str, revision: int, key: str, args: GeometryInput
    ) -> dict:
        return self.record_operation(
            owner, case_id, revision, key, "geometry", args.model_dump(mode="json"), geometry(args)
        )

    def sun(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        point: Point,
        captured_at: datetime | None,
    ) -> dict:
        result = sun_position(point, captured_at)
        return self.record_operation(
            owner,
            case_id,
            revision,
            key,
            "sun",
            {
                "point": point.model_dump(),
                "captured_at": captured_at.isoformat() if captured_at else None,
            },
            result,
        )

    def record_operation(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        kind: str,
        parameters: dict,
        result: dict,
    ) -> dict:
        def change(doc):
            self._quota(doc, "operations", self.settings.max_operations)
            item = {
                "id": str(uuid4()),
                "kind": kind,
                "parameters": parameters,
                "result": result,
                "algorithm": result.get("algorithm", "osm-overpass-template-v1"),
                "created_at": now(),
                "content_hash": digest([parameters, result]),
                "dependency_group": "OSM" if kind == "osm" else "computed",
            }
            doc["operations"].append(item)
            if kind == "osm":
                doc["scope_checked"].append(
                    {
                        "operation_id": item["id"],
                        "scope_revision": doc["scope_revision"],
                        "bounds": parameters["bounds"],
                        "retrieved_at": result.get("retrieved_at"),
                        "coverage": "COVERAGE_INCOMPLETE"
                        if result.get("truncated")
                        else "NO_VERIFIED_MATCH",
                        "limitations": "Ограниченная выборка объектов, не исчерпывающая проверка территории",
                    }
                )
                doc["coverage_status"] = (
                    "COVERAGE_INCOMPLETE" if result.get("truncated") else "NO_VERIFIED_MATCH"
                )
            return item

        return self.mutate(owner, case_id, revision, key, kind, parameters, change)

    def evidence(
        self, owner: str, case_id: str, revision: int, key: str, evidence: EvidenceInput
    ) -> dict:
        def change(doc):
            self._quota(doc, "evidence", self.settings.max_evidence)
            if evidence.candidate_id not in {c["id"] for c in doc["candidates"]}:
                raise GeoError("NOT_FOUND", "Кандидат не найден")
            ops = {o["id"]: o for o in doc["operations"]}
            op = ops.get(evidence.operation_id)
            if not op:
                raise GeoError("EVIDENCE_INSUFFICIENT", "Нужна реально сохранённая операция")
            if op["kind"] == "spatial_check":
                if op["parameters"]["observation_id"] not in evidence.observation_ids:
                    raise GeoError(
                        "INVALID_INPUT", "Укажите исходное наблюдение пространственной проверки"
                    )
                expected = (
                    "SUPPORTS"
                    if op["result"]["matches"] is True
                    else "CONTRADICTS"
                    if op["result"]["matches"] is False
                    else "CONTEXT"
                )
                if op["parameters"][
                    "candidate_id"
                ] != evidence.candidate_id or evidence.effect not in ("CONTEXT", expected):
                    raise GeoError(
                        "INVALID_INPUT",
                        "Эффект или кандидат противоречит результату пространственной проверки",
                    )
            obs = {o["id"]: o for o in doc["observations"]}
            if any(i not in obs for i in evidence.observation_ids):
                raise GeoError("NOT_FOUND", "Наблюдение не найдено")
            groups = {obs[i]["dependency_group"] for i in evidence.observation_ids}
            groups.add(op["dependency_group"])
            item = {
                "id": str(uuid4()),
                **evidence.model_dump(),
                "created_at": now(),
                "content_hash": digest(evidence.model_dump()),
                "dependency_groups": sorted(groups),
                "verification": "EXECUTION_VERIFIED",
                "applicability": "UNVERIFIED",
                "source": op["kind"],
                "limitations": ["Вызов подтверждён, соответствие фотографии не подтверждено"],
            }
            doc["evidence"].append(item)
            return item

        return self.mutate(owner, case_id, revision, key, "evidence", evidence.model_dump(), change)

    def spatial_check(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        candidate_id: str,
        observation_id: str,
        first: FeatureRef,
        second: FeatureRef,
    ) -> dict:
        doc = self.read(owner, case_id)
        candidate = next((item for item in doc["candidates"] if item["id"] == candidate_id), None)
        observation = next(
            (item for item in doc["observations"] if item["id"] == observation_id), None
        )
        if candidate is None or observation is None:
            raise GeoError("NOT_FOUND", "Кандидат или наблюдение не найдено")
        if not observation.get("spatial_constraint"):
            raise GeoError(
                "INVALID_INPUT", "У наблюдения нет структурированного пространственного ограничения"
            )
        if not any(
            candidate.get("source_operation_id") == reference.operation_id
            and candidate.get("source_feature_id") == reference.source_id
            for reference in (first, second)
        ):
            raise GeoError("INVALID_INPUT", "Проверка должна включать исходный объект кандидата")
        features = [resolve_feature(doc, reference) for reference in (first, second)]
        if doc["scope_mode"] == "STRICT" and any(
            membership(doc, Point.model_validate(feature["point"])) != "INSIDE"
            for feature in features
        ):
            raise GeoError("SCOPE_VIOLATION", "Точка проверки вне STRICT")
        result = compare_features(
            *features, SpatialConstraint.model_validate(observation["spatial_constraint"])
        )
        parameters = {
            "candidate_id": candidate_id,
            "observation_id": observation_id,
            "first": first.model_dump(),
            "second": second.model_dump(),
        }
        return self.record_operation(
            owner, case_id, revision, key, "spatial_check", parameters, result
        )

    def candidate_assessments(self, doc: dict) -> list[dict]:
        results = []
        operations = {operation["id"]: operation for operation in doc["operations"]}
        observations = {observation["id"]: observation for observation in doc["observations"]}
        for candidate in doc["candidates"]:
            priors = [
                observations[identifier]
                for identifier in candidate.get("observation_ids", [])
                if identifier in observations
                and observations[identifier].get("type") == "MODEL_GEO_PRIOR"
            ]
            checks = [
                evidence
                for evidence in doc["evidence"]
                if evidence.get("candidate_id") == candidate["id"]
                and operations.get(evidence.get("operation_id"), {}).get("kind") == "spatial_check"
            ]
            supported = sorted(
                {item["operation_id"] for item in checks if item["effect"] == "SUPPORTS"}
            )
            contradicted = sorted(
                {item["operation_id"] for item in checks if item["effect"] == "CONTRADICTS"}
            )
            status = (
                "NEEDS_REVIEW"
                if contradicted
                else "FACT_SUPPORTED"
                if supported
                else "PRELIMINARY"
                if candidate.get("source_operation_id")
                else "HYPOTHESIS"
            )
            results.append(
                {
                    **candidate,
                    "location_status": status,
                    "scene_verified": False,
                    "model_geo_prior_ids": [prior["id"] for prior in priors],
                    "geoprior_reasons": [prior["text"] for prior in priors],
                    "supported_operation_ids": supported,
                    "contradicted_operation_ids": contradicted,
                    "scope_membership": self._candidate_scope(doc, candidate),
                    "eligible_as_result": doc["scope_mode"] != "STRICT"
                    or self._candidate_scope(doc, candidate) == "INSIDE",
                    "radius_m": None,
                    "qualitative_confidence": "GEOGRAPHIC_FACTS_ONLY"
                    if status == "FACT_SUPPORTED"
                    else "LOW",
                    "supporting_reasons": [
                        item["description"] for item in checks if item["effect"] == "SUPPORTS"
                    ]
                    or (
                        [
                            "Координаты объекта получены из сохранённой OSM-операции; соответствие снимку ещё не проверено"
                        ]
                        if candidate.get("source_operation_id")
                        else [
                            "Кандидат зарегистрирован как гипотеза; независимого подтверждения места съёмки нет"
                        ]
                    ),
                    "contradicting_reasons": [
                        item["description"] for item in checks if item["effect"] == "CONTRADICTS"
                    ],
                    "verification_gaps": ["INDEPENDENT_SCENE_CORRESPONDENCE", "CAMERA_POSITION"],
                    "limitations": [
                        "Географическая согласованность условна относительно наблюдений клиента; место съёмки не подтверждено"
                    ],
                }
            )
        return sorted(
            results,
            key=lambda item: (
                doc["scope_mode"] == "PREFERRED"
                and not doc["expansion_level"]
                and item["scope_membership"] != "INSIDE",
                bool(item["contradicted_operation_ids"]),
                -len(item["supported_operation_ids"]),
                -item["score"],
                item["id"],
            ),
        )

    def rank(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        candidate_id: str,
        score: float,
        reason: str,
    ) -> dict:
        if not -100 <= score <= 100 or not 1 <= len(reason) <= 1000:
            raise GeoError("INVALID_INPUT", "Рейтинг от -100 до 100; нужно обоснование")

        def change(doc):
            self._quota(doc, "ranking_history", self.settings.max_operations)
            candidate = next((c for c in doc["candidates"] if c["id"] == candidate_id), None)
            if not candidate:
                raise GeoError("NOT_FOUND", "Кандидат не найден")
            doc["ranking_history"].append(
                {
                    "candidate_id": candidate_id,
                    "previous": candidate["score"],
                    "score": score,
                    "reason": reason,
                    "created_at": now(),
                }
            )
            candidate["score"] = score
            candidate["status"] = "DOWNRANKED" if score < 0 else "ACTIVE"
            return candidate

        return self.mutate(
            owner, case_id, revision, key, "rank", [candidate_id, score, reason], change
        )

    def next(self, owner: str, case_id: str) -> dict:
        doc = self.read(owner, case_id)
        budget = self.budget_status(owner, case_id)
        self.record_read(owner, case_id)
        previous_remaining = budget["budget_remaining"]
        budget = self.budget_status(owner, case_id)
        if doc["state"] in ("RESOLVED", "UNRESOLVED", "ARCHIVED"):
            return {"action": "REPORT", "candidate_ids": [], **budget}
        if any(previous_remaining[key] <= 0 for key in ("tool_calls", "duration_seconds")):
            return {"action": "BUDGET_EXHAUSTED", "candidate_ids": [], **budget}
        if doc["scope_mode"] == "STRICT" and any(
            a["boundary_status"] == "BOUNDARY_UNAVAILABLE"
            for a in doc["scope_geometry"] + doc["excluded_areas"]
        ):
            return {
                "action": "RESOLVE_BOUNDARY",
                "candidate_ids": [],
                "coverage_status": "COVERAGE_INCOMPLETE",
                **budget,
            }
        local = [c for c in doc["candidates"] if self._candidate_scope(doc, c) == "INSIDE"]
        checked = {e["candidate_id"] for e in doc["evidence"]}
        has_alternative = any(self._candidate_scope(doc, c) == "OUTSIDE" for c in doc["candidates"])
        if (
            doc["scope_mode"] == "PREFERRED"
            and not doc["expansion_level"]
            and local
            and all(c["id"] in checked for c in local)
            and has_alternative
            and self.coverage_status(owner, case_id) != "PROVIDER_UNAVAILABLE"
        ):
            operations = [o["id"] for o in doc["operations"] if o["kind"] == "geometry"]
            if operations:
                try:
                    self.expand(
                        owner,
                        case_id,
                        doc["revision"],
                        f"auto-expand:{doc['scope_revision']}",
                        "NO_VERIFIED_MATCH",
                        operations,
                        charge_tool=False,
                    )
                    doc = self.read(owner, case_id)
                except GeoError as exc:
                    if exc.code not in ("EVIDENCE_INSUFFICIENT", "BUDGET_EXHAUSTED"):
                        raise
        contradicted = {
            item["id"]
            for item in self.candidate_assessments(doc)
            if item["contradicted_operation_ids"]
        }
        candidates = sorted(
            doc["candidates"],
            key=lambda c: (
                0
                if doc["scope_mode"] != "PREFERRED"
                or doc["expansion_level"]
                or self._candidate_scope(doc, c) == "INSIDE"
                else 1,
                c["id"] in contradicted,
                sum(e["candidate_id"] == c["id"] for e in doc["evidence"]),
                -c["score"],
                c["id"],
            ),
        )
        plan = self.research_plan(doc, self.budget_status(owner, case_id)["budget_remaining"])
        return {
            "action": (
                "RESEARCH"
                if plan["recommended"]
                else "COLLECT_OBSERVATIONS"
                if not candidates
                else "CHECK_SPATIAL_CONSTRAINT"
            ),
            "research_plan": plan,
            "candidate_ids": [
                c["id"]
                for c in candidates
                if doc["scope_mode"] != "STRICT" or self._candidate_scope(doc, c) == "INSIDE"
            ],
            "scope_mode": doc["scope_mode"],
            "scope_revision": doc["scope_revision"],
            "coverage_status": self.coverage_status(owner, case_id),
            "case_revision": doc["revision"],
            **self.budget_status(owner, case_id),
            "limitations": [
                "Все кандидаты сохранены; рейтинг не является вероятностью",
                "Зависимые проверки не дают независимых подтверждений",
            ],
        }

    def transition(self, owner: str, case_id: str, revision: int, key: str, state: str) -> dict:
        def change(doc):
            if state not in STATES[doc["state"]]:
                raise GeoError("INVALID_INPUT", "Переход состояния запрещён")
            doc["state"] = state
            return {"state": state}

        return self.mutate(owner, case_id, revision, key, "transition", state, change)

    def finalize(
        self,
        owner: str,
        case_id: str,
        revision: int,
        key: str,
        level: str = "UNRESOLVED",
        assessment: str = "UNRESOLVED",
        candidate_id: str | None = None,
    ) -> dict:
        if assessment not in ("UNRESOLVED", "HYPOTHESIS", "PRELIMINARY", "FACT_SUPPORTED"):
            raise GeoError(
                "EVIDENCE_INSUFFICIENT",
                "Независимое визуальное подтверждение отсутствует; LOCATION_VERIFIED запрещён",
            )

        def change(doc):
            candidates = self.candidate_assessments(doc)
            top_candidates = [
                {"rank": index + 1, **candidate}
                for index, candidate in enumerate(
                    [item for item in candidates if item["eligible_as_result"]][:3]
                )
            ]
            selected = next(
                (candidate for candidate in candidates if candidate["id"] == candidate_id), None
            )
            if assessment != "UNRESOLVED":
                if (
                    selected is None
                    or level not in ("COUNTRY", "REGION", "CITY", "AREA", "POINT")
                    or selected["level"] != level
                ):
                    raise GeoError(
                        "EVIDENCE_INSUFFICIENT",
                        "Нужен существующий кандидат соответствующего уровня",
                    )
                order = {"HYPOTHESIS": 0, "PRELIMINARY": 1, "FACT_SUPPORTED": 2, "NEEDS_REVIEW": 0}
                if order[selected["location_status"]] < order[assessment]:
                    raise GeoError(
                        "EVIDENCE_INSUFFICIENT",
                        "Запрошенный уровень подтверждения не обеспечен проверками",
                    )
                if doc["scope_mode"] == "STRICT" and selected["scope_membership"] != "INSIDE":
                    raise GeoError("SCOPE_VIOLATION", "Результат вне STRICT")
                if doc["state"] == "ARCHIVED":
                    raise GeoError("INVALID_INPUT", "Архивное дело")
                doc["state"] = "REVIEW_REQUIRED"
                doc["result"] = {
                    "level": level,
                    "location_status": assessment,
                    "scene_verified": False,
                    "point": selected["point"],
                    "radius_m": None,
                    "best_candidate": selected,
                    "top_candidates": top_candidates,
                    "alternatives": [
                        candidate for candidate in candidates if candidate["id"] != candidate_id
                    ],
                    "scope": {
                        "initial_territory": doc.get("initial_search_scope", doc["search_scope"]),
                        "mode": doc["scope_mode"],
                        "scope_checked": doc["scope_checked"],
                        "expansion_history": doc["expansion_history"],
                        "coverage_status": self.coverage_status(owner, case_id),
                    },
                    "unperformed_operations": ["INDEPENDENT_IMAGE_CORRESPONDENCE_VERIFICATION"],
                    "message": "Предварительная географическая локализация. Соответствие фотографии этому месту независимо не подтверждено.",
                }
                return doc["result"]
            if level != "UNRESOLVED":
                raise GeoError(
                    "EVIDENCE_INSUFFICIENT",
                    "Для предварительного результата явно укажите assessment и candidate_id",
                )
            if "UNRESOLVED" not in STATES[doc["state"]]:
                raise GeoError("INVALID_INPUT", "Завершение запрещено в этом состоянии")
            doc["state"] = "UNRESOLVED"
            doc["result"] = {
                "level": "UNRESOLVED",
                "confidence": "LOW",
                "point": None,
                "location_status": "INSUFFICIENT_EVIDENCE",
                "scene_verified": False,
                "best_candidate": next(
                    (candidate for candidate in candidates if candidate["eligible_as_result"]), None
                ),
                "candidate_assessments": candidates,
                "top_candidates": top_candidates,
                "radius_m": None,
                "camera_azimuth_deg": None,
                "alternatives": [c["id"] for c in doc["candidates"]],
                "limitations": ["Наблюдения модели не верифицированы сервером"],
                "scope": {
                    "initial_territory": doc.get("initial_search_scope", doc["search_scope"]),
                    "mode": doc["scope_mode"],
                    "scope_checked": doc["scope_checked"],
                    "expansion_history": doc["expansion_history"],
                    "coverage_status": self.coverage_status(owner, case_id),
                    "confirmed_territory": None,
                    "best_confirmed_candidate": None,
                    "unperformed_operations": ["IMAGE_CORRESPONDENCE_VERIFICATION"],
                    "prior_is_evidence": False,
                },
                "message": "В пределах заданной территории место не установлено. Имеющихся данных недостаточно, чтобы подтвердить конкретную точку."
                if doc["scope_mode"] == "STRICT"
                else "Убедительное совпадение не подтверждено; альтернативные гипотезы сохранены.",
            }
            return doc["result"]

        payload = (
            level
            if assessment == "UNRESOLVED" and candidate_id is None
            else [level, assessment, candidate_id]
        )
        return self.mutate(owner, case_id, revision, key, "finalize", payload, change)

    def export(self, owner: str, case_id: str, format: str = "json") -> dict:
        doc = self.read(owner, case_id)
        self.record_read(owner, case_id)
        report = {
            "schema_version": "geoosint-report/1",
            "exported_at": now(),
            "case": doc,
            "evidence_graph": evidence_graph(doc),
            "hypothesis_ledger": hypothesis_ledger(doc),
            "research_plan": self.research_plan(
                doc, self.budget_status(owner, case_id)["budget_remaining"]
            ),
            "candidate_assessments": self.candidate_assessments(doc),
            "top_candidates": [
                {"rank": index + 1, **candidate}
                for index, candidate in enumerate(
                    [
                        item
                        for item in self.candidate_assessments(doc)
                        if item["eligible_as_result"]
                    ][:3]
                )
            ],
            "request_traces": self.traces(owner, case_id),
            "client_research": {
                "provenance": "CLIENT_REPORTED_NOT_SERVER_VERIFIED",
                "tool_calls": sum(
                    item["research"]["tool_calls"]
                    for item in doc["observations"]
                    if item.get("research")
                ),
                "query_count": sum(
                    len(item["research"]["queries"])
                    for item in doc["observations"]
                    if item.get("research")
                ),
                "network_attempts": None,
                "cloud_model_tokens": None,
            },
            "scope_events": self.coverage_events(owner, case_id),
            "coverage_status": self.coverage_status(owner, case_id),
            "source_refs": [
                {"operation_id": o["id"], "content_hash": o["content_hash"]}
                for o in doc["operations"]
            ],
            "limitations": [
                "Соответствие метаданных и сцены не подтверждено",
                "Внешний текст недоверенный",
            ],
        }
        if format == "json":
            return report
        if format != "markdown":
            raise GeoError("INVALID_INPUT", "Допустимы json и markdown")
        # Пользовательские строки только в JSON fenced block; не исполняются как инструкции.
        return {
            "markdown": "# GeoOSINT — отчёт\n\nНедоверенные данные расследования:\n\n"
            + "~~~json\n"
            + json.dumps(report, ensure_ascii=False, indent=2).replace("~", "\\u007e")
            + "\n~~~\n"
        }

    def delete(self, owner: str, case_id: str, revision: int) -> dict:
        with self.db.transaction() as cx:
            doc = self.read(owner, case_id)
            if doc["revision"] != revision:
                raise GeoError("REVISION_CONFLICT", "Ревизия устарела")
            from geoosint.storage.artifacts import ArtifactStore

            ArtifactStore(self).delete_case_files(owner, case_id)
            cx.execute("DELETE FROM cases WHERE id=? AND owner=?", (case_id, owner))
        return {"deleted": case_id, "backup_policy": "Копии удаляются оператором по сроку хранения"}
