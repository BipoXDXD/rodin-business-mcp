"""Parse-don't-validate input parsers aligned with Hyper3D OpenAPI v3 contracts."""

from __future__ import annotations

import math
from pathlib import Path
import re
from typing import Any, Iterable
from uuid import UUID

from .config import (
    IMAGE_EXTENSIONS,
    IMAGE_LABELS,
    MAX_IMAGE_SIZE,
    SYNC_IMAGE_EXTENSIONS,
)

MAX_PROMPT_LENGTH = 1024
MAX_RIG_HIERARCHY_NODES = 256
SEED_MIN = 0
SEED_MAX = 65535
BBOX_MIN = 1
BBOX_MAX = 2048
MIN_IMAGE_PIXELS = 512 * 512
MAX_IMAGE_PIXELS = 2048 * 2048
SIZE_PATTERN = re.compile(r"([1-9][0-9]*)x([1-9][0-9]*)")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def parse_uuid(value: str, field_name: str = "uuid") -> str:
    require(isinstance(value, str) and bool(value.strip()), f"{field_name} is required")
    cleaned = value.strip()
    try:
        UUID(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid UUID") from exc
    return cleaned


def parse_optional_uuid(value: str | None, field_name: str = "asset_id") -> str | None:
    if value is None:
        return None
    require(isinstance(value, str), f"{field_name} must be a string")
    cleaned = value.strip()
    if not cleaned:
        return None
    return parse_uuid(cleaned, field_name)


def parse_prompt(prompt: str, *, required: bool = False, max_length: int = MAX_PROMPT_LENGTH) -> str:
    require(isinstance(prompt, str), "prompt must be a string")
    require("\x00" not in prompt, "prompt must not contain NUL bytes")
    cleaned = prompt.strip()
    require(not required or bool(cleaned), "prompt is required")
    require(len(cleaned) <= max_length, f"prompt exceeds {max_length} characters")
    return cleaned


def parse_local_file(
    raw_path: str,
    *,
    allowed_extensions: Iterable[str],
    size_limit: int,
    label: str = "file",
) -> Path:
    require(isinstance(raw_path, str) and bool(raw_path.strip()), f"{label} path is required")
    require("\x00" not in raw_path, f"{label} path contains NUL byte")
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} not found: {raw_path}") from exc
    ext_set = {e.lower() for e in allowed_extensions}
    require(path.is_file(), f"{label} is not a regular file: {path.name}")
    require(path.suffix.lower() in ext_set, f"Unsupported {label} extension: {path.name}")
    size = path.stat().st_size
    require(0 < size <= size_limit, f"{label} size out of bounds (1..{size_limit} bytes): {path.name}")
    return path


def parse_image_paths(
    paths: list[str] | None,
    *,
    required: bool = False,
    max_count: int = 5,
    allowed_extensions: Iterable[str] = IMAGE_EXTENSIONS,
    size_limit: int = MAX_IMAGE_SIZE,
) -> list[Path]:
    items = list(paths or [])
    require(not required or bool(items), "At least one image is required")
    require(len(items) <= max_count, f"At most {max_count} images are allowed")
    return [
        parse_local_file(item, allowed_extensions=allowed_extensions, size_limit=size_limit, label="image")
        for item in items
    ]


def parse_image_labels(labels: list[str] | None, image_count: int) -> list[str]:
    if not labels:
        return []
    require(image_count > 0, "image_labels requires at least one uploaded image")
    require(len(labels) <= image_count, "More image_labels supplied than uploaded images")
    parsed: list[str] = []
    for item in labels:
        require(isinstance(item, str) and item in IMAGE_LABELS, f"Invalid image_label: {item!r}")
        parsed.append(item)
    return parsed


def parse_seed(seed: int | None) -> int | None:
    if seed is None:
        return None
    require(type(seed) is int and SEED_MIN <= seed <= SEED_MAX, f"seed must be an integer in {SEED_MIN}..{SEED_MAX}")
    return seed


def parse_bbox_condition(bbox: list[int] | None) -> list[int] | None:
    if bbox is None:
        return None
    require(
        isinstance(bbox, list)
        and len(bbox) == 3
        and all(type(v) is int and BBOX_MIN <= v <= BBOX_MAX for v in bbox),
        f"bbox_condition requires three integers in {BBOX_MIN}..{BBOX_MAX} (X, Y, Z)",
    )
    return list(bbox)


def parse_int_range(value: int | None, *, name: str, minimum: int, maximum: int) -> int | None:
    if value is None:
        return None
    require(type(value) is int and minimum <= value <= maximum, f"{name} must be an integer in {minimum}..{maximum}")
    return value


def parse_float_range(value: float, *, name: str, minimum: float, maximum: float) -> float:
    require(not isinstance(value, bool) and isinstance(value, (int, float)), f"{name} must be a number")
    val = float(value)
    require(math.isfinite(val) and minimum <= val <= maximum, f"{name} must be in {minimum}..{maximum}")
    return val


def parse_image_size(size: str | None) -> str | None:
    if size is None:
        return None
    require(isinstance(size, str), "size must be a string")
    cleaned = size.strip()
    match = SIZE_PATTERN.fullmatch(cleaned)
    require(match is not None, "size must match WIDTHxHEIGHT (positive integers)")
    width, height = int(match.group(1)), int(match.group(2))
    pixels = width * height
    require(
        MIN_IMAGE_PIXELS <= pixels <= MAX_IMAGE_PIXELS,
        "size total pixels must be between 512x512 and 2048x2048",
    )
    return cleaned


def _validate_hierarchy_transform(transform: Any, node_name: str) -> list[float]:
    require(isinstance(transform, list), f"Bone '{node_name}' transform must be a 4x4 matrix")
    if len(transform) == 16 and all(not isinstance(v, bool) and isinstance(v, (int, float)) for v in transform):
        flat = [float(v) for v in transform]
    elif (
        len(transform) == 4
        and all(isinstance(col, list) and len(col) == 4 for col in transform)
        and all(not isinstance(v, bool) and isinstance(v, (int, float)) for col in transform for v in col)
    ):
        flat = [float(v) for col in transform for v in col]
    else:
        raise ValueError(f"Bone '{node_name}' transform must be 16 finite numbers (or 4x4 lists)")
    require(all(math.isfinite(v) for v in flat), f"Bone '{node_name}' transform contains non-finite values")
    return transform


def parse_rig_hierarchy(hierarchy: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Validate OpenAPI v3 RodinRigHierarchyNode[] (1..256 unique nodes, acyclic, optional transform/length)."""
    if hierarchy is None:
        return None
    require(isinstance(hierarchy, list) and 1 <= len(hierarchy) <= MAX_RIG_HIERARCHY_NODES,
            f"hierarchy must contain 1..{MAX_RIG_HIERARCHY_NODES} bone nodes")

    seen_names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, node in enumerate(hierarchy):
        require(isinstance(node, dict), f"hierarchy[{index}] must be an object")
        raw_name = node.get("name")
        require(isinstance(raw_name, str) and bool(raw_name.strip()), f"hierarchy[{index}] requires a non-blank name")
        name = raw_name.strip()
        require(name not in seen_names, f"Duplicate bone name in hierarchy: '{name}'")
        seen_names.add(name)

        parent = node.get("parent")
        require(
            type(parent) is int and -1 <= parent < len(hierarchy) and parent != index,
            f"Bone '{name}' has invalid parent index: {parent!r}",
        )

        entry: dict[str, Any] = {"name": name, "parent": parent}
        if "transform" in node and node["transform"] is not None:
            entry["transform"] = _validate_hierarchy_transform(node["transform"], name)
        if "length" in node:
            length = node["length"]
            require(
                length is not None
                and not isinstance(length, bool)
                and isinstance(length, (int, float))
                and math.isfinite(length)
                and length > 0,
                f"Bone '{name}' length must be a positive finite number",
            )
            entry["length"] = float(length)
        normalized.append(entry)

    # Detect cycles across parent links
    for index in range(len(normalized)):
        visited: set[int] = set()
        current = index
        while current != -1:
            require(current not in visited, "hierarchy contains a parent cycle")
            visited.add(current)
            current = normalized[current]["parent"]

    return normalized


def detect_image_extension(content: bytes) -> str:
    """Identify PNG, JPEG, or WebP from magic bytes; raise ValueError otherwise."""
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and len(content) >= 12 and content[8:12] == b"WEBP":
        return ".webp"
    raise RuntimeError("API response did not contain valid PNG, JPEG, or WebP image bytes")


__all__ = [
    "SYNC_IMAGE_EXTENSIONS",
    "detect_image_extension",
    "parse_bbox_condition",
    "parse_float_range",
    "parse_image_labels",
    "parse_image_paths",
    "parse_image_size",
    "parse_int_range",
    "parse_local_file",
    "parse_optional_uuid",
    "parse_prompt",
    "parse_rig_hierarchy",
    "parse_seed",
    "parse_uuid",
    "require",
]
