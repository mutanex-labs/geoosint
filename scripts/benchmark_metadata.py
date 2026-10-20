"""Воспроизводимый замер metadata-only на синтетическом JPEG без сетевых запросов."""

import io
import json
import os
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, TiffImagePlugin

from geoosint.core.metadata import inspect_bytes

try:
    import psutil
except ImportError:
    psutil = None

PROCESS_ERRORS = (OSError, StopIteration) + ((psutil.Error,) if psutil else ())


def metrics(values):
    values = sorted(values)
    return {
        "samples": len(values),
        "p50_ms": statistics.median(values),
        "p95_ms": values[int(len(values) * 0.95) - 1],
    }


def main():
    directory = Path(".cache/metadata-benchmark")
    directory.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    r = TiffImagePlugin.IFDRational
    exif[34853] = {1: "N", 2: (r(0), r(0), r(0)), 3: "E", 4: (r(0), r(0), r(0))}
    output = io.BytesIO()
    Image.effect_noise((1920, 1080), 80).convert("RGB").save(
        output, format="JPEG", quality=90, exif=exif.tobytes()
    )
    content = output.getvalue()
    path = directory / "generated.jpg"
    path.write_bytes(content)
    for _ in range(25):
        inspect_bytes(content)
    phases = {k: [] for k in ("identify_format", "read_metadata", "normalize_gps", "total")}
    for _ in range(1000):
        result = inspect_bytes(content)
        assert result["gps_valid"] and result["external_requests"] == result["ai_calls"] == 0
        for key in phases:
            phases[key].append(result["timings_ms"][key])
    isolated, peak = [], 0
    for _ in range(50):
        started = time.perf_counter()
        with subprocess.Popen(
            [
                sys.executable,
                "-m",
                "geoosint.core.metadata_worker",
                str(path),
                str(25 * 1024 * 1024),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as child:
            while child.poll() is None:
                try:
                    if psutil:
                        process = psutil.Process(child.pid)
                        rss = sum(
                            p.memory_info().rss
                            for p in [process, *process.children(recursive=True)]
                        )
                    else:
                        status = Path(f"/proc/{child.pid}/status").read_text()
                        rss = (
                            int(
                                next(
                                    line.split()[1]
                                    for line in status.splitlines()
                                    if line.startswith("VmRSS:")
                                )
                            )
                            * 1024
                        )
                    peak = max(peak, rss)
                except PROCESS_ERRORS:
                    pass
                time.sleep(0.002)
            response, _ = child.communicate()
            assert child.returncode == 0
        assert json.loads(response)["data"]["gps_valid"]
        isolated.append((time.perf_counter() - started) * 1000)
    data = {
        "generated_file_bytes": len(content),
        "dimensions": [1920, 1080],
        "direct_warmed": {key: metrics(values) for key, values in phases.items()},
        "isolated_subprocess": metrics(isolated),
        "peak_worker_rss_bytes": peak,
        "parent_rss_bytes": psutil.Process().memory_info().rss if psutil else None,
        "external_requests": 0,
        "ai_calls": 0,
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "logical_cpus": os.cpu_count(),
            "concurrency": 1,
        },
        "limitations": [
            "Синтетический JPEG; это не тест подлинности и не слепая геолокация",
            "RSS дочернего процесса наблюдается с шагом 2 мс; короткий пик мог быть пропущен",
            "Изолированный subprocess включает запуск интерпретатора и чтение файла",
        ],
    }
    Path("docs").mkdir(exist_ok=True)
    Path("docs/performance-metadata.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
