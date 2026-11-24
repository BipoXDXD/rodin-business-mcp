"""Safe streaming downloads and base64 image persistence."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
import re
from typing import Any, Callable
from uuid import uuid4

import requests

from .security import DnsResolver, atomic_write_bytes, safe_filename_component, validate_safe_https_url
from .validators import detect_image_extension

GetFn = Callable[..., requests.Response]
DOWNLOAD_CHUNK_BYTES = 1024 * 1024


def download_signed_files(
    items: list[dict[str, Any]],
    *,
    task_uuid: str,
    output_dir: Path,
    timeout: tuple[float, float] = (10.0, 120.0),
    get_fn: GetFn = requests.get,
    resolve_dns: bool = True,
    resolver: DnsResolver | None = None,
) -> list[dict[str, Any]]:
    """Download signed result URLs atomically with SSRF and redirect protection."""
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_task_id = re.sub(r"[^A-Za-z0-9_-]", "-", task_uuid)[:64]
    saved: list[dict[str, Any]] = []

    for index, item in enumerate(items, start=1):
        url = str(item.get("url", ""))
        validate_kwargs: dict[str, Any] = {"resolve_dns": resolve_dns}
        if resolver is not None:
            validate_kwargs["resolver"] = resolver
        safe_url = validate_safe_https_url(url, **validate_kwargs)

        raw_name = str(item.get("name", ""))
        clean_name = safe_filename_component(raw_name, fallback=f"result-{index}.bin", max_len=140)
        target = output_dir / f"{safe_task_id}-{index}-{clean_name}"
        temp = target.with_name(f".{target.name}.{uuid4().hex}.part")

        try:
            with get_fn(safe_url, stream=True, timeout=timeout, allow_redirects=False) as res:
                if 300 <= getattr(res, "status_code", 200) < 400:
                    raise RuntimeError("Download redirects are not permitted")
                res.raise_for_status()
                with temp.open("wb") as out:
                    for chunk in res.iter_content(DOWNLOAD_CHUNK_BYTES):
                        if chunk:
                            out.write(chunk)
            temp.replace(target)
            saved.append({"path": str(target), "bytes": target.stat().st_size, "name": clean_name})
        finally:
            if temp.exists():
                temp.unlink(missing_ok=True)

    return saved


def save_decoded_image(
    item: dict[str, Any],
    *,
    output_dir: Path,
) -> dict[str, Any]:
    """Decode a base64 PNG/JPEG/WebP image response and write it atomically."""
    try:
        content = base64.b64decode(item["b64_json"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise RuntimeError(
            "Invalid base64 image in API response; check history before retrying."
        ) from exc

    suffix = detect_image_extension(content)
    target = output_dir / f"image-{uuid4().hex}{suffix}"
    try:
        atomic_write_bytes(target, content)
    except OSError as exc:
        raise RuntimeError(
            "The API succeeded, but local file write failed; do not automatically repeat the paid call."
        ) from exc

    return {
        "path": str(target),
        "bytes": len(content),
        "revised_prompt": item.get("revised_prompt"),
    }
