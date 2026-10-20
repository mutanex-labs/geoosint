"""Full OAuth smoke in an isolated container, with generated disposable credentials."""

import asyncio
import base64
import hashlib
import io
import json
import re
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from argon2 import PasswordHasher
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image, TiffImagePlugin

from geoosint.config import Settings
from geoosint.diagnostics.logging import configure_logging
from geoosint.mcp.server import create_server, http_app


async def scenario(base):
    callback = "http://127.0.0.1:48080/callback"
    async with httpx.AsyncClient(trust_env=False) as client:
        denied = await client.post(base + "/mcp", json={})
        assert denied.status_code == 401
        metadata = await client.get(base + "/.well-known/oauth-protected-resource/mcp")
        assert metadata.json()["resource"] == base + "/mcp"
        registration = await client.post(
            base + "/register",
            json={
                "redirect_uris": [callback],
                "client_name": "Container smoke",
                "token_endpoint_auth_method": "client_secret_post",
                "scope": "geoosint:use",
            },
        )
        assert registration.status_code == 201, registration.text
        registration = registration.json()
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        authorization = await client.get(
            base + "/authorize",
            params={
                "client_id": registration["client_id"],
                "redirect_uri": callback,
                "response_type": "code",
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "state": secrets.token_urlsafe(24),
                "scope": "geoosint:use",
                "resource": base + "/mcp",
            },
        )
        page = await client.get(authorization.headers["location"])
        fields = dict(re.findall(r'name="(request_id|csrf)" value="([^"]+)"', page.text))
        consent = await client.post(
            base + "/consent",
            headers={"Origin": base},
            data={**fields, "password": password, "decision": "allow"},
        )
        code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
        issued = await client.post(
            base + "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
                "code_verifier": verifier,
                "redirect_uri": callback,
                "code": code,
                "resource": base + "/mcp",
            },
        )
        assert issued.status_code == 200, issued.text
        token = issued.json()
    async with httpx.AsyncClient(
        trust_env=False, headers={"Authorization": "Bearer " + token["access_token"]}
    ) as client:
        async with streamable_http_client(base + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                created = await session.call_tool(
                    "geo_case_create",
                    {"title": "OAuth container smoke", "idempotency_key": "smoke"},
                )
                assert created.structuredContent["status"] == "OK", created
                cid = created.structuredContent["data"]["id"]
                exif = Image.Exif()
                rational = TiffImagePlugin.IFDRational
                exif[34853] = {
                    1: "N",
                    2: (rational(55), rational(45), rational(0)),
                    3: "E",
                    4: (rational(37), rational(36), rational(0)),
                }
                image = io.BytesIO()
                Image.new("RGB", (16, 16)).save(image, format="JPEG", exif=exif.tobytes())
                imported = await client.post(
                    base + f"/cases/{cid}/artifacts",
                    content=image.getvalue(),
                    headers={
                        "If-Match": "1",
                        "Idempotency-Key": "image",
                        "Content-Type": "application/octet-stream",
                    },
                )
                assert imported.status_code == 201, imported.text
                aid = imported.json()["data"]["result"]["artifact_id"]
                preflight = await session.call_tool(
                    "geo_case_preflight",
                    {
                        "case_id": cid,
                        "artifact_id": aid,
                        "expected_revision": 2,
                        "idempotency_key": "preflight",
                    },
                )
                assert preflight.structuredContent["status"] == "OK", preflight
                metadata = preflight.structuredContent["data"]["result"]
                assert metadata["latitude"] == 55.75 and metadata["longitude"] == 37.6
                assert metadata["location_status"] == "METADATA_LOCATION"
                assert metadata["external_requests"] == metadata["ai_calls"] == 0
                deleted = await session.call_tool(
                    "geo_case_delete", {"case_id": cid, "expected_revision": 3}
                )
                assert deleted.structuredContent["status"] == "OK"
    async with httpx.AsyncClient(trust_env=False) as client:
        revoked = await client.post(
            base + "/revoke",
            data={
                "token": token["refresh_token"],
                "client_id": registration["client_id"],
                "client_secret": registration["client_secret"],
            },
        )
        assert revoked.status_code == 200
        denied = await client.post(
            base + "/mcp", headers={"Authorization": "Bearer " + token["access_token"]}, json={}
        )
        assert denied.status_code == 401
    print(
        json.dumps(
            {
                "status": "OK",
                "oauth": "DCR_CODE_PKCE_S256_CONSENT_REVOCATION",
                "metadata": "OAUTH_UPLOAD_MCP_PREFLIGHT_EMBEDDED_GPS",
                "protocol": initialized.protocolVersion,
            }
        )
    )


if __name__ == "__main__":
    configure_logging()
    password = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="geoosint-oauth-") as directory:
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        base = f"http://127.0.0.1:{port}"
        server, runtime = create_server(
            Settings(
                mode="oauth-test",
                port=port,
                base_url=base,
                data_dir=Path(directory),
                operator_password_hash=PasswordHasher().hash(password),
                redirect_uris=("http://127.0.0.1:48080/callback",),
            )
        )
        asgi = uvicorn.Server(
            uvicorn.Config(
                http_app(server, runtime),
                host="127.0.0.1",
                port=port,
                log_level="critical",
                access_log=False,
            )
        )
        thread = threading.Thread(target=asgi.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        try:
            while not asgi.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("OAuth test server did not start")
                time.sleep(0.02)
            asyncio.run(scenario(base))
        finally:
            asgi.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
