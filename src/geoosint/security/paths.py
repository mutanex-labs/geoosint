from pathlib import Path
from uuid import UUID


def artifact_path(root: Path, identifier: str) -> Path:
    if str(UUID(identifier)) != identifier:
        raise ValueError("Нужен канонический UUID")
    root = root.resolve(strict=True)
    target = root / identifier
    if target.is_symlink() or target.resolve().parent != root:
        raise ValueError("Недопустимый путь артефакта")
    return target
