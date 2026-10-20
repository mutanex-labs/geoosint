import json
import sys
from pathlib import Path

from geoosint.core.metadata import inspect_bytes
from geoosint.errors import GeoError


def main() -> None:
    try:
        path = Path(sys.argv[1])
        maximum = int(sys.argv[2])
        with path.open("rb") as stream:
            content = stream.read(maximum + 1)
        result = {"status": "OK", "data": inspect_bytes(content, maximum)}
    except GeoError as exc:
        result = {"status": "ERROR", "code": exc.code, "message": str(exc)}
    except Exception:
        result = {"status": "ERROR", "code": "INVALID_FILE", "message": "Ошибка чтения метаданных"}
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
