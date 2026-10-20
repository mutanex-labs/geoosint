import io
import json
import subprocess
import sys

import pytest
from PIL import Image, TiffImagePlugin

from geoosint.core.metadata import inspect_bytes, xmp_gps
from geoosint.errors import GeoError


def rational(value, denominator=1):
    return TiffImagePlugin.IFDRational(value, denominator)


def gps_fields(latitude=55, longitude=37, lat_ref="N", lon_ref="E"):
    return {
        1: lat_ref,
        2: (rational(latitude), rational(45), rational(0)),
        3: lon_ref,
        4: (rational(longitude), rational(36), rational(0)),
        5: 1,
        6: rational(12),
        7: (rational(12), rational(30), rational(0)),
        29: "2024:06:01",
        16: "T",
        17: rational(90),
        31: rational(5),
        11: rational(2),
    }


def image_bytes(format="JPEG", gps=True, fields=None, xmp=None, exif_present=True):
    exif = Image.Exif()
    if exif_present:
        exif[271] = "Fixture Camera"
        exif[36867] = "2024:06:01 12:30:00"
        exif[42033] = "DO_NOT_RETURN_SERIAL"
        if gps:
            exif[34853] = fields if fields is not None else gps_fields()
    output = io.BytesIO()
    options = {"exif": exif.tobytes()} if exif_present else {}
    if xmp:
        options["xmp"] = xmp
    Image.new("RGB", (16, 16), (120, 80, 40)).save(output, format=format, **options)
    return output.getvalue()


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "TIFF"])
def test_supported_real_file_formats(format):
    content = image_bytes(format)
    result = inspect_bytes(content)
    assert result["detected_format"] == format
    assert result["latitude"] == 55.75
    assert result["longitude"] == 37.6
    assert result["altitude_m"] == -12
    assert result["gps_accuracy_m"] == 5
    assert result["gps_timestamp"] == "2024-06-01T12:30:00+00:00"
    assert result["timestamp_timezone_status"] == "OFFSET_MISSING"
    assert result["location_status"] == "METADATA_LOCATION"
    assert result["sources"][0]["gps_dop"] == 2
    assert result["external_requests"] == result["ai_calls"] == 0
    assert "DO_NOT_RETURN_SERIAL" not in json.dumps(result)


def test_no_exif_and_other_useful_fields():
    bare = inspect_bytes(image_bytes(exif_present=False))
    assert bare["metadata_status"] == "NO_GPS"
    assert bare["latitude"] is None
    assert bare["next_step"] == "CONTINUE_SCOPE_INVESTIGATION"
    without_gps = inspect_bytes(image_bytes(gps=False))
    assert without_gps["additional_metadata"]["Make"] == "Fixture Camera"
    assert without_gps["gps_present"] is False


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP", "TIFF"])
def test_metadata_does_not_decode_pixels_or_access_network(format, monkeypatch):
    import socket

    content = image_bytes(format)

    def forbidden(*args, **kwargs):
        raise AssertionError("Metadata-only must not decode all pixels or connect to network")

    monkeypatch.setattr(Image.Image, "load", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    assert inspect_bytes(content)["gps_valid"] is True


def test_south_west_and_zero():
    result = inspect_bytes(image_bytes(fields=gps_fields(lat_ref="S", lon_ref="W")))
    assert result["latitude"] == -55.75
    assert result["longitude"] == -37.6
    zero = gps_fields(latitude=0, longitude=0)
    zero[2] = zero[4] = (rational(0), rational(0), rational(0))
    result = inspect_bytes(image_bytes(fields=zero))
    assert result["gps_valid"] is True
    assert result["latitude"] == result["longitude"] == 0


@pytest.mark.parametrize(
    "change,expected",
    [
        ({1: "Q"}, "INVALID_GPS"),
        ({3: "N"}, "INVALID_GPS"),
        ({2: (rational(55), rational(0, 0), rational(0))}, "INVALID_GPS"),
        ({2: (rational(91), rational(0), rational(0))}, "INVALID_GPS"),
    ],
)
def test_invalid_gps_fields(change, expected):
    fields = {**gps_fields(), **change}
    result = inspect_bytes(image_bytes(fields=fields))
    assert result["gps_valid"] is False
    assert result["latitude"] is None
    assert result["metadata_status"] == expected


def test_incomplete_gps():
    fields = gps_fields()
    del fields[4]
    result = inspect_bytes(image_bytes(fields=fields))
    assert result["metadata_status"] == "INCOMPLETE_GPS"
    assert result["longitude"] is None


def test_xmp_conflict_and_safe_xml():
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" exif:GPSLatitude="10,0N" exif:GPSLongitude="20,0E"/></rdf:RDF></x:xmpmeta>'
    assert xmp_gps(xmp)["latitude"] == 10
    result = inspect_bytes(image_bytes(xmp=xmp))
    assert result["metadata_status"] == "CONFLICTING_GPS"
    assert result["latitude"] is None
    assert len([s for s in result["sources"] if s["gps_valid"]]) == 2
    malicious = b'<!DOCTYPE x [<!ENTITY xx SYSTEM "file:///etc/passwd">]><x>&xx;</x>'
    result = inspect_bytes(image_bytes(xmp=malicious))
    assert "XMP_INVALID_OR_UNSAFE" in result["warnings"]


def test_corrupt_unsupported_and_size_limits():
    for content in (b"not an image", b"\xff\xd8\xff\xe1\xff\xffinvalid", b"GIF89a broken"):
        with pytest.raises(GeoError):
            inspect_bytes(content)
    with pytest.raises(GeoError):
        inspect_bytes(image_bytes(), max_file_bytes=100)


def test_cli_wrong_extension_and_no_store(tmp_path):
    path = tmp_path / "wrong-extension.bin"
    path.write_bytes(image_bytes())
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "geoosint.cli.main",
            "metadata",
            "inspect",
            str(path),
            "--json",
            "--no-store",
        ],
        capture_output=True,
        check=True,
    )
    data = json.loads(result.stdout)
    assert data["detected_format"] == "JPEG"
    assert data["latitude"] == 55.75
    assert data["case_id"] is None


def test_artifact_preflight_isolation_deletion_no_network(runtime):
    svc = runtime.service
    cid = svc.create("a", "Metadata", "c")["id"]
    imported = runtime.artifacts.import_bytes("a", cid, 1, "upload", image_bytes())
    aid = imported["result"]["artifact_id"]
    assert imported["result"]["preflight"]["latitude"] == 55.75
    assert imported["result"]["preflight"]["evidence_id"]
    assert runtime.artifacts.import_bytes("a", cid, 1, "upload", image_bytes()) == imported
    for owner, artifact in (("b", aid), ("a", "fake"), ("a", "../../etc/passwd")):
        with pytest.raises(GeoError):
            runtime.artifacts.inspect(owner, cid, artifact, 2, "bad" + owner + artifact)
    checked = runtime.artifacts.inspect("a", cid, aid, 2, "inspect")
    result = checked["result"]
    assert result["latitude"] == 55.75
    assert result["evidence_id"]
    assert svc.read("a", cid)["state"] != "RESOLVED"
    assert svc.read("a", cid)["scope_mode"] == "AUTO"
    assert runtime.osm.requests == 0
    assert svc.read("a", cid)["evidence"][0]["applicability"] == "UNVERIFIED"
    runtime.artifacts.delete("a", cid, aid, 3, "delete")
    assert not (runtime.artifacts.root / aid).exists()
    report = svc.export("a", cid)
    assert "55.75" not in json.dumps(report)
    assert "37.6" not in json.dumps(report)
    assert "55.75" not in str(
        runtime.db.connection.execute("SELECT response FROM idempotency").fetchall()
    )


def test_case_delete_removes_original(runtime):
    cid = runtime.service.create("a", "Delete original", "c")["id"]
    imported = runtime.artifacts.import_bytes("a", cid, 1, "upload", image_bytes())
    aid = imported["result"]["artifact_id"]
    runtime.service.delete("a", cid, 2)
    assert not (runtime.artifacts.root / aid).exists()


def test_scene_conflict_does_not_make_exif_strict(runtime):
    from geoosint.cases.service import ObservationInput
    from geoosint.core.models import Bounds, CandidateInput

    svc = runtime.service
    cid = svc.create("a", "GPS versus scene", "c")["id"]
    imported = runtime.artifacts.import_bytes("a", cid, 1, "upload", image_bytes())
    aid = imported["result"]["artifact_id"]
    runtime.artifacts.inspect("a", cid, aid, 2, "gps")
    svc.observation(
        "a",
        cid,
        3,
        "scene",
        ObservationInput(
            category="TERRAIN",
            source="model",
            text="Предположительно пустыня; координаты файла кажутся несовместимыми со сценой",
            dependency_group="image:terrain",
            conflicts_with_artifact_ids=[aid],
        ),
    )
    doc = svc.read("a", cid)
    assert doc["state"] == "REVIEW_REQUIRED"
    assert doc["scope_mode"] == "AUTO"
    assert doc["evidence"][0]["review_status"] == "NEEDS_REVIEW"
    assert doc["evidence"][0]["result"]["location_status"] == "METADATA_LOCATION"
    svc.candidates(
        "a",
        cid,
        4,
        "alternative",
        [
            CandidateInput(
                label="Альтернатива вне GPS",
                level="AREA",
                area=Bounds(south=1, west=1, north=2, east=2),
            )
        ],
    )
    assert svc.next("a", cid)["candidate_ids"]
    with pytest.raises(GeoError):
        svc.finalize("a", cid, 5, "false-final", "POINT")
