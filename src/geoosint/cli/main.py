import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import uvicorn
from pydantic import TypeAdapter

from geoosint.config import Settings
from geoosint.core.geometry import geometry
from geoosint.core.models import Bounds, GeometryInput
from geoosint.errors import GeoError
from geoosint.mcp.server import OWNER, Runtime, create_server


def doctor(runtime: Runtime) -> dict:
    from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS

    subject = OWNER if runtime.settings.mode == "local" else runtime.settings.operator_subject
    integrity = runtime.db.integrity(subject)
    with runtime.db.transaction() as cx:
        version = cx.execute("SELECT max(version) FROM schema_version").fetchone()[0]
    probe = geometry(
        TypeAdapter(GeometryInput).validate_python(
            {
                "operation": "inverse",
                "start": {"latitude": 0, "longitude": 0},
                "end": {"latitude": 0, "longitude": 1},
            }
        )
    )
    return {
        "status": "OK" if integrity["status"] == "OK" and version == 4 else "ERROR",
        "schema_version": version,
        "integrity": integrity,
        "geometry_ready": abs(probe["distance_m"] - 111319.490793) < 0.01,
        "protocol_versions": SUPPORTED_PROTOCOL_VERSIONS,
        "providers": [runtime.osm.capabilities()],
        "cache": {"hits": runtime.cache.hits, "misses": runtime.cache.misses},
        "database_bytes": runtime.db.path.stat().st_size,
        "public_ready": False,
        "oauth": "CODE_PKCE_S256" if runtime.oauth else "DISABLED_LOCAL_ONLY",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="GeoOSINT — локальный сервер расследований")
    parser.add_argument("--json", action="store_true", help="Машиночитаемый JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("password-hash")
    metadata = sub.add_parser("metadata")
    meta_sub = metadata.add_subparsers(dest="metadata_command", required=True)
    inspect = meta_sub.add_parser("inspect")
    inspect.add_argument("path", type=Path)
    inspect.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    inspect.add_argument("--no-store", action="store_true")
    inspect.add_argument("--case-id")
    check = sub.add_parser("doctor")
    check.add_argument("--network", action="store_true")
    sub.add_parser("config")
    sub.add_parser("integrity")
    sub.add_parser("providers")
    sub.add_parser("migrations")
    create = sub.add_parser("case-create")
    create.add_argument("title")
    export = sub.add_parser("case-export")
    export.add_argument("case_id")
    export.add_argument("--format", choices=["json", "markdown"], default="json")
    calc = sub.add_parser("geometry")
    calc.add_argument("calculation", help="JSON параметров вычисления")
    backup = sub.add_parser("backup")
    backup.add_argument("destination", type=Path)
    args = parser.parse_args()
    runtime = None
    try:
        if args.command == "metadata" and (args.no_store or not args.case_id):
            from geoosint.storage.artifacts import inspect_file_isolated

            maximum = Settings.from_env().max_upload_bytes
            result = inspect_file_isolated(args.path, maximum)
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return
        if args.command == "password-hash":
            from getpass import getpass

            from argon2 import PasswordHasher

            password = getpass("Пароль оператора (не менее 16 знаков): ")
            if len(password) < 16 or password != getpass("Повторите пароль: "):
                raise ValueError("Пароль слишком короткий или не совпадает")
            print(PasswordHasher().hash(password))
            return
        settings = Settings.from_env()
        settings.validate_startup()
        subject = OWNER if settings.mode == "local" else settings.operator_subject
        if args.command == "serve":
            # Стандартный ASGI lifespan закрывает хранилище после завершения запросов.
            uvicorn.run(
                "geoosint.mcp.server:create_app",
                factory=True,
                host=settings.host,
                port=settings.port,
                proxy_headers=False,
                access_log=False,
            )
            return
        server, runtime = create_server(settings)
        if args.command == "metadata":
            doc = runtime.service.read(subject, args.case_id)
            with args.path.open("rb") as source:
                content = source.read(settings.max_upload_bytes + 1)
            imported = runtime.artifacts.import_bytes(
                subject, args.case_id, doc["revision"], str(uuid4()), content
            )
            result = runtime.artifacts.inspect(
                subject,
                args.case_id,
                imported["result"]["artifact_id"],
                imported["revision"],
                str(uuid4()),
            )
        elif args.command == "doctor":
            result = doctor(runtime)
            result["mcp_tools"] = len(server._tool_manager.list_tools())
            if args.network:
                result["network"] = asyncio.run(
                    runtime.osm.features(
                        Bounds(south=51.500, west=-0.128, north=51.501, east=-0.127),
                        "bridges",
                        limit=1,
                    )
                )
        elif args.command == "config":
            result = {"status": "OK", "mode": settings.mode, "public_ready": False}
        elif args.command == "integrity":
            result = runtime.db.integrity(subject)
        elif args.command == "providers":
            result = runtime.osm.capabilities()
        elif args.command == "migrations":
            result = {"version": doctor(runtime)["schema_version"]}
        elif args.command == "case-create":
            result = runtime.service.create(subject, args.title, str(uuid4()))
        elif args.command == "case-export":
            result = runtime.service.export(subject, args.case_id, args.format)
        elif args.command == "geometry":
            result = geometry(TypeAdapter(GeometryInput).validate_json(args.calculation))
        elif args.command == "backup":
            runtime.db.backup(args.destination)
            result = {"status": "OK", "backup": "created"}
        else:
            raise ValueError("Неизвестная команда")
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print("GeoOSINT:")
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    except (GeoError, ValueError, OSError) as exc:
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "code": getattr(exc, "code", "INVALID_INPUT"),
                    "message": str(exc),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(1)
    finally:
        if runtime:
            runtime.close()


if __name__ == "__main__":
    main()
