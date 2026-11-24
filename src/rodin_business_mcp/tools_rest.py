"""MCP tool definitions for the documented Hyper3D Rodin Business REST API v2."""

from __future__ import annotations

from contextlib import ExitStack
import mimetypes
from pathlib import Path
from typing import Any, Callable, Literal

from . import SERVER_NAME, VERSION
from .config import (
    AGENTIC_FORMATS,
    AGENTIC_ROUTES,
    AGENTIC_TIERS,
    ANIMATION_CATEGORIES,
    ANIMATION_EXTENSIONS,
    BONE_TEMPLATES,
    ENGINE_SKELETONS,
    EXPORT_SKELETON_TYPES,
    FORMATS,
    GEOMETRY_INSTRUCT_MODES,
    IMAGE_EXTENSIONS,
    MATERIALS,
    MAX_ANIMATION_SIZE,
    MAX_IMAGE_SIZE,
    MAX_MODEL_SIZE,
    MAX_SYNC_IMAGE_SIZE,
    MAX_TEXTURE_IMAGE_SIZE,
    MAX_TEXTURE_MODEL_SIZE,
    MESH_MODES,
    MODEL_EXTENSIONS,
    OFFICIAL_DOCS_URL,
    POLYGEN_MESH_MODES,
    QUALITIES,
    RESOLUTIONS,
    RIGGING_FORMATS,
    SKINNING_METHODS,
    STRICT_MODEL_EXTENSIONS,
    SYMMETRY_MODES,
    SYNC_IMAGE_EXTENSIONS,
    TEXTURE_MATERIALS,
    TEXTURE_MODES,
    TIERS,
)
from .downloads import download_signed_files, save_decoded_image
from .pricing import estimate_gen25_credits, estimate_operation_credits, verify_spend_budget
from .rest_client import RodinRestClient
from .validators import (
    parse_bbox_condition,
    parse_float_range,
    parse_image_labels,
    parse_image_paths,
    parse_image_size,
    parse_int_range,
    parse_local_file,
    parse_optional_uuid,
    parse_prompt,
    parse_rig_hierarchy,
    parse_seed,
    parse_uuid,
    require,
)

REST_TOOL_NAMES = (
    "rodin_capabilities",
    "rodin_balance",
    "rodin_estimate",
    "rodin_generate",
    "rodin_agentic",
    "rodin_bang_asset",
    "rodin_bang_model",
    "rodin_texture",
    "rodin_rigging",
    "rodin_animation_catalog",
    "rodin_polygen",
    "rodin_images",
    "rodin_status",
    "rodin_wait",
    "rodin_download_links",
    "rodin_save_results",
    "rodin_recent_jobs",
)


def register_rest_tools(
    mcp: Any,
    client: RodinRestClient,
    *,
    oauth_tool_names: tuple[str, ...] = (),
    oauth_connected_fn: Callable[[], bool] = lambda: False,
    get_fn: Any = None,
    resolve_dns: bool = True,
) -> dict[str, Callable[..., Any]]:
    """Register all 17 REST API v2 tools on the MCPServer and return a name->callable map."""
    registered: dict[str, Callable[..., Any]] = {}

    def _register(fn: Callable[..., Any]) -> Callable[..., Any]:
        mcp.tool()(fn)
        registered[fn.__name__] = fn
        return fn

    @_register
    def rodin_capabilities() -> dict[str, Any]:
        """FREE: List server capabilities, auth state, all REST v2 + optional OAuth tools, and spending guards."""
        all_tools = list(REST_TOOL_NAMES) + list(oauth_tool_names)
        return {
            "server": SERVER_NAME,
            "version": VERSION,
            "endpoint_base": client.config.api_base_url,
            "official_mcp_url": client.config.official_mcp_url,
            "auth_status": {
                "rest_api_key_configured": client.key_provider.is_configured(),
                "oauth_connected": oauth_connected_fn(),
            },
            "tools": all_tools,
            "rest_tools": list(REST_TOOL_NAMES),
            "oauth_tools": list(oauth_tool_names),
            "generation": ["Rodin Gen-2.5 (5 tiers, up to 12K textures & 10M raw)", "Rodin Agentic (beta, auto/high-poly/n-gons)"],
            "editing": [
                "BANG! on Rodin asset or uploaded model (semantic instruction + preview_render)",
                "Texture generation on uploaded model (1–5 reference images, up to 12K PBR/Shaded/Hybrid/All)",
                "Rigging (auto/human/quadruped/bird/dragon/kaiju/custom; 1–256 bone hierarchy; stock or FBX clips; UE5/Unity/ActorCore FBX)",
                "PolyGen Smart Low-Poly (200–10,000 faces; Quad/Raw/NGon; asset, model, images, or text)",
                "Synchronous 2D image generation & editing (up to 3 references)",
            ],
            "oauth_extras": [
                "Official OAuth 2.1 Device Flow (tokens stored in Keychain)",
                "Account/wallet breakdown (permanent + Business subscription balance + team groups + privileges)",
                "Persistent Hyper3D upload library (create/list/delete for images, models, animations)",
                "Permanent result display_url lookup",
            ],
            "browser_required": False,
            "spending_guard": "Paid tools default to dry-run preview (approved_spend=False). Execution requires approved_spend=True and finite max_credits >= estimate; paid submissions are never auto-retried.",
            "documentation": OFFICIAL_DOCS_URL,
        }

    @_register
    def rodin_balance(usage_page: int | None = None) -> dict[str, Any]:
        """FREE: Get Business API credit balance, plus optional paginated usage history (usage_page >= 1, up to 20 records)."""
        params: dict[str, Any] | None = None
        if usage_page is not None:
            require(type(usage_page) is int and usage_page >= 1, "usage_page must be a positive integer (>= 1)")
            params = {"usage_page": usage_page}
        return client.request_dict("GET", "check_balance", retryable=True, params=params)

    @_register
    def rodin_estimate(
        tier: str = "Gen-2.5-High",
        texture_mode: str = "high",
        operation: Literal["gen2.5", "agentic", "bang", "texture", "rigging", "polygen", "images"] = "gen2.5",
        agentic_tier: Literal["basic", "standard", "pro"] = "standard",
    ) -> dict[str, Any]:
        """FREE: Estimate credits locally for any documented paid REST v2 operation; sends no network request."""
        credits = estimate_operation_credits(
            operation,
            tier=tier,
            texture_mode=texture_mode,
            agentic_tier=agentic_tier,
        )
        return {
            "operation": operation,
            "estimated_credits": credits,
            "estimate_only": True,
            "note": "Estimates from Hyper3D docs (Nov 2026); the API response field 'consumed' is authoritative.",
        }

    @_register
    def rodin_generate(
        prompt: str = "",
        tier: Literal[
            "Gen-2.5-Extreme-Low", "Gen-2.5-Low", "Gen-2.5-Medium", "Gen-2.5-High", "Gen-2.5-Extreme-High"
        ] = "Gen-2.5-High",
        texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] = "high",
        mesh_mode: Literal["Raw", "Quad"] = "Raw",
        quality: Literal["extra-low", "low", "medium", "high"] = "medium",
        material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] = "PBR",
        geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
        quality_override: int | None = None,
        high_pack: bool = False,
        uhd_texture: bool = False,
        hd_texture: bool = False,
        quad_normal: bool = False,
        ta_pose: bool = False,
        is_micro: bool = False,
        preview_render: bool = False,
        soft: bool = False,
        detail_level: int | None = None,
        seed: int | None = None,
        bbox_condition: list[int] | None = None,
        use_original_alpha: bool = False,
        geometry_instruct_mode: Literal["creative", "faithful"] | None = None,
        is_symmetric: Literal["symmetric", "balanced", "asymmetric", "unknown"] | None = None,
        texture_delight: bool = False,
        image_paths: list[str] | None = None,
        image_labels: list[str] | None = None,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5–3 credits): Rodin Gen-2.5 text/image-to-3D.
        Supports all 5 tiers, 2K–12K textures (HighPack/uhd_texture/extreme-high), Fast mode,
        Quad/Raw topology, multiview labels, bbox_condition, T/A pose, micro detail, and preview_render.
        Defaults to dry-run preview; requires approved_spend=True and sufficient max_credits to submit.
        """
        files = parse_image_paths(image_paths, required=False, max_count=5, allowed_extensions=IMAGE_EXTENSIONS, size_limit=MAX_IMAGE_SIZE)
        clean_prompt = parse_prompt(prompt, required=False)
        require(bool(clean_prompt or files), "Provide a prompt or at least one image")
        require(tier in TIERS, f"Invalid tier: {tier}")
        require(texture_mode in TEXTURE_MODES, f"Invalid texture_mode: {texture_mode}")
        require(mesh_mode in MESH_MODES, f"Invalid mesh_mode: {mesh_mode}")
        require(quality in QUALITIES, f"Invalid quality: {quality}")
        require(material in MATERIALS, f"Invalid material: {material}")
        require(geometry_file_format in FORMATS, f"Unsupported format: {geometry_file_format}")

        max_faces = 200_000 if mesh_mode == "Quad" else (2_000_000 if tier in ("Gen-2.5-High", "Gen-2.5-Extreme-High") else 1_000_000)
        q_override = parse_int_range(quality_override, name="quality_override", minimum=500, maximum=max_faces)
        parsed_detail = parse_int_range(detail_level, name="detail_level", minimum=1, maximum=3)
        parsed_seed = parse_seed(seed)
        parsed_bbox = parse_bbox_condition(bbox_condition)
        parsed_labels = parse_image_labels(image_labels, len(files))

        require(not is_micro or tier == "Gen-2.5-Extreme-High", "is_micro is only allowed on Gen-2.5-Extreme-High")
        if geometry_instruct_mode is not None:
            require(geometry_instruct_mode in GEOMETRY_INSTRUCT_MODES, "Invalid geometry_instruct_mode")
            require(geometry_instruct_mode != "creative" or tier in TIERS[2:], "creative mode requires Gen-2.5-Medium or higher")
        if is_symmetric is not None:
            require(is_symmetric in SYMMETRY_MODES, "Invalid is_symmetric value")

        cost = estimate_gen25_credits(tier, texture_mode)
        fields = {
            "tier": tier,
            "mesh_mode": mesh_mode,
            "quality": quality,
            "texture_mode": texture_mode,
            "material": material,
            "geometry_file_format": geometry_file_format,
            "prompt": clean_prompt or None,
            "quality_override": q_override,
            "soft": soft,
            "detail_level": parsed_detail,
            "seed": parsed_seed,
            "bbox_condition": parsed_bbox,
            "use_original_alpha": use_original_alpha,
            "uhd_texture": uhd_texture,
            "hd_texture": hd_texture,
            "quad_normal": quad_normal,
            "TAPose": ta_pose,
            "is_micro": is_micro,
            "preview_render": preview_render,
            "geometry_instruct_mode": geometry_instruct_mode,
            "is_symmetric": is_symmetric,
            "texture_delight": texture_delight,
        }
        repeated: list[tuple[str, str]] = [("addons", "HighPack")] if high_pack else []
        repeated.extend(("image_label", lbl) for lbl in parsed_labels)
        return client.submit_paid(
            "rodin",
            fields,
            [("images", p) for p in files],
            cost,
            approved_spend=approved_spend,
            max_credits=max_credits,
            repeated=repeated,
        )

    @_register
    def rodin_agentic(
        image_paths: list[str],
        prompt: str = "",
        tier: Literal["basic", "standard", "pro"] = "standard",
        export_type: Literal["glb", "usdz", "fbx", "stl"] = "glb",
        route: Literal["high-poly", "n-gons"] | None = None,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 / 1 / 2 credits), BETA: Rodin Agentic from 1–5 images.
        Omit route for automatic selection, or set route='high-poly' or 'n-gons'.
        Poll rodin_status/rodin_wait with job.subscription_key. Defaults to dry-run preview.
        """
        imgs = parse_image_paths(image_paths, required=True, max_count=5, allowed_extensions=IMAGE_EXTENSIONS, size_limit=MAX_IMAGE_SIZE)
        clean_prompt = parse_prompt(prompt, required=False)
        require(tier in AGENTIC_TIERS, f"Invalid agentic tier: {tier}")
        require(export_type in AGENTIC_FORMATS, f"Unsupported export_type: {export_type}")
        if route is not None:
            require(route in AGENTIC_ROUTES, f"Invalid route: {route}")

        cost = estimate_operation_credits("agentic", agentic_tier=tier)
        fields = {
            "tier": tier,
            "prompt": clean_prompt or None,
            "export_type": export_type,
            "route": route,
        }
        return client.submit_paid(
            "rodin_agentic",
            fields,
            [("images", p) for p in imgs],
            cost,
            approved_spend=approved_spend,
            max_credits=max_credits,
        )

    @_register
    def rodin_bang_asset(
        asset_id: str,
        instruction: str = "",
        strength: int = 5,
        mesh_mode: Literal["Raw", "Quad"] = "Raw",
        quality_override: int | None = None,
        material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] = "PBR",
        resolution: Literal["Basic", "High"] = "Basic",
        geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
        escore: float = 3.5,
        reference_scale: float = 0.5,
        seed: int | None = None,
        preview_render: bool = False,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 credit): BANG! split/segment a completed Rodin asset (by top-level uuid).
        Supports optional natural-language split instruction and preview_render. Defaults to preview.
        """
        parsed_asset = parse_uuid(asset_id, "asset_id")
        clean_instruction = parse_prompt(instruction, required=False)
        parsed_strength = parse_int_range(strength, name="strength", minimum=1, maximum=12)
        require(mesh_mode in MESH_MODES, f"Invalid mesh_mode: {mesh_mode}")
        require(material in MATERIALS, f"Invalid material: {material}")
        require(resolution in RESOLUTIONS, f"Invalid resolution: {resolution}")
        require(geometry_file_format in FORMATS, f"Unsupported format: {geometry_file_format}")
        parsed_escore = parse_float_range(escore, name="escore", minimum=0.0, maximum=10.0)
        parsed_ref = parse_float_range(reference_scale, name="reference_scale", minimum=0.0, maximum=1.0)
        parsed_seed = parse_seed(seed)
        max_faces = 50_000 if mesh_mode == "Quad" else 2_000_000
        q_override = parse_int_range(quality_override, name="quality_override", minimum=500, maximum=max_faces)

        fields = {
            "asset_id": parsed_asset,
            "instruction": clean_instruction or None,
            "strength": parsed_strength,
            "mesh_mode": mesh_mode,
            "quality_override": q_override,
            "material": material,
            "resolution": resolution,
            "geometry_file_format": geometry_file_format,
            "escore": parsed_escore,
            "reference_scale": parsed_ref,
            "seed": parsed_seed,
            "preview_render": preview_render,
        }
        return client.submit_paid("bang", fields, [], 0.5, approved_spend=approved_spend, max_credits=max_credits)

    @_register
    def rodin_bang_model(
        model_path: str,
        image_path: str | None = None,
        prompt: str = "",
        instruction: str = "",
        strength: int = 5,
        mesh_mode: Literal["Raw", "Quad"] = "Raw",
        quality_override: int | None = None,
        material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] = "PBR",
        resolution: Literal["Basic", "High"] = "Basic",
        geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
        escore: float = 3.5,
        reference_scale: float = 0.5,
        seed: int | None = None,
        preview_render: bool = False,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 credit): BANG! split/segment an uploaded 3D model (obj/glb/stl/fbx/usd*).
        Reference image is required unless material='None' and instruction is empty. Defaults to preview.
        """
        model = parse_local_file(model_path, allowed_extensions=STRICT_MODEL_EXTENSIONS, size_limit=MAX_MODEL_SIZE, label="model")
        img = (
            parse_local_file(image_path, allowed_extensions=IMAGE_EXTENSIONS, size_limit=MAX_IMAGE_SIZE, label="image")
            if image_path
            else None
        )
        clean_prompt = parse_prompt(prompt, required=False)
        clean_instruction = parse_prompt(instruction, required=False)
        require(mesh_mode in MESH_MODES, f"Invalid mesh_mode: {mesh_mode}")
        require(material in MATERIALS, f"Invalid material: {material}")
        require(resolution in RESOLUTIONS, f"Invalid resolution: {resolution}")
        require(geometry_file_format in FORMATS, f"Unsupported format: {geometry_file_format}")
        require(
            img is not None or (material == "None" and not clean_instruction),
            "Reference image_path is required when material != 'None' or instruction is provided",
        )
        parsed_strength = parse_int_range(strength, name="strength", minimum=1, maximum=12)
        parsed_escore = parse_float_range(escore, name="escore", minimum=0.0, maximum=10.0)
        parsed_ref = parse_float_range(reference_scale, name="reference_scale", minimum=0.0, maximum=1.0)
        parsed_seed = parse_seed(seed)
        max_faces = 50_000 if mesh_mode == "Quad" else 2_000_000
        q_override = parse_int_range(quality_override, name="quality_override", minimum=500, maximum=max_faces)

        paths: list[tuple[str, Path]] = [("model", model)]
        if img is not None:
            paths.append(("image", img))
        fields = {
            "prompt": clean_prompt or None,
            "instruction": clean_instruction or None,
            "strength": parsed_strength,
            "mesh_mode": mesh_mode,
            "quality_override": q_override,
            "material": material,
            "resolution": resolution,
            "geometry_file_format": geometry_file_format,
            "escore": parsed_escore,
            "reference_scale": parsed_ref,
            "seed": parsed_seed,
            "preview_render": preview_render,
        }
        return client.submit_paid("bang", fields, paths, 0.5, approved_spend=approved_spend, max_credits=max_credits)

    @_register
    def rodin_texture(
        model_path: str,
        image_path: str | None = None,
        image_paths: list[str] | None = None,
        image_labels: list[str] | None = None,
        prompt: str = "",
        texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] = "high",
        resolution: Literal["Basic", "High"] = "Basic",
        material: Literal["PBR", "Shaded", "Hybrid", "All"] = "PBR",
        geometry_file_format: Literal["glb", "fbx", "obj", "usdz", "stl"] = "glb",
        escore: float = 3.5,
        reference_scale: float = 1.0,
        hd_texture: bool = False,
        uhd_texture: bool = False,
        texture_delight: bool = False,
        preview_render: bool = False,
        seed: int | None = None,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 or 2.5 credits): Generate 2K–12K textures for an uploaded 3D model using 1–5 reference images.
        Accepts image_paths (1–5) or a single image_path. 4K/8K uses uhd_texture + resolution; 6K/12K uses texture_mode='extreme-high'.
        Defaults to dry-run preview; requires approved_spend=True and sufficient max_credits.
        """
        require(not (image_path and image_paths), "Provide either image_path or image_paths, not both")
        raw_images = list(image_paths) if image_paths is not None else ([image_path] if image_path else [])
        imgs = parse_image_paths(
            raw_images,
            required=True,
            max_count=5,
            allowed_extensions=IMAGE_EXTENSIONS,
            size_limit=MAX_TEXTURE_IMAGE_SIZE,
        )
        model = parse_local_file(
            model_path,
            allowed_extensions=MODEL_EXTENSIONS,
            size_limit=MAX_TEXTURE_MODEL_SIZE,
            label="model",
        )
        clean_prompt = parse_prompt(prompt, required=False)
        parsed_labels = parse_image_labels(image_labels, len(imgs))
        require(texture_mode in TEXTURE_MODES, f"Invalid texture_mode: {texture_mode}")
        require(resolution in RESOLUTIONS, f"Invalid resolution: {resolution}")
        require(material in TEXTURE_MATERIALS, f"Invalid material: {material}")
        require(geometry_file_format in FORMATS, f"Unsupported format: {geometry_file_format}")
        parsed_escore = parse_float_range(escore, name="escore", minimum=0.0, maximum=10.0)
        parsed_ref = parse_float_range(reference_scale, name="reference_scale", minimum=0.5, maximum=1.0)
        parsed_seed = parse_seed(seed)

        cost = estimate_operation_credits("texture", texture_mode=texture_mode)
        fields = {
            "prompt": clean_prompt or None,
            "texture_mode": texture_mode,
            "resolution": resolution,
            "material": material,
            "geometry_file_format": geometry_file_format,
            "escore": parsed_escore,
            "reference_scale": parsed_ref,
            "hd_texture": hd_texture,
            "uhd_texture": uhd_texture,
            "texture_delight": texture_delight,
            "preview_render": preview_render,
            "seed": parsed_seed,
        }
        paths: list[tuple[str, Path]] = [("images", img) for img in imgs]
        paths.append(("model", model))
        repeated = [("image_label", lbl) for lbl in parsed_labels]
        return client.submit_paid(
            "rodin_texture_only",
            fields,
            paths,
            cost,
            approved_spend=approved_spend,
            max_credits=max_credits,
            repeated=repeated,
        )

    @_register
    def rodin_rigging(
        asset_id: str | None = None,
        model_path: str | None = None,
        image_path: str | None = None,
        bone_template: Literal["auto", "human", "quadruped", "bird", "dragon", "kaiju", "custom"] = "auto",
        skinning_method: Literal["shell", "core"] = "shell",
        hierarchy: list[dict[str, Any]] | None = None,
        skeleton_description: str | None = None,
        animation_presets: list[str] | None = None,
        animation_paths: list[str] | None = None,
        geometry_file_format: Literal["glb", "fbx", "usdz"] = "glb",
        export_skeleton_type: Literal["rodin", "ue5", "unity", "actorcore"] = "rodin",
        in_place: bool = False,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 credit): Rig a confirmed Rodin asset OR an uploaded self-contained model (+Y up, +Z forward).
        Presets: auto/human/quadruped/bird/dragon/kaiju/custom. For auto without image, API renders a clay preview.
        Custom requires exactly one of hierarchy (1–256 bones) or skeleton_description.
        export_skeleton_type ue5/unity/actorcore requires asset_id + fbx + humanoid rig (auto/human).
        Up to 10 distinct stock animation_presets OR up to 10 single-clip FBX animation_paths (mutually exclusive).
        Defaults to dry-run preview.
        """
        parsed_asset = parse_optional_uuid(asset_id, "asset_id")
        require(bool(parsed_asset) != bool(model_path), "Provide exactly one of asset_id or model_path")
        require(bone_template in BONE_TEMPLATES, f"Invalid bone_template: {bone_template}")
        require(skinning_method in SKINNING_METHODS, f"Invalid skinning_method: {skinning_method}")
        require(geometry_file_format in RIGGING_FORMATS, "Rigging only exports glb, fbx, or usdz")
        require(export_skeleton_type in EXPORT_SKELETON_TYPES, f"Invalid export_skeleton_type: {export_skeleton_type}")

        if export_skeleton_type in ENGINE_SKELETONS:
            require(
                bool(parsed_asset) and geometry_file_format == "fbx" and bone_template in {"auto", "human"},
                "export_skeleton_type ue5/unity/actorcore requires asset_id, fbx, and a humanoid rig (auto or human)",
            )

        description = skeleton_description.strip() if isinstance(skeleton_description, str) and skeleton_description.strip() else None
        if skeleton_description is not None and not isinstance(skeleton_description, str):
            raise ValueError("skeleton_description must be a string")

        parsed_hierarchy = parse_rig_hierarchy(hierarchy)
        if bone_template == "custom":
            require(
                bool(parsed_hierarchy) != bool(description),
                "bone_template='custom' requires exactly one of hierarchy or skeleton_description",
            )
        else:
            require(
                parsed_hierarchy is None and description is None,
                "Preset bone templates do not accept hierarchy or skeleton_description",
            )

        paths: list[tuple[str, Path]] = []
        if model_path:
            paths.append(("model", parse_local_file(model_path, allowed_extensions=STRICT_MODEL_EXTENSIONS, size_limit=MAX_MODEL_SIZE, label="model")))
        if image_path:
            paths.append(("image", parse_local_file(image_path, allowed_extensions=SYNC_IMAGE_EXTENSIONS, size_limit=MAX_IMAGE_SIZE, label="image")))

        presets = list(animation_presets or [])
        clips = list(animation_paths or [])
        require(not (presets and clips), "animation_presets and animation_paths are mutually exclusive")
        require(len(presets) <= 10 and len(clips) <= 10, "At most 10 animations are allowed")
        require(all(isinstance(p, str) and bool(p.strip()) for p in presets), "Animation preset IDs must be non-blank strings")
        require(len(set(presets)) == len(presets), "animation_presets must contain distinct IDs")
        require(not (in_place and clips), "in_place cannot be used with custom animation_paths")

        paths.extend(
            ("animation_files", parse_local_file(p, allowed_extensions=ANIMATION_EXTENSIONS, size_limit=MAX_ANIMATION_SIZE, label="animation"))
            for p in clips
        )

        fields = {
            "asset_id": parsed_asset,
            "bone_template": bone_template,
            "skinning_method": skinning_method,
            "hierarchy": parsed_hierarchy,
            "skeleton_description": description,
            "geometry_file_format": geometry_file_format,
            "export_skeleton_type": None if export_skeleton_type == "rodin" else export_skeleton_type,
            "in_place": True if in_place else None,
        }
        result = client.submit_paid(
            "rigging",
            fields,
            paths,
            0.5,
            approved_spend=approved_spend,
            max_credits=max_credits,
            repeated=[("animation_preset", p.strip()) for p in presets],
        )
        result["animation_presets"] = [p.strip() for p in presets]
        return result

    @_register
    def rodin_animation_catalog(
        category: Literal[
            "human", "canine", "bird", "fish", "heavyhooved", "leanhooved", "lizard", "rodentia", "seaturtle"
        ] | None = None,
    ) -> dict[str, Any]:
        """FREE: List built-in rigging animations with permanent animation_id, frames, duration, loop metadata, and preview_url."""
        if category is not None:
            require(category in ANIMATION_CATEGORIES, f"Invalid animation category: {category}")
        payload = client.request_raw(
            "GET",
            "rigging_animations",
            retryable=True,
            params={"category": category} if category is not None else None,
        )
        if isinstance(payload, dict) and payload.get("error"):
            raise RuntimeError(f"Rodin error {payload['error']}: {payload.get('message', '')}".strip())
        if not isinstance(payload, list):
            raise RuntimeError("Unexpected animation catalog response format")
        return {"category": category, "count": len(payload), "animations": payload}

    @_register
    def rodin_polygen(
        asset_id: str | None = None,
        model_path: str | None = None,
        image_paths: list[str] | None = None,
        image_labels: list[str] | None = None,
        prompt: str = "",
        face_count: int | None = None,
        mesh_mode: Literal["Quad", "Raw", "NGon"] = "Quad",
        geometry_file_format: Literal["obj", "glb", "usdz", "fbx", "stl"] = "obj",
        material: Literal["PBR", "Shaded", "Hybrid", "All", "None"] | None = None,
        texture_mode: Literal["legacy", "extreme-low", "low", "medium", "high", "extreme-high"] | None = None,
        uhd_texture: bool = False,
        resolution: Literal["Basic", "High"] = "Basic",
        transfer_texture: bool = True,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 credit): PolyGen Smart Low-Poly (200–10,000 faces, Quad/Raw/NGon) from a Rodin asset_id,
        uploaded model, 1–5 reference images, or text prompt. Defaults to dry-run preview.
        """
        parsed_asset = parse_optional_uuid(asset_id, "asset_id")
        require(not (parsed_asset and model_path), "asset_id and model_path are mutually exclusive")
        images = parse_image_paths(image_paths, required=False, max_count=5, allowed_extensions=IMAGE_EXTENSIONS, size_limit=MAX_IMAGE_SIZE)
        clean_prompt = parse_prompt(prompt, required=False)
        require(bool(parsed_asset or model_path or images or clean_prompt), "Provide asset_id, model_path, image_paths, or prompt")
        parsed_faces = parse_int_range(face_count, name="face_count", minimum=200, maximum=10_000)
        require(mesh_mode in POLYGEN_MESH_MODES, f"Invalid mesh_mode: {mesh_mode}")
        require(geometry_file_format in FORMATS, f"Unsupported format: {geometry_file_format}")
        require(resolution in RESOLUTIONS, f"Invalid resolution: {resolution}")
        if material is not None:
            require(material in MATERIALS, f"Invalid material: {material}")
        if texture_mode is not None:
            require(texture_mode in TEXTURE_MODES, f"Invalid texture_mode: {texture_mode}")

        if model_path:
            require(
                material is None and texture_mode is None and not uhd_texture and not clean_prompt,
                "Uploaded models transfer their own textures: omit material, texture_mode, uhd_texture, and prompt",
            )
        else:
            require(transfer_texture, "transfer_texture=False only applies when uploading model_path")

        parsed_labels = parse_image_labels(image_labels, len(images))
        paths: list[tuple[str, Path]] = []
        if model_path:
            paths.append(("model", parse_local_file(model_path, allowed_extensions=STRICT_MODEL_EXTENSIONS, size_limit=MAX_MODEL_SIZE, label="model")))
        paths.extend(("images", p) for p in images)

        fields = {
            "asset_id": parsed_asset,
            "prompt": clean_prompt or None,
            "face_count": parsed_faces,
            "mesh_mode": mesh_mode,
            "geometry_file_format": geometry_file_format,
            "material": material,
            "texture_mode": texture_mode,
            "uhd_texture": True if uhd_texture else None,
            "resolution": resolution,
            "transfer_texture": False if model_path and not transfer_texture else None,
            "image_label": parsed_labels or None,
        }
        return client.submit_paid("polygen", fields, paths, 0.5, approved_spend=approved_spend, max_credits=max_credits)

    @_register
    def rodin_images(
        prompt: str,
        size: str | None = None,
        image_paths: list[str] | None = None,
        output_dir: str | None = None,
        approved_spend: bool = False,
        max_credits: float = 0,
    ) -> dict[str, Any]:
        """PAID (0.5 credit), SYNCHRONOUS: Generate an image from text or edit using 1–3 PNG/JPEG/WebP references (<=10 MiB).
        Saves decoded image to RODIN_OUTPUT_DIR or ~/Downloads/Hyper3D and returns local path (never base64).
        Defaults to dry-run preview.
        """
        clean_prompt = parse_prompt(prompt, required=True, max_length=4096)
        parsed_size = parse_image_size(size)
        refs = parse_image_paths(
            image_paths,
            required=False,
            max_count=3,
            allowed_extensions=SYNC_IMAGE_EXTENSIONS,
            size_limit=MAX_SYNC_IMAGE_SIZE,
        )
        fields: dict[str, Any] = {"prompt": clean_prompt}
        if parsed_size is not None:
            fields["size"] = parsed_size

        preview = {
            "endpoint": "images",
            "estimated_credits": 0.5,
            "parameters": fields,
            "files": [{"field": "images", "name": p.name} for p in refs],
        }
        if not approved_spend:
            return {
                "submitted": False,
                "reason": "Explicit approval required (approved_spend=True); no credits were spent.",
                **preview,
            }

        verify_spend_budget(0.5, max_credits)
        target_dir = client.config.output_dir(output_dir)
        target_dir.mkdir(parents=True, exist_ok=True)

        if refs:
            with ExitStack() as stack:
                parts: list[tuple[str, tuple[Any, ...]]] = [(k, (None, v)) for k, v in fields.items()]
                parts.extend(
                    ("images", (p.name, stack.enter_context(p.open("rb")), mimetypes.guess_type(p.name)[0] or "application/octet-stream"))
                    for p in refs
                )
                payload = client.request_dict("POST", "images", retryable=False, files=parts)
        else:
            payload = client.request_dict("POST", "images", retryable=False, json=fields)

        data = payload.get("data")
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise RuntimeError("Images API response did not include an image; check history before retrying.")

        saved_entry = save_decoded_image(data[0], output_dir=target_dir)
        return {
            "submitted": True,
            **preview,
            "actual_credits": payload.get("consumed"),
            "created": payload.get("created"),
            "saved_files": [saved_entry],
        }

    @_register
    def rodin_status(subscription_key: str) -> dict[str, Any]:
        """FREE: Check task status once using jobs.subscription_key (or job.subscription_key for Agentic)."""
        require(isinstance(subscription_key, str) and bool(subscription_key.strip()), "subscription_key is required")
        return client.request_dict(
            "POST",
            "status",
            retryable=True,
            json={"subscription_key": subscription_key.strip()},
        )

    @_register
    def rodin_wait(
        subscription_key: str,
        timeout_seconds: int = 45,
        poll_interval_seconds: int = 5,
        max_interval_seconds: int = 30,
    ) -> dict[str, Any]:
        """FREE: Poll task status with backoff (honoring HTTP 429 Retry-After) until Done, Failed, or timeout (1..120s)."""
        return client.wait_for_task(
            subscription_key,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_interval_seconds=max_interval_seconds,
        )

    @_register
    def rodin_download_links(task_uuid: str) -> dict[str, Any]:
        """FREE: Fetch temporary signed result URLs using top-level task uuid after all jobs are Done."""
        parsed = parse_uuid(task_uuid, "task_uuid")
        return client.request_dict("POST", "download", retryable=True, json={"task_uuid": parsed})

    @_register
    def rodin_save_results(task_uuid: str, output_dir: str | None = None) -> dict[str, Any]:
        """FREE: Fetch signed URLs and download finished artifacts atomically with DNS-checked SSRF protection."""
        parsed = parse_uuid(task_uuid, "task_uuid")
        data = client.request_dict("POST", "download", retryable=True, json={"task_uuid": parsed})
        target_dir = client.config.output_dir(output_dir)
        download_kwargs: dict[str, Any] = {
            "task_uuid": parsed,
            "output_dir": target_dir,
            "timeout": (client.config.connect_timeout, client.config.read_timeout),
            "resolve_dns": resolve_dns,
        }
        if get_fn is not None:
            download_kwargs["get_fn"] = get_fn
        saved = download_signed_files(list(data.get("list") or []), **download_kwargs)
        return {"task_uuid": parsed, "output_dir": str(target_dir), "saved_files": saved}

    @_register
    def rodin_recent_jobs(limit: int = 20) -> dict[str, Any]:
        """FREE: List recent paid jobs submitted during this MCP server session (task_uuid and subscription_key)."""
        jobs = client.job_store.recent(limit)
        return {"count": len(jobs), "jobs": jobs}

    return registered
