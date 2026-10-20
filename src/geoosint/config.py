import json
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr

from geoosint.core.models import StrictModel


class Settings(StrictModel):
    mode: str = "local"
    host: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1024, le=65535)
    data_dir: Path = Path("data")
    base_url: str = "http://127.0.0.1:8787"
    origins: tuple[str, ...] = ("http://127.0.0.1:8787", "http://localhost:8787")
    osm_enabled: bool = False
    overpass_endpoint: Literal[
        "https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"
    ] = "https://overpass-api.de/api/interpreter"
    osm_user_agent: str = ""
    operator_password_hash: SecretStr = SecretStr("")
    operator_subject: str = Field(default="operator", min_length=1, max_length=128)
    redirect_uris: tuple[str, ...] = ()
    boundaries_file: Path | None = None
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1024, le=100 * 1024 * 1024)
    max_artifacts_per_case: int = Field(default=10, ge=1, le=100)
    max_cases: int = Field(default=100, ge=1, le=10000)
    max_candidates: int = Field(default=500, ge=1, le=10000)
    max_observations: int = Field(default=1000, ge=1, le=10000)
    max_operations: int = Field(default=2000, ge=1, le=10000)
    max_evidence: int = Field(default=2000, ge=1, le=10000)
    max_area_m2: float = Field(default=100_000_000, gt=0, le=1_000_000_000)
    cache_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=1024**3)

    def validate_startup(self) -> None:
        if self.mode not in ("local", "oauth-test", "production"):
            raise ValueError("Неизвестный режим запуска")
        if self.mode != "production" and self.host not in ("127.0.0.1", "::1"):
            raise ValueError("Локальный режим требует loopback")
        if self.mode != "local":
            from argon2 import Type, extract_parameters

            try:
                parameters = extract_parameters(self.operator_password_hash.get_secret_value())
            except Exception as exc:
                raise ValueError("Нужен Argon2id hash пароля оператора") from exc
            if (
                parameters.type != Type.ID
                or parameters.memory_cost < 19456
                or parameters.time_cost < 2
            ):
                raise ValueError("Нужен Argon2id: memory_cost >= 19456, time_cost >= 2")
            if (
                parameters.memory_cost > 131072
                or parameters.parallelism > 4
                or parameters.time_cost > 10
            ):
                raise ValueError("Argon2 параметры превышают ресурсный бюджет")
            if not self.redirect_uris:
                raise ValueError("Требуется точный allowlist redirect URI")
            base = urlsplit(self.base_url)
            if (
                base.path not in ("", "/")
                or base.query
                or base.fragment
                or base.username
                or not base.hostname
            ):
                raise ValueError("BASE_URL должен быть origin без пути и учётных данных")
            if self.mode == "production" and base.scheme != "https":
                raise ValueError("Production требует HTTPS BASE_URL")
            if self.mode == "oauth-test" and (
                base.scheme != "http" or base.hostname not in ("127.0.0.1", "localhost")
            ):
                raise ValueError("OAuth test требует HTTP loopback BASE_URL")
            for uri in self.redirect_uris:
                redirect = urlsplit(uri)
                if (
                    redirect.fragment
                    or redirect.username
                    or not redirect.hostname
                    or (
                        redirect.scheme != "https"
                        and not (
                            redirect.scheme == "http"
                            and redirect.hostname in ("127.0.0.1", "localhost")
                        )
                    )
                ):
                    raise ValueError("Небезопасный redirect URI")
        if self.osm_enabled and (
            not self.osm_user_agent.startswith("GeoOSINT/") or len(self.osm_user_agent) < 20
        ):
            raise ValueError("OSM требует GeoOSINT/version и контакт оператора в User-Agent")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.data_dir.is_symlink():
            raise ValueError("Каталог данных не должен быть symlink")

    @classmethod
    def from_env(cls) -> "Settings":
        names = {
            key: os.environ[f"GEO_{key.upper()}"]
            for key in cls.model_fields
            if f"GEO_{key.upper()}" in os.environ and key not in ("origins", "redirect_uris")
        }
        if "BASE_URL" in os.environ:
            names["base_url"] = os.environ["BASE_URL"]
        if names.get("boundaries_file") == "":
            names.pop("boundaries_file")
        for field in ("origins", "redirect_uris"):
            if f"GEO_{field.upper()}" in os.environ:
                names[field] = json.loads(os.environ[f"GEO_{field.upper()}"])
        result = cls.model_validate(names)
        return result
