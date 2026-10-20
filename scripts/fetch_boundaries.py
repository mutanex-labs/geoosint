"""Получить три фиксированных открытых полигона; запуск оператором, не MCP fetch."""

import hashlib
import json
from pathlib import Path

import httpx
from shapely import make_valid
from shapely.geometry import mapping, shape

from geoosint.core.scope import parse_geometry
from geoosint.serialization import canonical, digest, now

ROOT = Path("data-sources/osm-boundaries")
AREAS = [
    (2555133, "CITY", "Москва"),
    (51490, "REGION", "Московская область"),
    (60189, "COUNTRY", "Россия"),
    (72197, "REGION", "Владимирская область"),
]


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    areas = []
    for relation, kind, name in AREAS:
        path = ROOT / f"{relation}.geojson"
        source_path = ROOT / f"{relation}.source.json"
        url = f"https://polygons.openstreetmap.fr/get_geojson.py?id={relation}&params=0"
        if not path.exists():
            with (
                httpx.Client(timeout=60, follow_redirects=False) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > 8 * 1024 * 1024:
                        raise ValueError("Превышен лимит загрузки границы")
            path.write_bytes(content)
            source_path.write_text(
                canonical(
                    {
                        "source": url,
                        "retrieved_at": now(),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "license": "ODbL-1.0",
                        "attribution": "OpenStreetMap contributors",
                        "relation_id": relation,
                    }
                ),
                encoding="utf-8",
            )
        provenance = json.loads(source_path.read_text(encoding="utf-8"))
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != provenance["sha256"]:
            raise ValueError("Изменён исходный файл границы")
        geometry = json.loads(raw)
        polygons = (
            geometry["coordinates"]
            if geometry["type"] == "MultiPolygon"
            else [geometry["coordinates"]]
        )
        normalized = []
        repairs = []
        for index, coordinates in enumerate(polygons):
            polygon = shape({"type": "Polygon", "coordinates": coordinates})
            # Не упрощаем координаты: self-touching ring разбивается на валидные компоненты.
            if not polygon.is_valid:
                repaired = make_valid(polygon)
                pieces = list(repaired.geoms) if hasattr(repaired, "geoms") else [repaired]
                if any(piece.geom_type != "Polygon" for piece in pieces):
                    raise ValueError("Ремонт потерял площадную геометрию")
                normalized.extend(mapping(piece)["coordinates"] for piece in pieces)
                repairs.append(
                    {
                        "component": index,
                        "method": "shapely.make_valid",
                        "area_delta_degrees2": repaired.area - polygon.area,
                    }
                )
            else:
                normalized.append(coordinates)
        parsed = parse_geometry({"type": "MultiPolygon", "coordinates": normalized}, registry=True)
        output = parsed.model_dump(mode="json")
        areas.append(
            {
                **provenance,
                "object_id": f"osm:relation/{relation}",
                "kind": kind,
                "name": name,
                "parent_ids": [] if kind == "COUNTRY" else ["osm:relation/60189"],
                "geometry": output,
                "content_hash": digest(output),
                "geometry_processing": repairs,
                "coverage_limitations": [
                    "Снимок OSM, не кадастровая или юридическая граница",
                    "Спорные границы отражают данные источника",
                    "Без упрощения; исправления топологии перечислены отдельно",
                ],
            }
        )
        print(f"{relation}: {len(raw)} bytes; repaired components: {len(repairs)}")
    destination = ROOT / "registry.json"
    destination.write_text(canonical({"areas": areas}), encoding="utf-8")
    print(f"Registry: {destination}")


if __name__ == "__main__":
    main()
