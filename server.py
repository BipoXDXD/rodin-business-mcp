"""Unofficial local MCP bridge for the documented Hyper3D Rodin Business API v2.

The API key is read on the Mac via Keychain or process environment. Never exposed
as an MCP tool parameter. All billable tools require explicit approval/budget.
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import lru_cache
import base64
import binascii
import getpass
import ipaddress
import json
import mimetypes
import math
import os
from pathlib import Path
import re
import subprocess
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from mcp.server import MCPServer
import requests

API = "https://api.hyper3d.com/api/v2"
KEYCHAIN_SERVICE = "hyper3d-business"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".gif", ".avif", ".heic", ".heif", ".bmp", ".jp2"}
MODEL_EXTENSIONS = {".obj", ".stl", ".gltf", ".glb", ".fbx", ".usdz", ".usd", ".usda", ".usdc"}
MAX_IMAGE_SIZE = 25 * 1024 * 1024
MAX_MODEL_SIZE = 512 * 1024 * 1024
TIERS = ("Gen-2.5-Extreme-Low", "Gen-2.5-Low", "Gen-2.5-Medium", "Gen-2.5-High", "Gen-2.5-Extreme-High")
TEXTURE_MODES = ("legacy", "extreme-low", "low", "medium", "high", "extreme-high")
FORMATS = ("glb", "fbx", "obj", "usdz", "stl")
LABELS = {"F", "FL", "FR", "B", "BL", "BR", "L", "R", "U", "D", "?"}
SERVER_NAME = "Rodin-Business"
VERSION = "2.3.0"
ANIMATION_CATEGORIES = ("human", "canine", "bird", "fish", "heavyhooved", "leanhooved",
                        "lizard", "rodentia", "seaturtle")
POLYGEN_MODEL_EXTENSIONS = MODEL_EXTENSIONS - {".gltf"}
ENGINE_SKELETONS = {"ue5", "unity", "actorcore"}
mcp = MCPServer(SERVER_NAME, version=VERSION)


@lru_cache(maxsize=1)
def _key() -> str:
    value = os.getenv("RODIN_API_KEY", "").strip()
    if not value and os.name == "posix":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=False, timeout=15,
        )
        if result.returncode == 0:
            value = result.stdout.strip()
    if not value:
        raise RuntimeError("Missing API key: set RODIN_API_KEY or the 'hyper3d-business' Keychain item")
    return value


def _request(method: str, endpoint: str, **kwargs):
    """Only fixed REST endpoints, never arbitrary URLs supplied by tools."""
    if endpoint not in {"rodin", "rodin_agentic", "bang", "rodin_texture_only", "rigging", "polygen",
                        "rigging_animations", "images", "status", "download", "check_balance"}:
        raise ValueError("Endpoint not allowed")
    response = requests.request(
        method, f"{API}/{endpoint}",
        headers={"Authorization": f"Bearer {_key()}"},
        timeout=(10, 120), **kwargs,
    )
    response.raise_for_status()
    return response.json()


def _api(method: str, endpoint: str, **kwargs) -> dict:
    payload = _request(method, endpoint, **kwargs)
    if not isinstance(payload, dict):
        raise RuntimeError("Unexpected API response format")
    if payload.get("error"):
        raise RuntimeError(f"Rodin error: {payload['error']}: {payload.get('message', '')}")
    return payload


def _check(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _asset_id(value: str | None) -> str | None:
    """Normalize an optional Rodin task uuid; reject anything that is not a UUID."""
    value = value.strip() if value else None
    if value:
        try:
            UUID(value)
        except ValueError as exc:
            raise ValueError("asset_id must be a UUID") from exc
    return value


def _path(value: str, extensions: set[str], size_limit: int) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    _check(path.is_file() and path.suffix.lower() in extensions, f"File not allowed: {path.name}")
    _check(0 < path.stat().st_size <= size_limit, f"File size out of range: {path.name}")
    return path


def _images(paths: list[str] | None, *, required: bool = False) -> list[Path]:
    values = paths or []
    _check(not required or bool(values), "Provide at least one image")
    _check(len(values) <= 5, "At most five images")
    return [_path(v, IMAGE_EXTENSIONS, MAX_IMAGE_SIZE) for v in values]


def _validate_common(prompt: str, files: list[Path], fmt: str,
                     quality_override: int | None, max_faces: int,
                     seed: int | None, bbox_condition: list[int] | None) -> None:
    _check(bool(prompt.strip() or files), "Provide a prompt or image(s)")
    _check(len(prompt) <= 1024, "Prompt is limited to 1024 characters")
    _check(fmt in FORMATS, "Unsupported format")
    if quality_override is not None:
        _check(500 <= quality_override <= max_faces, f"quality_override must be 500..{max_faces}")
    if seed is not None:
        _check(0 <= seed <= 65535, "seed must be 0..65535")
    if bbox_condition is not None:
        _check(len(bbox_condition) == 3 and all(type(v) is int and 1 <= v <= 2048 for v in bbox_condition),
               "bbox_condition requires three integers between 1 and 2048 (X, Y, Z)")


def _field(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _estimate(tier: str, texture_mode: str) -> float:
    _check(tier in TIERS, f"Invalid tier: {tier}")
    _check(texture_mode in TEXTURE_MODES, "Invalid texture_mode")
    return .5 + (.5 if tier == "Gen-2.5-Extreme-High" else 0) + (2 if texture_mode == "extreme-high" else 0)


def _paid(endpoint: str, fields: dict, paths: list[tuple[str, Path]], cost: float,
          approved_spend: bool, max_credits: float, *, repeated: list[tuple[str, str]] | None = None) -> dict:
    """Guard spending, encode multipart fields and local binary files. No key in tool args."""
    preview = {"endpoint": endpoint, "estimated_credits": cost,
               "parameters": {k: v for k, v in fields.items() if v is not None},
               "files": [{"field": field, "name": file.name} for field, file in paths]}
    if not approved_spend:
        return {"submitted": False, "reason": "Explicit approval required; no credits spent", **preview}
    _check(math.isfinite(max_credits) and max_credits >= cost and max_credits > 0,
           f"Estimated cost {cost} exceeds finite budget max_credits={max_credits}")
    parts: list[tuple[str, tuple]] = [
        (key, (None, _field(value))) for key, value in fields.items() if value is not None
    ]
    parts.extend((key, (None, value)) for key, value in (repeated or []))
    with ExitStack() as stack:
        for field, path in paths:
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            parts.append((field, (path.name, stack.enter_context(path.open("rb")), mime)))
        payload = _api("POST", endpoint, files=parts)
    if not payload.get("uuid"):
        raise RuntimeError(f"The API returned no UUID: {payload.get('message', 'task not confirmed')}")
    return {"submitted": True, **preview,
            "actual_credits": payload.get("consumed"), "result": payload}


@mcp.tool()
def rodin_capabilities() -> dict:
    """List all local Business REST integrations, available generation families and cost guards. FREE."""
    return {
        "server": SERVER_NAME, "version": VERSION, "endpoint_base": API,
        "tools": ["rodin_capabilities", "rodin_balance", "rodin_estimate", "rodin_generate",
                  "rodin_agentic",
                  "rodin_bang_asset", "rodin_bang_model", "rodin_texture",
                  "rodin_rigging", "rodin_animation_catalog", "rodin_polygen", "rodin_images",
                  "rodin_status", "rodin_download_links", "rodin_save_results"],
        "generation": ["Rodin Gen-2.5", "Rodin Agentic (beta)"],
        "editing": ["BANG! on Rodin asset", "BANG! on uploaded model", "Texture existing model",
                    "Rigging: auto/human/quadruped/bird/dragon/kaiju/custom; generate/modify; animations; UE5/Unity/ActorCore FBX skeletons",
                    "PolyGen low-poly (200–10,000 faces; asset, model, images or text)", "Image generation/editing"],
        "images": {"tool": "rodin_images", "synchronous": True, "max_reference_images": 3,
                   "output_directory": "RODIN_OUTPUT_DIR or ~/Downloads/Hyper3D",
                   "estimated_credits": .5},
        "rigging": {"tool": "rodin_rigging", "asynchronous": True,
                    "status_tool": "rodin_status", "download_tool": "rodin_save_results",
                    "catalog_tool": "rodin_animation_catalog", "estimated_credits": .5},
        "polygen": {"tool": "rodin_polygen", "asynchronous": True, "estimated_credits": .5},
        "not_available": "Hyper3D library uploads (list/delete) exist only in the hosted OAuth MCP; REST sends files directly.",
        "browser_required": False,
        "warning": "No generation occurs without approved_spend=True and sufficient max_credits; prices are estimates from published docs.",
        "documentation": "https://docs.hyper3d.ai/en"
    }


@mcp.tool()
def rodin_balance() -> dict:
    """Get the current Business API credit balance; does not consume generation credits."""
    return _api("GET", "check_balance")


@mcp.tool()
def rodin_estimate(
    tier: str = "Gen-2.5-High",
    texture_mode: str = "high",
    operation: Literal["gen2.5", "agentic", "bang", "texture", "rigging", "polygen", "images"] = "gen2.5",
    agentic_tier: Literal["basic", "standard", "pro"] = "standard",
) -> dict:
    """Estimate credits locally for any documented paid REST operation; no API request."""
    if operation == "gen2.5":
        credits = _estimate(tier, texture_mode)
    elif operation in {"bang", "rigging", "polygen", "images"}:
        credits = .5
    elif operation == "texture":
        _check(texture_mode in TEXTURE_MODES, "Invalid texture_mode")
        credits = .5 + (2 if texture_mode == "extreme-high" else 0)
    elif operation == "agentic":
        credits = {"basic": .5, "standard": 1.0, "pro": 2.0}[agentic_tier]
    else:
        raise ValueError("Invalid operation")
    return {"operation": operation, "estimated_credits": credits, "estimate_only": True,
            "note": "Prices published in Nov 2026; check the docs before running. This does not reserve credits."}


@mcp.tool()
def rodin_generate(
    prompt: str = "",
    tier: Literal["Gen-2.5-Extreme-Low", "Gen-2.5-Low", "Gen-2.5-Medium", "Gen-2.5-High", "Gen-2.5-Extreme-High"] = "Gen-2.5-High",
    texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] = "high",
    mesh_mode: Literal["Raw", "Quad"] = "Raw",
    quality: Literal["extra-low", "low", "medium", "high"] = "medium",
    material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] = "PBR",
    geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
    quality_override: int | None = None,
    high_pack: bool = False, uhd_texture: bool = False, hd_texture: bool = False,
    quad_normal: bool = False, ta_pose: bool = False, is_micro: bool = False,
    preview_render: bool = False, soft: bool = False, detail_level: int | None = None,
    seed: int | None = None, bbox_condition: list[int] | None = None,
    use_original_alpha: bool = False,
    geometry_instruct_mode: Literal["creative", "faithful"] | None = None,
    is_symmetric: Literal["symmetric", "balanced", "asymmetric", "unknown"] | None = None,
    texture_delight: bool = False,
    image_paths: list[str] | None = None, image_labels: list[str] | None = None,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID: Gen-2.5 text/image-to-3D. Supports High/Extreme-High, 2K–12K texture combinations,
    PBR/Hybrid, topology, target faces, multiview image labels, seed, proportions, creative/faithful,
    micro detail and smoothing. Request approval from user before setting approved_spend=True.
    Set max_credits to approved per-request limit. Defaults perform no paid operation.
    """
    files = _images(image_paths)
    max_faces = 200000 if mesh_mode == "Quad" else (2000000 if tier in ("Gen-2.5-High", "Gen-2.5-Extreme-High") else 1000000)
    _validate_common(prompt, files, geometry_file_format, quality_override, max_faces, seed, bbox_condition)
    _check(tier in TIERS, "Invalid tier")
    _check(mesh_mode in {"Quad", "Raw"}, "Invalid mesh_mode")
    if detail_level is not None:
        _check(1 <= detail_level <= 3, "detail_level must be 1..3")
    _check(not is_micro or tier == "Gen-2.5-Extreme-High", "is_micro requires Gen-2.5-Extreme-High")
    _check(geometry_instruct_mode != "creative" or tier in TIERS[2:], "creative requires Gen-2.5-Medium or higher")
    if image_labels:
        _check(bool(files) and len(image_labels) <= len(files) and all(x in LABELS for x in image_labels), "Invalid image_labels")
    cost = _estimate(tier, texture_mode)
    fields = {"tier": tier, "mesh_mode": mesh_mode, "quality": quality, "texture_mode": texture_mode,
              "material": material, "geometry_file_format": geometry_file_format,
              "prompt": prompt.strip() or None, "quality_override": quality_override,
              "soft": soft, "detail_level": detail_level, "seed": seed,
              "bbox_condition": bbox_condition, "use_original_alpha": use_original_alpha,
              "uhd_texture": uhd_texture, "hd_texture": hd_texture, "quad_normal": quad_normal,
              "TAPose": ta_pose, "is_micro": is_micro, "preview_render": preview_render,
              "geometry_instruct_mode": geometry_instruct_mode,
              "is_symmetric": is_symmetric, "texture_delight": texture_delight}
    repeated = ([('addons', 'HighPack')] if high_pack else [])
    repeated.extend(("image_label", x) for x in (image_labels or []))
    return _paid("rodin", fields, [("images", x) for x in files], cost, approved_spend, max_credits, repeated=repeated)


@mcp.tool()
def rodin_agentic(
    image_paths: list[str], prompt: str = "",
    tier: Literal["basic", "standard", "pro"] = "standard",
    export_type: Literal["glb", "usdz", "fbx", "stl"] = "glb",
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID, BETA: Rodin Agentic auto-chooses 3D generation strategies from 1–5 images.
    Tiers basic 0.5 credit, standard 1, pro 2. Requires explicit approval."""
    imgs = _images(image_paths, required=True)
    _check(len(prompt) <= 1024, "Prompt is too long")
    cost = {"basic": .5, "standard": 1.0, "pro": 2.0}[tier]
    return _paid("rodin_agentic", {"tier": tier, "prompt": prompt or None, "export_type": export_type},
                 [("images", p) for p in imgs], cost, approved_spend, max_credits)


@mcp.tool()
def rodin_bang_asset(
    asset_id: str, strength: int = 5,
    mesh_mode: Literal["Raw", "Quad"] = "Raw", quality_override: int | None = None,
    material: Literal["PBR", "Shaded", "All", "None"] = "PBR",
    resolution: Literal["Basic", "High"] = "Basic",
    geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
    escore: float = 3.5, reference_scale: float = .5, seed: int | None = None,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID: BANG! split/refine previously generated Rodin asset using its top-level uuid.
    Consumes 0.5 credit. Requires explicit approval."""
    _check(bool(asset_id.strip()), "asset_id is required")
    _check(1 <= strength <= 12, "strength must be 1..12")
    _check(0 <= escore <= 10 and 0 <= reference_scale <= 1, "escore/reference_scale out of range")
    if seed is not None:
        _check(0 <= seed <= 65535, "Invalid seed")
    if quality_override is not None:
        _check(500 <= quality_override <= (50000 if mesh_mode == "Quad" else 2000000), "quality_override out of range")
    return _paid("bang", {"asset_id": asset_id, "strength": strength,
                          "mesh_mode": mesh_mode, "quality_override": quality_override,
                          "material": material, "resolution": resolution,
                          "geometry_file_format": geometry_file_format,
                          "escore": escore, "reference_scale": reference_scale, "seed": seed},
                 [], .5, approved_spend, max_credits)


@mcp.tool()
def rodin_bang_model(
    model_path: str, image_path: str | None = None,
    prompt: str = "", instruction: str = "", strength: int = 5,
    mesh_mode: Literal["Raw", "Quad"] = "Raw", quality_override: int | None = None,
    material: Literal["PBR", "Shaded", "All", "None"] = "PBR",
    resolution: Literal["Basic", "High"] = "Basic",
    geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
    escore: float = 3.5, reference_scale: float = .5, seed: int | None = None,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID: BANG! split/refine an uploaded 3D model with optional segmentation instruction.
    Reference image required if material != None or instruction supplied. Cost 0.5 credit."""
    model = _path(model_path, MODEL_EXTENSIONS, MAX_MODEL_SIZE)
    img = _path(image_path, IMAGE_EXTENSIONS, MAX_IMAGE_SIZE) if image_path else None
    _check(img is not None or (material == "None" and not instruction),
           "A reference image is required with materials or instruction")
    _check(1 <= strength <= 12, "strength must be 1..12")
    _check(0 <= escore <= 10 and 0 <= reference_scale <= 1, "escore/reference_scale out of range")
    if seed is not None:
        _check(0 <= seed <= 65535, "Invalid seed")
    if quality_override is not None:
        _check(500 <= quality_override <= (50000 if mesh_mode == "Quad" else 2000000), "quality_override out of range")
    _check(len(prompt) <= 1024, "Prompt is too long")
    paths = [("model", model)]
    if img:
        paths.append(("image", img))
    return _paid("bang", {"prompt": prompt or None, "instruction": instruction or None,
                          "strength": strength, "mesh_mode": mesh_mode,
                          "quality_override": quality_override, "material": material,
                          "resolution": resolution, "geometry_file_format": geometry_file_format,
                          "escore": escore, "reference_scale": reference_scale, "seed": seed},
                 paths, .5, approved_spend, max_credits)


@mcp.tool()
def rodin_texture(
    image_path: str, model_path: str, prompt: str = "",
    texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] = "high",
    resolution: Literal["Basic", "High"] = "Basic",
    material: Literal["PBR", "Shaded", "All"] = "PBR",
    geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
    escore: float = 3.5, reference_scale: float = 1,
    hd_texture: bool = False, texture_delight: bool = False, seed: int | None = None,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID: generate 2K/4K/6K/12K PBR/Shaded textures for an existing 3D model.
    12K requires texture_mode='extreme-high', resolution='High'.
    Costs 0.5 credit (+2 for extreme-high); requires explicit approval."""
    img = _path(image_path, IMAGE_EXTENSIONS, MAX_IMAGE_SIZE)
    model = _path(model_path, MODEL_EXTENSIONS, MAX_MODEL_SIZE)
    _check(len(prompt) <= 1024, "Prompt is too long")
    _check(0 <= escore <= 10 and .5 <= reference_scale <= 1, "escore/reference_scale out of range")
    if seed is not None:
        _check(0 <= seed <= 65535, "Invalid seed")
    cost = .5 + (2 if texture_mode == "extreme-high" else 0)
    return _paid("rodin_texture_only",
                 {"prompt": prompt or None, "texture_mode": texture_mode,
                  "resolution": resolution, "material": material,
                  "geometry_file_format": geometry_file_format,
                  "escore": escore, "reference_scale": reference_scale,
                  "hd_texture": hd_texture, "texture_delight": texture_delight, "seed": seed},
                 [("image", img), ("model", model)], cost, approved_spend, max_credits)


@mcp.tool()
def rodin_rigging(
    asset_id: str | None = None, model_path: str | None = None,
    image_path: str | None = None,
    bone_template: Literal["auto", "human", "quadruped", "bird", "dragon", "kaiju", "custom"] = "auto",
    rigging_mode: Literal["generate", "modify"] = "generate",
    skinning_method: Literal["shell", "core"] = "shell",
    hierarchy: list[dict] | None = None, skeleton_description: str | None = None,
    animation_presets: list[str] | None = None, animation_paths: list[str] | None = None,
    material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] | None = None,
    geometry_file_format: Literal["glb", "fbx", "usdz"] = "glb",
    export_skeleton_type: Literal["rodin", "ue5", "unity", "actorcore"] = "rodin",
    in_place: bool = False,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID (0.5 credit): rig a confirmed Rodin asset OR an uploaded model, never both.
    auto/human/quadruped/bird/dragon/kaiju/custom skeletons; generate or modify; shell/core skinning.
    Output glb/fbx/usdz. export_skeleton_type ue5/unity/actorcore rewrites the FBX skeleton
    (Rodin asset_id + fbx + humanoid rig only). in_place=True exports looping stock
    animations in place (not with animation_paths). Find stock IDs with rodin_animation_catalog.
    custom generation needs hierarchy OR skeleton_description; modify needs the full
    baseline skeleton plus changes in skeleton_description, no hierarchy and no auto.
    Uploaded textured models need a JPG/PNG/WebP reference; auto also needs an image
    (a Rodin asset can reuse its input image). Embedded uploaded textures are not kept.
    Up to 10 distinct stock animation_presets OR up to 10 single-clip FBX animation_paths
    (25 MiB each), mutually exclusive; clips must match the target skeleton.
    Poll rodin_status with jobs.subscription_key, then rodin_save_results with result.uuid.
    Defaults return a preview only. Explicit spending approval and budget are required.
    """
    asset_id = _asset_id(asset_id)
    _check(bool(asset_id) != bool(model_path), "Provide exactly one of asset_id or model_path")
    _check(bone_template in {"auto", "human", "quadruped", "bird", "dragon", "kaiju", "custom"},
           "Invalid bone_template")
    _check(geometry_file_format in {"glb", "fbx", "usdz"}, "Rigging exports only glb, fbx or usdz")
    _check(export_skeleton_type in {"rodin"} | ENGINE_SKELETONS, "Invalid export_skeleton_type")
    if export_skeleton_type in ENGINE_SKELETONS:
        _check(bool(asset_id) and geometry_file_format == "fbx" and bone_template in {"auto", "human"},
               "UE5/Unity/ActorCore skeletons require asset_id, fbx and a humanoid rig (auto or human)")
    _check(rigging_mode in {"generate", "modify"}, "Invalid rigging_mode")
    _check(skinning_method in {"shell", "core"}, "Invalid skinning_method")
    _check(material in {None, "PBR", "Shaded", "Hybrid", "All", "None"}, "Invalid material")
    _check(geometry_file_format in FORMATS, "Invalid geometry_file_format")
    description = skeleton_description.strip() if skeleton_description else None
    if rigging_mode == "modify":
        _check(bone_template != "auto" and bool(description) and hierarchy is None,
               "modify requires an explicit template, skeleton_description and no hierarchy")
    elif bone_template == "custom":
        _check(bool(hierarchy) != bool(description), "custom requires hierarchy OR skeleton_description")
    else:
        _check(hierarchy is None and description is None,
               "Preset templates do not accept hierarchy or skeleton_description in generate mode")
    if hierarchy is not None:
        _check(bool(hierarchy), "Empty hierarchy")
        for index, node in enumerate(hierarchy):
            _check(isinstance(node, dict) and isinstance(node.get("name"), str) and bool(node["name"].strip()),
                   "Every bone requires a name")
            parent = node.get("parent")
            _check(type(parent) is int and -1 <= parent < len(hierarchy) and parent != index,
                   "Invalid bone parent")
        for index in range(len(hierarchy)):
            seen = set()
            current = index
            while current != -1:
                _check(current not in seen, "hierarchy contains a cycle")
                seen.add(current)
                current = hierarchy[current]["parent"]
    paths = []
    if model_path:
        paths.append(("model", _path(model_path, MODEL_EXTENSIONS - {".gltf"}, MAX_MODEL_SIZE)))
    if image_path:
        paths.append(("image", _path(image_path, {".jpg", ".jpeg", ".png", ".webp"}, MAX_IMAGE_SIZE)))
    _check(not model_path or material in {None, "None"} or bool(image_path),
           "A model with materials requires a reference image")
    _check(bone_template != "auto" or bool(image_path or asset_id),
           "auto requires an image; asset_id can reuse its original input image")
    presets = animation_presets or []
    clips = animation_paths or []
    _check(not (presets and clips), "animation_presets and animation_paths are mutually exclusive")
    _check(len(presets) + len(clips) <= 10, "At most ten animations")
    _check(all(isinstance(p, str) and bool(p.strip()) for p in presets), "Empty animation preset")
    _check(len(set(presets)) == len(presets), "Animation presets must be distinct")
    _check(not (in_place and clips), "in_place does not apply to animation_paths")
    paths.extend(("animation_files", _path(p, {".fbx"}, 25 * 1024 * 1024)) for p in clips)
    fields = {"asset_id": asset_id, "bone_template": bone_template, "rigging_mode": rigging_mode,
              "skinning_method": skinning_method, "hierarchy": hierarchy,
              "skeleton_description": description, "material": material,
              "geometry_file_format": geometry_file_format,
              "export_skeleton_type": None if export_skeleton_type == "rodin" else export_skeleton_type,
              "in_place": True if in_place else None}
    result = _paid("rigging", fields, paths, .5, approved_spend, max_credits,
                   repeated=[("animation_preset", p) for p in presets])
    result["animation_presets"] = presets
    return result


@mcp.tool()
def rodin_animation_catalog(
    category: Literal["human", "canine", "bird", "fish", "heavyhooved", "leanhooved",
                      "lizard", "rodentia", "seaturtle"] | None = None,
) -> dict:
    """FREE: list stock rigging animations (animation_id, name, description, frames,
    duration, loop metadata, signed MP4 preview). Pass chosen animation_id values to
    rodin_rigging.animation_presets. Categories do not guarantee skeleton compatibility.
    """
    if category is not None:
        _check(category in ANIMATION_CATEGORIES, "Invalid category")
    payload = _request("GET", "rigging_animations",
                       params={"category": category} if category is not None else None)
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"Rodin error: {payload['error']}: {payload.get('message', '')}")
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected animation catalog format")
    return {"category": category, "count": len(payload), "animations": payload}


@mcp.tool()
def rodin_polygen(
    asset_id: str | None = None, model_path: str | None = None,
    image_paths: list[str] | None = None, image_labels: list[str] | None = None,
    prompt: str = "", face_count: int | None = None,
    mesh_mode: Literal["Quad", "Raw", "NGon"] = "Quad",
    geometry_file_format: Literal["obj", "glb", "usdz", "fbx", "stl"] = "obj",
    material: Literal["PBR", "Shaded", "Hybrid", "All"] | None = None,
    texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] | None = None,
    uhd_texture: bool = False, resolution: Literal["Basic", "High"] = "Basic",
    transfer_texture: bool = True,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID (0.5 credit): PolyGen Smart Low Poly from ONE source: a completed Rodin
    Gen-2/2.5 asset_id, an uploaded model (OBJ/GLB/STL/FBX/USD*), 1–5 reference images,
    or a text prompt alone. face_count 200–10,000 (omit for automatic; 4,000 for models).
    Geometry only unless material is set (asset/images/text). Uploaded models keep their
    own textures (transfer_texture=False drops them); material, texture_mode, uhd_texture
    and prompt do not apply to them. resolution=High needs high-res export access.
    Poll rodin_status with jobs.subscription_key, then rodin_save_results with result.uuid.
    Defaults return a preview only. Explicit spending approval and budget are required.
    """
    asset_id = _asset_id(asset_id)
    _check(not (asset_id and model_path), "asset_id and model_path are mutually exclusive")
    images = _images(image_paths)
    prompt = prompt.strip()
    _check(len(prompt) <= 1024, "Prompt is limited to 1024 characters")
    _check(bool(asset_id or model_path or images or prompt), "Provide asset_id, model_path, images or a prompt")
    if face_count is not None:
        _check(200 <= face_count <= 10000, "face_count must be 200..10000")
    if model_path:
        _check(material is None and texture_mode is None and not uhd_texture and not prompt,
               "Uploaded models get no new textures: do not set material, texture_mode, uhd_texture or prompt")
    else:
        _check(transfer_texture, "transfer_texture only applies to model_path")
    if image_labels:
        _check(bool(images) and len(image_labels) <= len(images) and all(x in LABELS for x in image_labels),
               "Invalid image_labels")
    paths: list[tuple[str, Path]] = []
    if model_path:
        paths.append(("model", _path(model_path, POLYGEN_MODEL_EXTENSIONS, MAX_MODEL_SIZE)))
    paths.extend(("images", p) for p in images)
    fields = {"asset_id": asset_id, "prompt": prompt or None, "face_count": face_count,
              "mesh_mode": mesh_mode, "geometry_file_format": geometry_file_format,
              "material": material, "texture_mode": texture_mode,
              "uhd_texture": True if uhd_texture else None, "resolution": resolution,
              "transfer_texture": False if model_path and not transfer_texture else None,
              "image_label": image_labels or None}
    return _paid("polygen", fields, paths, .5, approved_spend, max_credits)


@mcp.tool()
def rodin_images(
    prompt: str, size: str | None = None, image_paths: list[str] | None = None,
    approved_spend: bool = False, max_credits: float = 0,
) -> dict:
    """PAID (0.5 credit): generate an image, or edit with 1–3 PNG/JPEG/WebP references
    (10 MiB each). size is WIDTHxHEIGHT, 512*512..2048*2048 total pixels; omit to use
    the API default (2048x2048 generation, last reference dimensions for editing).
    Synchronous: saves decoded PNG/JPEG/WebP into RODIN_OUTPUT_DIR or
    ~/Downloads/Hyper3D and returns local file paths, no polling or base64 in tool output.
    Defaults preview only; explicit spending approval and finite budget are required.
    """
    _check(bool(prompt.strip()), "prompt is required")
    if size is not None:
        match = re.fullmatch(r"([1-9][0-9]*)x([1-9][0-9]*)", size)
        _check(match is not None, "size must be WIDTHxHEIGHT")
        width, height = map(int, match.groups())
        _check(512 * 512 <= width * height <= 2048 * 2048, "Total pixel count out of range")
    refs = image_paths or []
    _check(len(refs) <= 3, "At most three reference images")
    paths = [_path(p, {".png", ".jpg", ".jpeg", ".webp"}, 10 * 1024 * 1024) for p in refs]
    fields = {"prompt": prompt.strip(), **({"size": size} if size is not None else {})}
    preview = {"endpoint": "images", "estimated_credits": .5, "parameters": fields,
               "files": [{"field": "images", "name": p.name} for p in paths]}
    if not approved_spend:
        return {"submitted": False, "reason": "Explicit approval required; no credits spent", **preview}
    _check(math.isfinite(max_credits) and max_credits >= .5, "Requires a finite budget max_credits >= 0.5")
    directory = Path(os.getenv("RODIN_OUTPUT_DIR", "~/Downloads/Hyper3D")).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if paths:
        with ExitStack() as stack:
            parts = [(k, (None, v)) for k, v in fields.items()]
            parts.extend(("images", (p.name, stack.enter_context(p.open("rb")), mimetypes.guess_type(p.name)[0]))
                         for p in paths)
            payload = _api("POST", "images", files=parts)
    else:
        payload = _api("POST", "images", json=fields)
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RuntimeError("Images response has no image; check your history before retrying")
    item = data[0]
    try:
        content = base64.b64decode(item["b64_json"], validate=True)
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise RuntimeError("Invalid base64 image; check your history before retrying") from exc
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise RuntimeError("The response contains no valid PNG/JPEG/WebP")
    target = directory / f"image-{uuid4().hex}{suffix}"
    try:
        with target.open("xb") as output:
            output.write(content)
    except OSError as exc:
        raise RuntimeError("The API responded but saving locally failed; do not retry the generation automatically") from exc
    return {"submitted": True, **preview, "actual_credits": payload.get("consumed"),
            "created": payload.get("created"),
            "saved_files": [{"path": str(target), "bytes": len(content),
                             "revised_prompt": item.get("revised_prompt")}]}


























@mcp.tool()
def rodin_status(subscription_key: str) -> dict:
    """FREE: check asynchronous task status using jobs.subscription_key (Agentic uses job.subscription_key)."""
    _check(bool(subscription_key.strip()), "subscription_key is required")
    return _api("POST", "status", json={"subscription_key": subscription_key})


@mcp.tool()
def rodin_download_links(task_uuid: str) -> dict:
    """FREE: fetch signed result URLs using top-level task uuid, after all statuses are Done."""
    _check(bool(task_uuid.strip()), "task_uuid is required")
    return _api("POST", "download", json={"task_uuid": task_uuid})


def _safe_download_url(url: str) -> None:
    parsed = urlsplit(url)
    _check(parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password,
           "Invalid HTTPS download URL")
    _check(parsed.port in (None, 443), "Download port not allowed")
    host = parsed.hostname
    _check(host.lower() not in {"localhost", "localhost.localdomain"} and not host.lower().endswith(".localhost"), "Local host not allowed")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        _check(ip.is_global, "Private or reserved IP address not allowed")


@mcp.tool()
def rodin_save_results(task_uuid: str) -> dict:
    """FREE: fetch signed links and download finished results into ~/Downloads/Hyper3D.
    Override destination using RODIN_OUTPUT_DIR in server process. Does not generate assets."""
    _check(bool(task_uuid.strip()), "task_uuid is required")
    data = _api("POST", "download", json={"task_uuid": task_uuid})
    directory = Path(os.getenv("RODIN_OUTPUT_DIR", "~/Downloads/Hyper3D")).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    files_saved: list[dict] = []
    safe_task_id = re.sub(r"[^A-Za-z0-9_-]", "-", task_uuid)[:64]
    for index, item in enumerate(data.get("list", []), start=1):
        url = item.get("url", "")
        _safe_download_url(str(url))
        # Prefix UUID and index to avoid overwriting files from separate jobs.
        name = Path(str(item.get("name", ""))).name or f"result-{index}.bin"
        target = directory / f"{safe_task_id}-{index}-{name[:140]}"
        temp = target.with_name(target.name + ".part")
        try:
            with requests.get(str(url), stream=True, timeout=(10, 120), allow_redirects=False) as res:
                res.raise_for_status()
                if 300 <= res.status_code < 400:
                    raise RuntimeError("Download redirects not allowed")
                with temp.open("wb") as out:
                    for chunk in res.iter_content(1024 * 1024):
                        if chunk:
                            out.write(chunk)
            temp.replace(target)
            files_saved.append({"path": str(target), "bytes": target.stat().st_size})
        finally:
            if temp.exists():
                temp.unlink()
    return {"task_uuid": task_uuid, "saved_files": files_saved}


if __name__ == "__main__":
    mcp.run()
