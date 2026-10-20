import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from geoosint.config import Settings
from geoosint.mcp.server import create_server, http_app


@contextmanager
def running_server(directory, **overrides):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    settings = Settings(
        data_dir=directory, port=port, base_url=f"http://127.0.0.1:{port}", **overrides
    )
    mcp, runtime = create_server(settings)
    app = http_app(mcp, runtime)
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("Тестовый ASGI сервер не запущен")
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}", runtime
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()


async def connect(url, action):
    async with streamable_http_client(url + "/mcp") as (read, write, _):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            assert initialized.protocolVersion in (
                "2025-11-25",
                "2025-06-18",
                "2025-03-26",
                "2024-11-05",
            )
            return await action(session)


async def test_all_tools_http_restart_delete(tmp_path):
    called = set()
    with running_server(tmp_path) as (url, _runtime):

        async def scenario(session):
            tools = (await session.list_tools()).tools
            assert len(tools) >= 16
            for tool in tools:
                Draft202012Validator.check_schema(tool.inputSchema)
                assert tool.inputSchema["additionalProperties"] is False
                assert tool.annotations is not None
            schemas = {t.name: t.outputSchema for t in tools}

            async def call(name, arguments=None, error=None):
                result = await session.call_tool(name, arguments or {})
                called.add(name)
                assert not result.isError, result
                structured = result.structuredContent
                assert structured and "request_id" in structured
                Draft202012Validator(schemas[name]).validate(structured)
                if error:
                    assert structured["errors"][0]["code"] == error
                else:
                    assert structured["status"] == "OK", structured
                return structured["data"]

            caps = await call("geo_capabilities")
            assert caps["public_ready"] is False
            extra = await session.call_tool("geo_capabilities", {"unexpected": True})
            assert extra.isError
            created = await call(
                "geo_case_create", {"title": "MCP E2E", "idempotency_key": "create"}
            )
            cid = created["id"]
            replay = await call(
                "geo_case_create", {"title": "MCP E2E", "idempotency_key": "create"}
            )
            assert replay == created
            summary = await call("geo_case_get", {"case_id": cid})
            assert summary["revision"] == 1

            def mutation(revision, key):
                return {"case_id": cid, "expected_revision": revision, "idempotency_key": key}

            obs = await call(
                "geo_observation_add",
                {
                    **mutation(1, "obs"),
                    "observation": {
                        "category": "INFRASTRUCTURE",
                        "text": "Мост рядом с рельсами",
                        "source": "модель",
                        "dependency_group": "image:scene1",
                    },
                },
            )
            assert obs["result"]["verification"] == "UNVERIFIED"
            candidates = await call(
                "geo_candidates_add",
                {
                    **mutation(2, "candidate"),
                    "candidates": [
                        {
                            "label": "Область А",
                            "level": "AREA",
                            "area": {"south": 0, "west": 0, "north": 1, "east": 1},
                        },
                        {
                            "label": "Альтернатива",
                            "level": "AREA",
                            "area": {"south": 1, "west": 1, "north": 2, "east": 2},
                        },
                    ],
                },
            )
            candidate_id = candidates["result"][0]["id"]
            await call(
                "geo_candidates_rank",
                {
                    **mutation(3, "rank"),
                    "candidate_id": candidate_id,
                    "score": -90,
                    "reason": "Низкий визуальный рейтинг",
                },
            )
            operation = await call(
                "geo_geometry",
                {
                    **mutation(4, "geometry"),
                    "calculation": {
                        "operation": "inverse",
                        "start": {"latitude": 0, "longitude": 0},
                        "end": {"latitude": 0, "longitude": 1},
                    },
                },
            )
            await call(
                "geo_sun_position",
                {
                    **mutation(5, "sun"),
                    "point": {"latitude": 0, "longitude": 0},
                    "captured_at": "2024-03-20T12:00:00+00:00",
                },
            )
            await call("geo_places_search", {"query": "Москва"})
            before_osm = await call("geo_case_get", {"case_id": cid})
            await call(
                "geo_osm_features",
                {
                    **mutation(6, "osm"),
                    "category": "bridges",
                    "bounds": {"south": 0, "west": 0, "north": 0.01, "east": 0.01},
                },
                error="UPSTREAM_UNAVAILABLE",
            )
            after_osm = await call("geo_case_get", {"case_id": cid})
            assert before_osm == after_osm
            await call(
                "geo_evidence_add",
                {
                    **mutation(6, "evidence"),
                    "evidence": {
                        "candidate_id": candidate_id,
                        "operation_id": operation["result"]["id"],
                        "description": "Геодезическое вычисление",
                        "observation_ids": [obs["result"]["id"]],
                    },
                },
            )
            next_step = await call("geo_case_next", {"case_id": cid})
            assert candidate_id in next_step["candidate_ids"]
            await call(
                "geo_spatial_check",
                {
                    **mutation(7, "spatial-invalid"),
                    "candidate_id": candidate_id,
                    "observation_id": obs["result"]["id"],
                    "first": {"operation_id": operation["result"]["id"], "source_id": "node/1"},
                    "second": {"operation_id": operation["result"]["id"], "source_id": "node/2"},
                },
                error="INVALID_INPUT",
            )
            await call(
                "geo_case_finalize",
                {**mutation(7, "false-final"), "level": "POINT"},
                error="EVIDENCE_INSUFFICIENT",
            )
            await call("geo_case_transition", {**mutation(7, "review"), "state": "REVIEW_REQUIRED"})
            await call("geo_case_finalize", mutation(8, "final"))
            report = await call("geo_case_export", {"case_id": cid})
            assert report["case"]["result"]["level"] == "UNRESOLVED"
            assert len(report["case"]["candidates"]) == 2
            markdown = await call("geo_case_export", {"case_id": cid, "format": "markdown"})
            assert "UNRESOLVED" in markdown["markdown"]
            bad = await session.call_tool(
                "geo_geometry",
                {
                    **mutation(9, "bad"),
                    "calculation": {
                        "operation": "inverse",
                        "start": {"latitude": 91, "longitude": 0},
                        "end": {"latitude": 0, "longitude": 0},
                    },
                },
            )
            assert bad.isError
            await call("geo_case_get", {"case_id": "../../foreign"}, error="NOT_FOUND")
            resources = await session.list_resources()
            assert any(str(r.uri) == "geoosint://guide" for r in resources.resources)
            await session.read_resource("geoosint://guide")
            for name in ("geo_metadata_inspect", "geo_case_preflight", "geo_artifact_delete"):
                await call(
                    name, {**mutation(9, name), "artifact_id": "fake-artifact"}, error="NOT_FOUND"
                )
            await call(
                "geo_case_scope_set",
                {**mutation(9, "scope-update"), "search_scope": {"mode": "AUTO"}},
            )
            await call(
                "geo_case_scope_expand",
                {
                    **mutation(10, "bad-expand"),
                    "reason": "USER_AUTHORIZED",
                    "user_authorized": True,
                },
                error="SCOPE_VIOLATION",
            )
            return cid, {t.name for t in tools}

        cid, tool_names = await connect(url, scenario)

    with running_server(tmp_path) as (url, runtime):

        async def persisted(session):
            report = await session.call_tool("geo_case_export", {"case_id": cid})
            assert report.structuredContent["data"]["case"]["revision"] == 10
            deleted = await session.call_tool(
                "geo_case_delete", {"case_id": cid, "expected_revision": 10}
            )
            assert deleted.structuredContent["status"] == "OK"
            called.add("geo_case_delete")
            missing = await session.call_tool("geo_case_get", {"case_id": cid})
            assert missing.structuredContent["errors"][0]["code"] == "NOT_FOUND"

        await connect(url, persisted)
        assert runtime.db.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert runtime.db.connection.execute("SELECT count(*) FROM idempotency").fetchone()[0] == 0
    assert called == tool_names


async def test_http_boundaries_and_no_side_effects(tmp_path):
    with running_server(tmp_path) as (url, runtime):
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(url + "/healthz")).status_code == 200
            assert (await client.get(url + "/readyz")).json()["status"] == "READY"
            for token in ("unknown", "expired", "revoked", "wrong-audience", "missing-scope"):
                response = await client.post(
                    url + "/mcp",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
                assert response.status_code == 401
            response = await client.post(
                url + "/mcp",
                headers={"Origin": "https://evil.example"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 403
            response = await client.post(
                url + "/mcp",
                headers={"Host": "evil.example"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert response.status_code == 421
            assert runtime.db.connection.execute("SELECT count(*) FROM cases").fetchone()[0] == 0


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
async def test_negotiation(tmp_path, version):
    with running_server(tmp_path) as (url, _):
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                url + "/mcp",
                headers={"Accept": "application/json, text/event-stream"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": version,
                        "capabilities": {},
                        "clientInfo": {"name": "geoosint-tests", "version": "1"},
                    },
                },
            )
            assert response.status_code == 200
            assert response.json()["result"]["protocolVersion"] == version
