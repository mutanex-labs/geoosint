import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from geoosint.errors import GeoError

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_ENDPOINTS = (OVERPASS_URL, "https://overpass.private.coffee/api/interpreter")


def validate_addresses(addresses: list[str]) -> None:
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise GeoError("FORBIDDEN", "Провайдер разрешается во внутренний или недопустимый адрес")


async def pinned_endpoint(url: str) -> tuple[str, str]:
    if url not in OVERPASS_ENDPOINTS:
        raise GeoError("FORBIDDEN", "URL не входит в список разрешённых провайдеров")
    parsed = urlsplit(url)
    hostname = parsed.hostname
    assert hostname is not None
    records = await asyncio.get_running_loop().getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    addresses = sorted({r[4][0] for r in records})
    validate_addresses(addresses)
    address = addresses[0]
    if ":" in address:
        address = f"[{address}]"
    return f"https://{address}{parsed.path}", hostname
