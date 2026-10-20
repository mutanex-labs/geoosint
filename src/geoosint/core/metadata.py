"""Metadata-only extraction. No pixel decoding and no network clients."""

import hashlib
import io
import math
import re
import time
import warnings
from datetime import UTC, datetime
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml import ElementTree
from PIL import Image, UnidentifiedImageError
from PIL import __version__ as pillow_version

from geoosint.errors import GeoError

FORMATS = ("JPEG", "PNG", "TIFF", "WEBP")
MAX_METADATA_BYTES = 1024 * 1024
Image.MAX_IMAGE_PIXELS = 40_000_000
SAFE_TAGS = {
    271: "Make",
    272: "Model",
    305: "Software",
    274: "Orientation",
    37386: "FocalLength",
    41989: "FocalLengthIn35mmFormat",
    42036: "LensModel",
    40961: "ColorSpace",
    36867: "DateTimeOriginal",
    36868: "CreateDate",
    36881: "OffsetTimeOriginal",
    36882: "OffsetTimeDigitized",
}


def number(value: Any) -> float:
    if isinstance(value, (tuple, list)) and len(value) == 2:
        numerator, denominator = value
        if float(denominator) == 0:
            raise ValueError("Нулевой знаменатель")
        result = float(numerator) / float(denominator)
    else:
        result = float(value)
    if not math.isfinite(result):
        raise ValueError("Неконечное число")
    return result


def reference(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("ascii").rstrip("\x00")
    return str(value).rstrip("\x00")


def dms(value: Any, ref: str, latitude: bool) -> float:
    if ref not in (("N", "S") if latitude else ("E", "W")) or len(value) != 3:
        raise ValueError("Неверный GPSRef или DMS")
    degrees, minutes, seconds = [number(v) for v in value]
    if degrees < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        raise ValueError("Неверный DMS")
    result = degrees + minutes / 60 + seconds / 3600
    if result > (90 if latitude else 180):
        raise ValueError("GPS вне диапазона")
    return -result if ref in ("S", "W") else result


def exif_gps(gps: dict[int, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": "EXIF",
        "gps_present": bool(gps),
        "gps_valid": False,
        "latitude": None,
        "longitude": None,
        "altitude_m": None,
        "gps_timestamp": None,
        "gps_accuracy_m": None,
        "gps_dop": None,
        "image_direction_deg": None,
        "image_direction_ref": None,
        "warnings": [],
        "status": "NO_GPS",
    }
    if not gps:
        return result
    if not all(k in gps for k in (1, 2, 3, 4)):
        result["status"] = "INCOMPLETE_GPS"
        return result
    try:
        latitude = dms(gps[2], reference(gps[1]), True)
        longitude = dms(gps[4], reference(gps[3]), False)
        result.update(
            latitude=latitude, longitude=longitude, gps_valid=True, status="METADATA_VALID"
        )
    except (ValueError, TypeError, ZeroDivisionError, OverflowError, UnicodeError):
        result["status"] = "INVALID_GPS"
        return result
    for tag, field in (
        (6, "altitude_m"),
        (31, "gps_accuracy_m"),
        (11, "gps_dop"),
        (17, "image_direction_deg"),
    ):
        if tag not in gps:
            continue
        try:
            value = number(gps[tag])
            if value < 0 or (tag == 17 and value >= 360):
                raise ValueError("Недопустимое поле")
            if tag == 6:
                ref = gps.get(5)
                if isinstance(ref, bytes):
                    ref = ref[0] if ref else None
                if ref not in (0, 1):
                    raise ValueError("Неизвестный AltitudeRef")
                value *= -1 if ref == 1 else 1
            result[field] = value
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            result["warnings"].append("INVALID_" + field.upper())
    if 16 in gps:
        ref = reference(gps[16])
        if ref in ("T", "M"):
            result["image_direction_ref"] = ref
        else:
            result["warnings"].append("INVALID_DIRECTION_REF")
    if 29 in gps and 7 in gps:
        try:
            date = reference(gps[29])
            hour, minute, second = [number(v) for v in gps[7]]
            if hour != int(hour) or minute != int(minute) or not 0 <= second < 60:
                raise ValueError("Неверное время")
            stamp = datetime.strptime(date, "%Y:%m:%d").replace(
                hour=int(hour),
                minute=int(minute),
                second=int(second),
                microsecond=round((second - int(second)) * 1_000_000),
                tzinfo=UTC,
            )
            result["gps_timestamp"] = stamp.isoformat()
        except (ValueError, TypeError, ZeroDivisionError, OverflowError):
            result["warnings"].append("INVALID_GPS_TIMESTAMP")
    return result


def xmp_gps(raw: bytes | str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    if len(raw) > MAX_METADATA_BYTES:
        raise ValueError("Слишком большой XMP")
    root = ElementTree.fromstring(raw)
    values: dict[str, str] = {}
    elements = list(root.iter())
    if len(elements) > 2048:
        raise ValueError("Слишком сложный XMP")

    def depth(node: Element, level: int = 0) -> None:
        if level > 20:
            raise ValueError("Слишком глубокий XMP")
        for child in node:
            depth(child, level + 1)

    depth(root)
    namespace = "{http://ns.adobe.com/exif/1.0/}"
    for element in elements:
        for key, val in element.attrib.items():
            if key.startswith(namespace):
                values[key[len(namespace) :]] = val
        if element.tag.startswith(namespace) and element.text:
            values[element.tag[len(namespace) :]] = element.text.strip()
    if not any(k in values for k in ("GPSLatitude", "GPSLongitude")):
        return None
    result = {
        "source": "XMP",
        "gps_present": True,
        "gps_valid": False,
        "latitude": None,
        "longitude": None,
        "status": "INCOMPLETE_GPS",
    }
    if not all(k in values for k in ("GPSLatitude", "GPSLongitude")):
        return result

    def coordinate(text: str, latitude: bool) -> float:
        match = re.fullmatch(
            r"\s*(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?([NSEW])\s*", text
        )
        if not match:
            raise ValueError("Неподдержанный XMP GPS формат")
        deg, minute, sec, ref = match.groups()
        return dms((float(deg), float(minute), float(sec or 0)), ref, latitude)

    try:
        result.update(
            latitude=coordinate(values["GPSLatitude"], True),
            longitude=coordinate(values["GPSLongitude"], False),
            gps_valid=True,
            status="METADATA_VALID",
        )
    except ValueError:
        result["status"] = "INVALID_GPS"
    return result


def inspect_bytes(content: bytes, max_file_bytes: int = 25 * 1024 * 1024) -> dict[str, Any]:
    started = time.perf_counter()
    if not content or len(content) > max_file_bytes:
        raise GeoError("INVALID_FILE", "Файл пуст или превышает лимит")
    result: dict[str, Any] = {
        "case_id": None,
        "artifact_id": None,
        "detected_format": None,
        "original_sha256": hashlib.sha256(content).hexdigest(),
        "metadata_status": "NO_GPS",
        "gps_present": False,
        "gps_valid": False,
        "latitude": None,
        "longitude": None,
        "altitude_m": None,
        "gps_timestamp": None,
        "capture_timestamp": None,
        "timestamp_timezone_status": "UNKNOWN",
        "gps_accuracy_m": None,
        "parser_name": "Pillow+defusedxml",
        "parser_version": pillow_version,
        "warnings": [],
        "conflicts": [],
        "evidence_id": None,
        "sources": [],
        "method": None,
        "location_status": "UNVERIFIED",
        "additional_metadata": {},
        "external_requests": 0,
        "ai_calls": 0,
        "provenance": {},
    }
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content), formats=list(FORMATS)) as image:
                identified = time.perf_counter()
                result["detected_format"] = image.format
                if image.width * image.height > 40_000_000:
                    raise ValueError("Слишком большое изображение")
                exif = image.getexif()
                raw_exif = image.info.get("exif", b"")
                if len(raw_exif) > MAX_METADATA_BYTES or len(exif) > 512:
                    raise ValueError("Слишком много метаданных")
                gps = exif.get_ifd(34853) if 34853 in exif else {}
                details = {**dict(exif), **(exif.get_ifd(34665) if 34665 in exif else {})}
                xmp = image.info.get("xmp") or image.info.get("XML:com.adobe.xmp")
                if not xmp and image.format == "TIFF" and 700 in exif:
                    xmp = bytes(exif[700])
                metadata_read = time.perf_counter()
                additional: dict[str, Any] = {
                    name: str(details[tag])[:500]
                    for tag, name in SAFE_TAGS.items()
                    if tag in details
                }
                additional.update(ImageWidth=image.width, ImageHeight=image.height)
                result["additional_metadata"] = additional
                result["provenance"] = {key: "EXIF:" + key for key in additional}
                if image.info.get("exif") and not exif:
                    result["warnings"].append("EXIF_UNREADABLE")
                sources = [exif_gps(gps)]
                try:
                    xmp_result = xmp_gps(xmp)
                    if xmp_result:
                        sources.append(xmp_result)
                except Exception:
                    result["warnings"].append("XMP_INVALID_OR_UNSAFE")
                result["sources"] = sources
                result["gps_present"] = any(s["gps_present"] for s in sources)
                valid = [s for s in sources if s["gps_valid"]]
                if valid:
                    first = valid[0]
                    if any(
                        abs(s["latitude"] - first["latitude"]) > 1e-7
                        or abs(s["longitude"] - first["longitude"]) > 1e-7
                        for s in valid[1:]
                    ):
                        result["conflicts"].append("EXIF_XMP_GPS_CONFLICT")
                        result["metadata_status"] = "CONFLICTING_GPS"
                    else:
                        for key in (
                            "latitude",
                            "longitude",
                            "altitude_m",
                            "gps_timestamp",
                            "gps_accuracy_m",
                        ):
                            result[key] = first.get(key)
                            result["provenance"][key] = first["source"]
                        result.update(
                            gps_valid=True,
                            metadata_status="METADATA_VALID",
                            method="EMBEDDED_GPS",
                            location_status="METADATA_LOCATION",
                        )
                elif result["gps_present"]:
                    result["metadata_status"] = next(
                        s["status"] for s in sources if s["gps_present"]
                    )
                for source in sources:
                    result["warnings"].extend(source.get("warnings", []))
                original_time = additional.get("DateTimeOriginal") or additional.get("CreateDate")
                offset = additional.get("OffsetTimeOriginal") or additional.get(
                    "OffsetTimeDigitized"
                )
                if original_time:
                    try:
                        parsed = datetime.strptime(original_time, "%Y:%m:%d %H:%M:%S")
                        if offset:
                            parsed = datetime.fromisoformat(parsed.isoformat() + offset)
                        result["capture_timestamp"] = parsed.isoformat()
                        result["timestamp_timezone_status"] = (
                            "OFFSET_PRESENT" if parsed.tzinfo else "OFFSET_MISSING"
                        )
                    except ValueError:
                        result["warnings"].append("INVALID_CAPTURE_TIMESTAMP")
                image.verify()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise GeoError(
            "UNSUPPORTED_OR_INVALID_FILE",
            "Формат не поддерживается или структура файла небезопасна",
        ) from exc
    except (ValueError, OSError, SyntaxError, TypeError, OverflowError) as exc:
        raise GeoError("INVALID_FILE", "Повреждённый файл или недопустимые метаданные") from exc
    finished = time.perf_counter()
    result["timings_ms"] = {
        "identify_format": (identified - started) * 1000,
        "read_metadata": (metadata_read - identified) * 1000,
        "normalize_gps": (finished - metadata_read) * 1000,
        "total": (finished - started) * 1000,
    }
    result["warnings"].append("EMBEDDED_GPS_DOES_NOT_VERIFY_SCENE")
    result["next_step"] = (
        "REVIEW_METADATA_LOCATION" if result["gps_valid"] else "CONTINUE_SCOPE_INVESTIGATION"
    )
    return result
