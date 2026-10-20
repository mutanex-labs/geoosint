"""Локальные замеры без сети; вывод в JSON, без утверждений о другом оборудовании."""

import argparse
import asyncio
import json
import os
import platform
import statistics
import time

import psutil
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from geoosint.core.geometry import geometry
from geoosint.core.models import InverseInput, Point


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "p50_ms": statistics.median(samples),
        "p95_ms": ordered[int(len(ordered) * 0.95) - 1],
    }


async def bench(url, count, concurrency):
    calculation = InverseInput(
        operation="inverse",
        start=Point(latitude=55, longitude=37),
        end=Point(latitude=56, longitude=38),
    )
    for _ in range(100):
        geometry(calculation)
    samples = []
    for _ in range(count):
        start = time.perf_counter()
        geometry(calculation)
        samples.append((time.perf_counter() - start) * 1000)
    results = {"geometry": summary(samples)}
    if url:

        async def worker():
            values = []
            sizes = []
            async with streamable_http_client(url) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for _ in range(10):
                        await session.call_tool("geo_capabilities")
                    for _ in range(count // concurrency):
                        start = time.perf_counter()
                        result = await session.call_tool("geo_capabilities")
                        assert result.structuredContent["status"] == "OK"
                        values.append((time.perf_counter() - start) * 1000)
                        sizes.append(len(result.model_dump_json().encode("utf-8")))
            return values, sizes

        clients = await asyncio.gather(*(worker() for _ in range(concurrency)))
        results["mcp"] = summary([s for client, _ in clients for s in client])
        sizes = [s for _, sizes in clients for s in sizes]
        results["mcp"]["tool_calls_measured"] = len(sizes)
        results["mcp"]["response_bytes_total"] = sum(sizes)
        results["mcp"]["response_bytes_p50"] = statistics.median(sizes)
        results["mcp"]["cloud_model_tokens"] = "NOT_MEASURED"
    results["environment"] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "concurrent_clients": concurrency,
        "rss_client_bytes": psutil.Process().memory_info().rss,
        "memory_method": "psutil RSS benchmark client; server/container measured separately",
    }
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    parser.add_argument("--count", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(bench(args.url, args.count, args.concurrency)), indent=2))
