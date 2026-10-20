"""OAuth domain persistence; protocol validation/PKCE are implemented by the MCP SDK."""

import asyncio
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

SCOPE = "geoosint:use"


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OAuthProvider:
    def __init__(
        self,
        path: Path,
        base_url: str,
        password_hash: str,
        redirect_uris: tuple[str, ...],
        subject: str = "operator",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.resource = self.base_url + "/mcp"
        self.password_hash, self.redirect_uris, self.subject = password_hash, redirect_uris, subject
        self.cx = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.cx.row_factory = sqlite3.Row
        self.cx.execute("PRAGMA journal_mode=WAL")
        self.cx.execute("PRAGMA busy_timeout=5000")
        self.cx.execute("PRAGMA secure_delete=ON")
        self.cx.executescript("""
            CREATE TABLE IF NOT EXISTS clients(id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS credentials(
                hash TEXT PRIMARY KEY, kind TEXT, expires REAL, family TEXT, value TEXT);
            CREATE TABLE IF NOT EXISTS pending(
                id TEXT PRIMARY KEY, expires REAL, client_id TEXT, params TEXT, csrf TEXT);
            CREATE INDEX IF NOT EXISTS credentials_family ON credentials(family);
        """)
        self.lock = threading.RLock()
        self.password_gate = asyncio.Semaphore(1)
        self.attempts: list[float] = []

    def close(self) -> None:
        self.cx.close()

    def _cleanup(self) -> None:
        self.cx.execute("DELETE FROM credentials WHERE expires<?", (time.time(),))
        self.cx.execute("DELETE FROM pending WHERE expires<?", (time.time(),))

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.lock:
            row = self.cx.execute("SELECT value FROM clients WHERE id=?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or any(uri not in self.redirect_uris for uri in uris):
            raise RegistrationError("invalid_redirect_uri", "Redirect URI не разрешён оператором")
        if client_info.token_endpoint_auth_method not in (
            "client_secret_post",
            "client_secret_basic",
        ):
            raise RegistrationError("invalid_client_metadata", "Нужен confidential DCR client")
        if set(client_info.grant_types) - {"authorization_code", "refresh_token"}:
            raise RegistrationError("invalid_client_metadata", "Grant type не поддерживается")
        if client_info.response_types != ["code"] or client_info.scope != SCOPE:
            raise RegistrationError(
                "invalid_client_metadata", "Недопустимый scope или response_type"
            )
        if len(client_info.model_dump_json()) > 8192:
            raise RegistrationError("invalid_client_metadata", "Слишком большая регистрация")
        with self.lock:
            if self.cx.execute("SELECT count(*) FROM clients").fetchone()[0] >= 100:
                raise RegistrationError(
                    "invalid_client_metadata", "Лимит зарегистрированных клиентов"
                )
            self.cx.execute(
                "INSERT INTO clients VALUES(?,?)",
                (client_info.client_id, client_info.model_dump_json()),
            )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if params.resource != self.resource:
            raise AuthorizeError("invalid_request", "Неверный resource")
        if not params.state or not 16 <= len(params.state) <= 512:
            raise AuthorizeError("invalid_request", "Требуется непустой state длиной 16–512")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.code_challenge):
            raise AuthorizeError("invalid_request", "Некорректный S256 challenge")
        if params.scopes != [SCOPE] or str(params.redirect_uri) not in self.redirect_uris:
            raise AuthorizeError("invalid_scope", "Недопустимый scope или redirect URI")
        identifier = secrets.token_urlsafe(32)
        with self.lock:
            self._cleanup()
            if self.cx.execute("SELECT count(*) FROM pending").fetchone()[0] >= 100:
                raise AuthorizeError("temporarily_unavailable", "Лимит незавершённых авторизаций")
            self.cx.execute(
                "INSERT INTO pending VALUES(?,?,?,?,NULL)",
                (
                    fingerprint(identifier),
                    time.time() + 300,
                    client.client_id,
                    params.model_dump_json(),
                ),
            )
        return self.base_url + "/consent?request_id=" + identifier

    async def consent(self, request: Request) -> Response:
        headers = {
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'",
        }
        if request.method == "GET":
            identifier = request.query_params.get("request_id", "")
            with self.lock:
                row = self.cx.execute(
                    "SELECT * FROM pending WHERE id=? AND expires>?",
                    (fingerprint(identifier), time.time()),
                ).fetchone()
                if not row:
                    return JSONResponse(
                        {"error": "invalid_request"}, status_code=400, headers=headers
                    )
                csrf = secrets.token_urlsafe(32)
                self.cx.execute(
                    "UPDATE pending SET csrf=? WHERE id=?", (fingerprint(csrf), row["id"])
                )
            client = await self.get_client(row["client_id"])
            if client is None:
                return JSONResponse({"error": "invalid_client"}, status_code=400, headers=headers)
            params = AuthorizationParams.model_validate_json(row["params"])
            body = (
                '<!doctype html><html lang="ru"><meta charset="utf-8"><title>GeoOSINT</title>'
                "<h1>Разрешить доступ к GeoOSINT</h1><p>Клиент: "
                + html.escape(client.client_name or client.client_id or "")
                + "</p><p>Callback: "
                + html.escape(str(params.redirect_uri))
                + "</p><p>Доступ: чтение, изменение и удаление ваших расследований.</p>"
                + '<form method="post" action="/consent"><input type="hidden" name="request_id" value="'
                + html.escape(identifier, quote=True)
                + '"><input type="hidden" name="csrf" value="'
                + csrf
                + '"><label>Пароль оператора <input type="password" name="password" '
                + 'required autocomplete="current-password" maxlength="1024"></label>'
                + '<button name="decision" value="allow">Разрешить</button>'
                + '<button name="decision" value="deny">Отклонить</button></form></html>'
            )
            response: Response = HTMLResponse(body, headers=headers)
            response.set_cookie(
                "geo_csrf",
                csrf,
                max_age=300,
                httponly=True,
                secure=urlsplit(self.base_url).scheme == "https",
                samesite="strict",
                path="/consent",
            )
            return response
        if request.headers.get("origin") != self.base_url:
            return JSONResponse({"error": "invalid_origin"}, status_code=403, headers=headers)
        form = await request.form()
        identifier, csrf, password = (
            str(form.get(k, "")) for k in ("request_id", "csrf", "password")
        )
        with self.lock:
            row = self.cx.execute(
                "SELECT * FROM pending WHERE id=? AND expires>?",
                (fingerprint(identifier), time.time()),
            ).fetchone()
        if (
            not row
            or not row["csrf"]
            or not hmac.compare_digest(fingerprint(csrf), row["csrf"])
            or not hmac.compare_digest(csrf, request.cookies.get("geo_csrf", ""))
        ):
            return JSONResponse({"error": "invalid_csrf"}, status_code=403, headers=headers)
        if form.get("decision") != "allow":
            with self.lock:
                self.cx.execute("DELETE FROM pending WHERE id=?", (row["id"],))
            params = AuthorizationParams.model_validate_json(row["params"])
            return RedirectResponse(
                construct_redirect_uri(
                    str(params.redirect_uri), error="access_denied", state=params.state
                ),
                status_code=303,
                headers=headers,
            )
        async with self.password_gate:
            self.attempts = [t for t in self.attempts if time.monotonic() - t < 60]
            if len(self.attempts) >= 5:
                return JSONResponse({"error": "rate_limited"}, status_code=429, headers=headers)
            self.attempts.append(time.monotonic())
            try:
                if not 1 <= len(password) <= 1024:
                    raise VerificationError("invalid password length")
                await asyncio.to_thread(PasswordHasher().verify, self.password_hash, password)
            except VerificationError:
                return JSONResponse({"error": "access_denied"}, status_code=403, headers=headers)
        params = AuthorizationParams.model_validate_json(row["params"])
        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 60,
            client_id=row["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=self.subject,
        )
        with self.lock:
            self.cx.execute("BEGIN IMMEDIATE")
            try:
                removed = self.cx.execute(
                    "DELETE FROM pending WHERE id=? AND expires>?", (row["id"], time.time())
                ).rowcount
                if removed != 1:
                    self.cx.execute("ROLLBACK")
                    return JSONResponse(
                        {"error": "invalid_request"}, status_code=400, headers=headers
                    )
                self.cx.execute(
                    "INSERT INTO credentials VALUES(?,?,?,?,?)",
                    (
                        fingerprint(code),
                        "code",
                        record.expires_at,
                        "",
                        record.model_dump_json(exclude={"code"}),
                    ),
                )
                self.cx.execute("COMMIT")
            except BaseException:
                if self.cx.in_transaction:
                    self.cx.execute("ROLLBACK")
                raise
        response = RedirectResponse(
            construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
            status_code=303,
            headers=headers,
        )
        response.delete_cookie("geo_csrf", path="/consent")
        return response

    def _load(self, token: str, kind: str) -> sqlite3.Row | None:
        if len(token) > 512:
            return None
        with self.lock:
            row = self.cx.execute(
                "SELECT * FROM credentials WHERE hash=? AND kind=? AND expires>?",
                (fingerprint(token), kind, time.time()),
            ).fetchone()
        return cast(sqlite3.Row | None, row)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self._load(authorization_code, "code")
        if not row:
            return None
        result = AuthorizationCode(code=authorization_code, **json.loads(row["value"]))
        return result if result.client_id == client.client_id else None

    def _issue(
        self,
        client_id: str,
        scopes: list[str],
        subject: str,
        resource: str,
        family: str | None = None,
    ) -> OAuthToken:
        self._cleanup()
        if self.cx.execute("SELECT count(*) FROM credentials").fetchone()[0] > 10000:
            raise TokenError("invalid_grant", "Лимит токенов")
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        family = family or secrets.token_urlsafe(32)
        expiration = int(time.time())
        fields = {
            "client_id": client_id,
            "scopes": scopes,
            "subject": subject,
            "resource": resource,
        }
        for token, kind, seconds in [(access, "access", 900), (refresh, "refresh", 86400)]:
            self.cx.execute(
                "INSERT INTO credentials VALUES(?,?,?,?,?)",
                (
                    fingerprint(token),
                    kind,
                    expiration + seconds,
                    family,
                    json.dumps({**fields, "expires_at": expiration + seconds}),
                ),
            )
        return OAuthToken(
            access_token=access, refresh_token=refresh, expires_in=900, scope=" ".join(scopes)
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        with self.lock:
            self.cx.execute("BEGIN IMMEDIATE")
            try:
                row = self._load(authorization_code.code, "code")
                if (
                    not row
                    or authorization_code.client_id != client.client_id
                    or authorization_code.resource != self.resource
                ):
                    raise TokenError("invalid_grant", "Код недоступен")
                self.cx.execute("DELETE FROM credentials WHERE hash=?", (row["hash"],))
                result = self._issue(
                    client.client_id or "",
                    authorization_code.scopes,
                    authorization_code.subject or "",
                    self.resource,
                )
                self.cx.execute("COMMIT")
                return result
            except BaseException:
                self.cx.execute("ROLLBACK")
                raise

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load(token, "access")
        if not row:
            return None
        value = json.loads(row["value"])
        if value["resource"] != self.resource or not value["subject"]:
            return None
        return AccessToken(token=token, claims={"iss": self.base_url}, **value)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        row = self._load(refresh_token, "refresh")
        if not row:
            used = self._load(refresh_token, "used_refresh")
            if used and json.loads(used["value"])["client_id"] == client.client_id:
                with self.lock:
                    self.cx.execute("DELETE FROM credentials WHERE family=?", (used["family"],))
            return None
        value = json.loads(row["value"])
        if value["client_id"] != client.client_id or value["resource"] != self.resource:
            return None
        return RefreshToken(token=refresh_token, **value)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        with self.lock:
            self.cx.execute("BEGIN IMMEDIATE")
            try:
                row = self._load(refresh_token.token, "refresh")
                if (
                    not row
                    or refresh_token.client_id != client.client_id
                    or set(scopes) - set(refresh_token.scopes)
                ):
                    raise TokenError("invalid_grant", "Refresh token недоступен")
                self.cx.execute(
                    "DELETE FROM credentials WHERE family=? AND kind IN ('access','refresh')",
                    (row["family"],),
                )
                self.cx.execute(
                    "INSERT INTO credentials VALUES(?,?,?,?,?)",
                    (
                        row["hash"],
                        "used_refresh",
                        row["expires"],
                        row["family"],
                        json.dumps({"client_id": client.client_id}),
                    ),
                )
                result = self._issue(
                    client.client_id or "",
                    scopes,
                    refresh_token.subject or "",
                    self.resource,
                    row["family"],
                )
                self.cx.execute("COMMIT")
                return result
            except BaseException:
                self.cx.execute("ROLLBACK")
                raise

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        with self.lock:
            row = self.cx.execute(
                "SELECT family FROM credentials WHERE hash=?", (fingerprint(token.token),)
            ).fetchone()
            if row:
                self.cx.execute("DELETE FROM credentials WHERE family=?", (row[0],))
