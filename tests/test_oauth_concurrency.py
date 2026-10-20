import asyncio
import time

import httpx
import pytest
from test_oauth import login, mcp_call, oauth_server, password_hash

from geoosint.config import Settings
from geoosint.security.oauth import fingerprint

# Fixtures re-exported for this module.
__all__ = ["oauth_server", "password_hash"]


async def test_parallel_refresh_never_two_chains(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, token, _ = await login(client, url)
        request = {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "resource": url + "/mcp",
        }
        results = await asyncio.gather(
            *(client.post(url + "/token", data=request) for _ in range(2))
        )
        assert sum(r.status_code == 200 for r in results) == 1
        assert sum(r.status_code == 400 for r in results) == 1
        fresh = next(r.json() for r in results if r.status_code == 200)
        # Повторное использование обнаружено: новая семья также отозвана.
        assert await runtime.oauth.load_access_token(fresh["access_token"]) is None
        assert await runtime.oauth.load_access_token(token["access_token"]) is None
        assert (
            runtime.oauth.cx.execute(
                "SELECT count(*) FROM credentials WHERE kind='refresh'"
            ).fetchone()[0]
            == 0
        )


async def test_each_mcp_request_rechecks_token(oauth_server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        _, token, _ = await login(client, url)
    async with httpx.AsyncClient(
        trust_env=False, headers={"Authorization": "Bearer " + token["access_token"]}
    ) as client:
        async with streamable_http_client(url + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "geo_case_create", {"title": "Before expiry", "idempotency_key": "once"}
                )
                cid = result.structuredContent["data"]["id"]
                runtime.oauth.cx.execute(
                    "UPDATE credentials SET expires=? WHERE hash=?",
                    (time.time() - 1, fingerprint(token["access_token"])),
                )
                # HTTP middleware отклоняет следующую операцию на уже открытом клиенте.
                response = await client.post(
                    url + "/mcp",
                    headers={"Accept": "application/json, text/event-stream"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "geo_case_delete",
                            "arguments": {"case_id": cid, "expected_revision": 1},
                        },
                    },
                )
                assert response.status_code == 401
                assert runtime.db.read("operator", cid)["revision"] == 1


async def test_parallel_subjects_no_context_mixing(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, token, _ = await login(client, url)
    other = runtime.oauth._issue(
        registration["client_id"], ["geoosint:use"], "second", url + "/mcp"
    )
    pairs = await asyncio.gather(
        mcp_call(
            url,
            token["access_token"],
            "geo_case_create",
            {"title": "Alice", "idempotency_key": "same-key"},
        ),
        mcp_call(
            url,
            other.access_token,
            "geo_case_create",
            {"title": "Bob", "idempotency_key": "same-key"},
        ),
    )
    first_id, second_id = [result.structuredContent["data"]["id"] for result in pairs]
    assert first_id != second_id
    assert runtime.db.read("operator", first_id)["title"] == "Alice"
    assert runtime.db.read("second", second_id)["title"] == "Bob"
    denied = await asyncio.gather(
        mcp_call(
            url,
            other.access_token,
            "geo_case_delete",
            {"case_id": first_id, "expected_revision": 1},
        ),
        mcp_call(url, token["access_token"], "geo_case_get", {"case_id": second_id}),
    )
    assert all(r.structuredContent["errors"][0]["code"] == "NOT_FOUND" for r in denied)


def test_incomplete_production_configuration_closed(tmp_path, password_hash):
    base = {
        "mode": "production",
        "host": "0.0.0.0",
        "base_url": "https://geo.example.org",
        "operator_password_hash": password_hash,
        "redirect_uris": ("https://chatgpt.com/exact-callback",),
        "data_dir": tmp_path,
    }
    for changes in [
        {"operator_password_hash": ""},
        {"redirect_uris": ()},
        {"base_url": "http://geo.example.org"},
        {"base_url": "https://geo.example.org/path"},
        {"redirect_uris": ("file:///etc/passwd",)},
    ]:
        with pytest.raises(ValueError):
            Settings(**{**base, **changes}).validate_startup()


async def test_token_database_unavailable_fails_closed(oauth_server, monkeypatch):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        _, token, _ = await login(client, url)

        def unavailable(*args):
            raise OSError("test fixture unavailable database")

        monkeypatch.setattr(runtime.oauth, "_load", unavailable)
        response = await client.post(
            url + "/mcp",
            headers={"Authorization": "Bearer " + token["access_token"]},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "geo_case_create",
                    "arguments": {"title": "Forbidden", "idempotency_key": "no"},
                },
            },
        )
        assert response.status_code in (401, 503, 500)
        assert runtime.db.connection.execute("SELECT count(*) FROM cases").fetchone()[0] == 0
