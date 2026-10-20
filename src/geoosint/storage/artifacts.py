import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from geoosint.errors import GeoError
from geoosint.security.paths import artifact_path
from geoosint.storage.database import digest, now


def inspect_file_isolated(path: Path, maximum: int) -> dict:
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "geoosint.core.metadata_worker", str(path), str(maximum)],
            timeout=5,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        result = json.loads(completed.stdout)
    except subprocess.TimeoutExpired as exc:
        raise GeoError("METADATA_TIMEOUT", "Парсер превысил время обработки") from exc
    except (subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise GeoError("INVALID_FILE", "Парсер не смог обработать файл") from exc
    if result["status"] != "OK":
        raise GeoError(result["code"], result["message"])
    data = result["data"]
    data["timings_ms"]["preflight_end_to_end"] = (time.perf_counter() - started) * 1000
    return data


class ArtifactStore:
    def __init__(self, service):
        self.service = service
        self.root = service.settings.data_dir / "artifacts"
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError("Артефакты не могут быть symlink")
        self.parser_gate = threading.BoundedSemaphore(2)

    def inspect_isolated(self, path: Path) -> dict:
        with self.parser_gate:
            return inspect_file_isolated(path, self.service.settings.max_upload_bytes)

    def get(self, owner: str, case_id: str, artifact_id: str) -> tuple[Path, dict]:
        self.service.read(owner, case_id)
        with self.service.db.lock:
            row = self.service.db.connection.execute(
                "SELECT * FROM artifacts WHERE id=? AND case_id=? AND owner=?",
                (artifact_id, case_id, owner),
            ).fetchone()
        if not row:
            raise GeoError("NOT_FOUND", "Артефакт не найден")
        try:
            path = artifact_path(self.root, artifact_id)
        except ValueError as exc:
            raise GeoError("NOT_FOUND", "Артефакт не найден") from exc
        if not path.is_file():
            raise GeoError("NOT_FOUND", "Исходный файл недоступен")
        return path, dict(row)

    def import_bytes(
        self, owner: str, case_id: str, revision: int, key: str, content: bytes
    ) -> dict:
        if not content or len(content) > self.service.settings.max_upload_bytes:
            raise GeoError("INVALID_FILE", "Файл пуст или превышает лимит")
        import hashlib

        content_hash = hashlib.sha256(content).hexdigest()
        parameters = {"sha256": content_hash, "size_bytes": len(content)}
        replay = self.service.preflight(
            owner, case_id, revision, key, "artifact_upload", parameters
        )
        if replay is not None:
            return replay
        artifact_id = str(uuid4())
        path = artifact_path(self.root, artifact_id)
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            metadata = self.inspect_isolated(path)

            def change(doc):
                count = self.service.db.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE case_id=? AND owner=?", (case_id, owner)
                ).fetchone()[0]
                if count >= self.service.settings.max_artifacts_per_case:
                    raise GeoError("RATE_LIMITED", "Достигнут лимит артефактов")
                self.service.db.connection.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        case_id,
                        owner,
                        now(),
                        content_hash,
                        len(content),
                        metadata["detected_format"],
                    ),
                )
                doc["image_received"] = True
                preflight = self._register_metadata(doc, case_id, artifact_id, metadata)
                return {
                    "artifact_id": artifact_id,
                    "original_sha256": content_hash,
                    "detected_format": metadata["detected_format"],
                    "preflight": preflight,
                }

            result = self.service.mutate(
                owner, case_id, revision, key, "artifact_upload", parameters, change
            )
            if result["result"]["artifact_id"] != artifact_id:
                path.unlink()
            return result
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _register_metadata(self, doc: dict, case_id: str, artifact_id: str, metadata: dict) -> dict:
        evidence_id = str(uuid4())
        metadata.update(case_id=case_id, artifact_id=artifact_id, evidence_id=evidence_id)
        metadata["review_status"] = (
            "NEEDS_REVIEW"
            if any(
                artifact_id in observation.get("conflicts_with_artifact_ids", [])
                for observation in doc["observations"]
            )
            else "UNREVIEWED"
        )
        item = {
            "id": evidence_id,
            "type": "EMBEDDED_METADATA",
            "source": "ORIGINAL_FILE",
            "artifact_id": artifact_id,
            "created_at": now(),
            "content_hash": digest(metadata),
            "dependency_groups": ["artifact:" + artifact_id],
            "verification": "METADATA_VALID"
            if metadata["gps_valid"]
            else "METADATA_PRESENT"
            if metadata["gps_present"]
            else "METADATA_ABSENT",
            "applicability": "UNVERIFIED",
            "review_status": metadata["review_status"],
            "candidate_id": None,
            "effect": "CONTEXT",
            "result": metadata,
            "limitations": ["GPS может быть подделан или относиться к другой сцене"],
        }
        self.service._quota(doc, "evidence", self.service.settings.max_evidence)
        doc["evidence"].append(item)
        doc.setdefault("preflight", []).append(
            {
                "artifact_id": artifact_id,
                "evidence_id": evidence_id,
                "metadata_status": metadata["metadata_status"],
                "completed_at": now(),
            }
        )
        # GPS — гипотеза; search_scope не меняется, STRICT не возникает из EXIF.
        return metadata

    def inspect(self, owner: str, case_id: str, artifact_id: str, revision: int, key: str) -> dict:
        parameters = {"artifact_id": artifact_id}
        replay = self.service.preflight(
            owner, case_id, revision, key, "metadata_inspect", parameters
        )
        if replay is not None:
            return replay
        path, row = self.get(owner, case_id, artifact_id)
        metadata = self.inspect_isolated(path)
        if metadata["original_sha256"] != row["sha256"]:
            raise GeoError("ARTIFACT_CHANGED", "Нарушена неизменность оригинала")

        def change(doc):
            return self._register_metadata(doc, case_id, artifact_id, metadata)

        return self.service.mutate(
            owner, case_id, revision, key, "metadata_inspect", parameters, change
        )

    def delete(self, owner: str, case_id: str, artifact_id: str, revision: int, key: str) -> dict:
        replay = self.service.preflight(
            owner, case_id, revision, key, "artifact_delete", artifact_id
        )
        if replay is not None:
            return replay
        path, _ = self.get(owner, case_id, artifact_id)

        def change(doc):
            self.service.db.connection.execute(
                "DELETE FROM artifacts WHERE id=? AND case_id=? AND owner=?",
                (artifact_id, case_id, owner),
            )
            self.service.db.connection.execute(
                "DELETE FROM idempotency WHERE case_id=? AND owner=?", (case_id, owner)
            )
            for evidence in doc["evidence"]:
                if evidence.get("artifact_id") == artifact_id:
                    evidence.clear()
                    evidence.update(
                        id=str(uuid4()),
                        type="DELETED_ARTIFACT",
                        artifact_id=artifact_id,
                        candidate_id=None,
                        reproducible=False,
                    )
            doc["image_received"] = bool(
                self.service.db.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE case_id=? AND owner=?", (case_id, owner)
                ).fetchone()[0]
            )
            path.unlink()
            return {"deleted": artifact_id, "reproducible": False}

        return self.service.mutate(
            owner, case_id, revision, key, "artifact_delete", artifact_id, change
        )

    def delete_case_files(self, owner: str, case_id: str) -> None:
        with self.service.db.lock:
            rows = self.service.db.connection.execute(
                "SELECT id FROM artifacts WHERE case_id=? AND owner=?", (case_id, owner)
            ).fetchall()
        for row in rows:
            artifact_path(self.root, row[0]).unlink(missing_ok=True)
