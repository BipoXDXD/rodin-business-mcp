"""Network SSRF guards, safe filename normalization, and atomic disk writes."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
import re
import socket
from typing import Callable
from urllib.parse import urlsplit
from uuid import uuid4

DnsResolver = Callable[[str, int], list[tuple]]


def _is_global_ip(ip_str: str) -> bool:
    addr = ipaddress.ip_address(ip_str)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_global and not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _default_resolver(host: str, port: int) -> list[tuple]:
    return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)


def validate_safe_https_url(
    url: str,
    *,
    resolve_dns: bool = True,
    resolver: DnsResolver = _default_resolver,
) -> str:
    """Validate an external HTTPS URL and reject private, loopback, or reserved destinations."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Download/upload URL must be a non-empty HTTPS string")
    if any(ch in url for ch in ("\x00", "\r", "\n")):
        raise ValueError("URL contains forbidden control characters")

    parsed = urlsplit(url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Only HTTPS URLs with a valid hostname are allowed")
    if parsed.username or parsed.password:
        raise ValueError("Embedded credentials in URL are not allowed")
    if parsed.port not in (None, 443):
        raise ValueError("Only standard HTTPS port 443 is allowed")

    host = parsed.hostname.lower().rstrip(".")
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        raise ValueError("Localhost targets are not allowed")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if not _is_global_ip(str(literal_ip)):
            raise ValueError("Private or reserved IP addresses are not allowed")
        return url.strip()

    if resolve_dns:
        try:
            infos = resolver(host, 443)
        except OSError as exc:
            raise ValueError(f"Failed to resolve host '{host}'") from exc
        if not infos:
            raise ValueError(f"Host '{host}' resolved to no addresses")
        for info in infos:
            sockaddr = info[4]
            if not sockaddr or not _is_global_ip(str(sockaddr[0])):
                raise ValueError("Host resolves to a private or reserved IP address")

    return url.strip()


def safe_filename_component(raw: str, *, fallback: str, max_len: int = 120) -> str:
    """Strip path separators and unsafe characters from an untrusted filename component."""
    base = Path(str(raw or "")).name.strip()
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip(".")
    if not cleaned:
        cleaned = fallback
    return cleaned[:max_len]


def atomic_write_bytes(target: Path, content: bytes, *, mode: int | None = None) -> Path:
    """Atomically write bytes via a sibling temporary file in the same directory."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.part")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_mode = mode if mode is not None else 0o644
        fd = os.open(str(temp), flags, file_mode)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        except Exception:
            raise
        temp.replace(target)
        if mode is not None:
            os.chmod(target, mode)
        return target
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
