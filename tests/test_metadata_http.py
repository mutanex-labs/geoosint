import json

import httpx
from test_metadata import image_bytes
from test_oauth import login, mcp_call, oauth_server, password_hash

__all__ = ["oauth_server", "password_hash"]


async def test_oauth_upload_mcp_preflight_and_delete(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        registration, token, _ = await login(client, url)
        created = await mcp_call(
            url,
            token["access_token"],
            "geo_case_create",
            {"title": "Metadata first", "idempotency_key": "create-metadata"},
        )
        cid = created.structuredContent["data"]["id"]
        denied = await client.post(
            url + f"/cases/{cid}/artifacts",
            content=image_bytes(),
            headers={"If-Match": "1", "Idempotency-Key": "upload"},
        )
        assert denied.status_code == 401
        assert not list(runtime.artifacts.root.iterdir())
        headers = {
            "Authorization": "Bearer " + token["access_token"],
            "If-Match": "1",
            "Idempotency-Key": "upload",
            "Content-Type": "application/octet-stream",
        }
        imported = await client.post(
            url + f"/cases/{cid}/artifacts", content=image_bytes(), headers=headers
        )
        assert imported.status_code == 201, imported.text
        data = imported.json()["data"]
        aid = data["result"]["artifact_id"]
        retry = await client.post(
            url + f"/cases/{cid}/artifacts", content=image_bytes(), headers=headers
        )
        assert retry.status_code == 201, retry.text
        assert retry.json()["data"] == data
        other = runtime.oauth._issue(
            registration["client_id"], ["geoosint:use"], "other", url + "/mcp"
        )
        foreign = await client.post(
            url + f"/cases/{cid}/artifacts",
            content=image_bytes(),
            headers={
                **headers,
                "Authorization": "Bearer " + other.access_token,
                "If-Match": "2",
                "Idempotency-Key": "foreign",
            },
        )
        assert foreign.status_code == 404
        checked = await mcp_call(
            url,
            token["access_token"],
            "geo_case_preflight",
            {
                "case_id": cid,
                "artifact_id": aid,
                "expected_revision": 2,
                "idempotency_key": "preflight",
            },
        )
        assert checked.structuredContent["status"] == "OK", checked
        metadata = checked.structuredContent["data"]["result"]
        assert metadata["latitude"] == 55.75
        assert metadata["longitude"] == 37.6
        assert metadata["location_status"] == "METADATA_LOCATION"
        assert metadata["external_requests"] == metadata["ai_calls"] == 0
        assert runtime.osm.requests == 0
        assert runtime.service.read("operator", cid)["scope_mode"] == "AUTO"
        inspected = await mcp_call(
            url,
            token["access_token"],
            "geo_metadata_inspect",
            {
                "case_id": cid,
                "artifact_id": aid,
                "expected_revision": 3,
                "idempotency_key": "inspect-again",
            },
        )
        assert inspected.structuredContent["status"] == "OK"
        assert inspected.structuredContent["data"]["result"]["latitude"] == 55.75
        foreign_read = await mcp_call(
            url,
            other.access_token,
            "geo_metadata_inspect",
            {
                "case_id": cid,
                "artifact_id": aid,
                "expected_revision": 4,
                "idempotency_key": "foreign-inspect",
            },
        )
        assert foreign_read.structuredContent["errors"][0]["code"] == "NOT_FOUND"
        deleted = await mcp_call(
            url,
            token["access_token"],
            "geo_artifact_delete",
            {
                "case_id": cid,
                "artifact_id": aid,
                "expected_revision": 4,
                "idempotency_key": "delete-image",
            },
        )
        assert deleted.structuredContent["status"] == "OK"
        report = await mcp_call(url, token["access_token"], "geo_case_export", {"case_id": cid})
        assert "55.75" not in json.dumps(report.structuredContent)
        assert not (runtime.artifacts.root / aid).exists()


async def test_oversized_upload_leaves_no_artifact(oauth_server):
    url, runtime = oauth_server
    async with httpx.AsyncClient(trust_env=False) as client:
        _, token, _ = await login(client, url)
        case = await mcp_call(
            url,
            token["access_token"],
            "geo_case_create",
            {"title": "Limits", "idempotency_key": "limits"},
        )
        cid = case.structuredContent["data"]["id"]
        runtime.settings.max_upload_bytes = 100
        response = await client.post(
            url + f"/cases/{cid}/artifacts",
            content=image_bytes(),
            headers={
                "Authorization": "Bearer " + token["access_token"],
                "If-Match": "1",
                "Idempotency-Key": "large",
            },
        )
        assert response.status_code in (400, 413)
        assert runtime.db.connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert not list(runtime.artifacts.root.iterdir())
        assert runtime.db.read("operator", cid)["revision"] == 1
