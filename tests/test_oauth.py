import base64
import hashlib
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from argon2 import PasswordHasher
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from test_mcp import running_server

from geoosint.security.oauth import SCOPE, fingerprint

CALLBACK = "http://127.0.0.1:48080/callback"


@pytest.fixture(scope="module")
def password_hash():
    return PasswordHasher().hash("test-only-operator-password")


@pytest.fixture
def oauth_server(tmp_path, password_hash):
    with running_server(
        tmp_path, mode="oauth-test", operator_password_hash=password_hash, redirect_uris=(CALLBACK,)
    ) as pair:
        yield pair


async def register(client, url, **overrides):
    response = await client.post(
        url + "/register",
        json={
            "redirect_uris": [CALLBACK],
            "client_name": "MCP security tests",
            "token_endpoint_auth_method": "client_secret_post",
            "scope": SCOPE,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            **overrides,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def authorize(client, url, registration, **overrides):
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    response = await client.get(
        url + "/authorize",
        params={
            "client_id": registration["client_id"],
            "redirect_uri": CALLBACK,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": secrets.token_urlsafe(24),
            "scope": SCOPE,
            "resource": url + "/mcp",
            **overrides,
        },
    )
    return response, verifier


async def consent(client, url, location, **overrides):
    page = await client.get(location)
    assert page.status_code == 200
    values = {k: v for k, v in re.findall(r'name="(request_id|csrf)" value="([^"]+)"', page.text)}
    return await client.post(
        url + "/consent",
        headers={"Origin": url},
        data={
            **values,
            "password": "test-only-operator-password",
            "decision": "allow",
            **overrides,
        },
    )


def token_parameters(url, registration, verifier, code):
    return {
        "grant_type": "authorization_code",
        "client_id": registration["client_id"],
        "client_secret": registration["client_secret"],
        "redirect_uri": CALLBACK,
        "code_verifier": verifier,
        "code": code,
        "resource": url + "/mcp",
    }


async def login(client, url):
    registration = await register(client, url)
    response, verifier = await authorize(client, url, registration)
    assert response.status_code == 302, response.text
    approved = await consent(client, url, response.headers["location"])
    assert approved.status_code == 303, approved.text
    code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
    parameters = token_parameters(url, registration, verifier, code)
    token = await client.post(url + "/token", data=parameters)
    assert token.status_code == 200, token.text
    return registration, token.json(), parameters


async def mcp_call(url, token, name, arguments):
    async with httpx.AsyncClient(
        headers={"Authorization": "Bearer " + token}, trust_env=False
    ) as client:
        async with streamable_http_client(url + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(name, arguments)


async def test_complete_oauth_http_rotation_revocation(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        challenge = await client.post(url + "/mcp", json={})
        assert challenge.status_code == 401
        assert "resource_metadata=" in challenge.headers["www-authenticate"]
        metadata = (await client.get(url + "/.well-known/oauth-protected-resource/mcp")).json()
        assert metadata["resource"] == url + "/mcp"
        metadata = (await client.get(url + "/.well-known/oauth-authorization-server")).json()
        assert metadata["code_challenge_methods_supported"] == ["S256"]
        assert metadata["issuer"].rstrip("/") == url
        registration, token, params = await login(client, url)
        # Протокольный код одноразовый; повтор не выдаёт новые токены.
        counts = runtime.oauth.cx.execute("SELECT count(*) FROM credentials").fetchone()[0]
        replay = await client.post(url + "/token", data=params)
        assert replay.status_code == 400
        assert runtime.oauth.cx.execute("SELECT count(*) FROM credentials").fetchone()[0] == counts
        created = await mcp_call(
            url,
            token["access_token"],
            "geo_case_create",
            {"title": "OAuth protected", "idempotency_key": "oauth-case"},
        )
        assert created.structuredContent["status"] == "OK"
        cid = created.structuredContent["data"]["id"]
        assert runtime.db.read("operator", cid)["id"] == cid
        row = runtime.oauth.cx.execute(
            "SELECT hash,value FROM credentials WHERE kind='access'"
        ).fetchone()
        assert row["hash"] == fingerprint(token["access_token"])
        assert token["access_token"] not in row["value"]
        refresh = {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "resource": url + "/mcp",
        }
        bad_resource = await client.post(
            url + "/token", data={**refresh, "resource": "https://evil.example/mcp"}
        )
        assert bad_resource.status_code == 400
        rotated = await client.post(url + "/token", data=refresh)
        assert rotated.status_code == 200, rotated.text
        newer = rotated.json()
        assert newer["access_token"] != token["access_token"]
        assert newer["refresh_token"] != token["refresh_token"]
        old = await client.post(
            url + "/mcp", headers={"Authorization": "Bearer " + token["access_token"]}, json={}
        )
        assert old.status_code == 401
        exported = await mcp_call(url, newer["access_token"], "geo_case_export", {"case_id": cid})
        assert exported.structuredContent["status"] == "OK"
        revoked = await client.post(
            url + "/revoke",
            data={
                "token": newer["refresh_token"],
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
            },
        )
        assert revoked.status_code == 200, revoked.text
        rejected = await client.post(
            url + "/mcp", headers={"Authorization": "Bearer " + newer["access_token"]}, json={}
        )
        assert rejected.status_code == 401
        assert runtime.db.read("operator", cid)["revision"] == 1


async def test_bad_pkce_redirect_resource_csrf_no_tokens(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        bad_client = await client.post(
            url + "/register",
            json={
                "redirect_uris": ["https://evil.example/callback"],
                "scope": SCOPE,
                "token_endpoint_auth_method": "client_secret_post",
            },
        )
        assert bad_client.status_code == 400
        assert runtime.oauth.cx.execute("SELECT count(*) FROM clients").fetchone()[0] == 0
        registration = await register(client, url)
        bad_redirect, _ = await authorize(
            client, url, registration, redirect_uri="https://evil.example/callback"
        )
        assert bad_redirect.status_code == 400
        bad_resource, _ = await authorize(
            client, url, registration, resource="https://evil.example/mcp"
        )
        assert "error=" in bad_resource.headers["location"]
        pending, verifier = await authorize(client, url, registration)
        bad_consent = await consent(client, url, pending.headers["location"], csrf="invalid")
        assert bad_consent.status_code == 403
        wrong_password = await consent(client, url, pending.headers["location"], password="wrong")
        assert wrong_password.status_code == 403
        assert runtime.oauth.cx.execute("SELECT count(*) FROM credentials").fetchone()[0] == 0
        approved = await consent(client, url, pending.headers["location"])
        code = parse_qs(urlsplit(approved.headers["location"]).query)["code"][0]
        parameters = token_parameters(url, registration, verifier, code)
        for changes in [
            {"code_verifier": secrets.token_urlsafe(48)},
            {"redirect_uri": "https://evil.example/callback"},
            {"resource": "https://evil.example/mcp"},
            {"client_secret": "wrong"},
        ]:
            result = await client.post(url + "/token", data={**parameters, **changes})
            assert result.status_code in (400, 401)
            assert (
                runtime.oauth.cx.execute(
                    "SELECT count(*) FROM credentials WHERE kind='access'"
                ).fetchone()[0]
                == 0
            )
        good = await client.post(url + "/token", data=parameters)
        assert good.status_code == 200


async def test_real_token_expiry_audience_scopes_and_idor(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, token, _ = await login(client, url)
        created = await mcp_call(
            url,
            token["access_token"],
            "geo_case_create",
            {"title": "Private", "idempotency_key": "private"},
        )
        cid = created.structuredContent["data"]["id"]
        oauth = runtime.oauth
        # Два разных субъекта выпускаются доверенным серверным fixture, не входом клиента.
        other = oauth._issue(registration["client_id"], [SCOPE], "other-user", url + "/mcp")
        denied = await mcp_call(url, other.access_token, "geo_case_get", {"case_id": cid})
        assert denied.structuredContent["errors"][0]["code"] == "NOT_FOUND"
        wrong_scope = oauth._issue(registration["client_id"], [], "operator", url + "/mcp")
        wrong_audience = oauth._issue(
            registration["client_id"], [SCOPE], "operator", "https://elsewhere.example/mcp"
        )
        expired = oauth._issue(registration["client_id"], [SCOPE], "operator", url + "/mcp")
        oauth.cx.execute(
            "UPDATE credentials SET expires=? WHERE hash=?",
            (time.time() - 1, fingerprint(expired.access_token)),
        )
        for value, expected in [
            (wrong_scope.access_token, 403),
            (wrong_audience.access_token, 401),
            (expired.access_token, 401),
            ("unknown", 401),
        ]:
            response = await client.post(
                url + "/mcp", headers={"Authorization": "Bearer " + value}, json={}
            )
            assert response.status_code == expected, response.text
        assert runtime.db.read("operator", cid)["revision"] == 1
        assert (
            json.loads(oauth._load(token["access_token"], "access")["value"])["subject"]
            == "operator"
        )


async def test_refresh_reuse_revokes_family(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, token, _ = await login(client, url)
        parameters = {
            "grant_type": "refresh_token",
            "refresh_token": token["refresh_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "resource": url + "/mcp",
        }
        fresh = await client.post(url + "/token", data=parameters)
        assert fresh.status_code == 200
        replay = await client.post(url + "/token", data=parameters)
        assert replay.status_code == 400
        assert await runtime.oauth.load_access_token(fresh.json()["access_token"]) is None


async def test_older_refresh_replay_after_two_rotations(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, original, _ = await login(client, url)
        current = original
        for _ in range(2):
            response = await client.post(
                url + "/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": current["refresh_token"],
                    "client_id": registration["client_id"],
                    "client_secret": registration["client_secret"],
                    "resource": url + "/mcp",
                },
            )
            assert response.status_code == 200
            current = response.json()
        replay = await client.post(
            url + "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": original["refresh_token"],
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "resource": url + "/mcp",
            },
        )
        assert replay.status_code == 400
        assert await runtime.oauth.load_access_token(current["access_token"]) is None
