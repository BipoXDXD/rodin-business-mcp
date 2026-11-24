"""Configuration constants and credential lookup for Rodin-Business MCP."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Callable

API_BASE_URL = "https://api.hyper3d.com/api/v2"
ACCOUNT_API_BASE_URL = "https://api.hyper3d.com/api"
OFFICIAL_MCP_URL = "https://api.hyper3d.com/api/mcp"
OFFICIAL_DOCS_URL = "https://docs.hyper3d.ai/en"

KEYCHAIN_API_SERVICE = "hyper3d-business"
KEYCHAIN_OAUTH_SERVICE = "hyper3d-business-oauth"
KEYCHAIN_OAUTH_ACCOUNT = "api.hyper3d.com"

OAUTH_CLIENT_ID = "https://hyper3d.ai/oauth_cimd/cli.json"
OAUTH_SCOPES = "rodin:generate rodin:read account:read offline_access"
WALLET_UNITS_PER_CREDIT = 10.0

DEFAULT_OUTPUT_DIR = "~/Downloads/Hyper3D"
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 120.0

# File limits from OpenAPI v3 specifications
MAX_IMAGE_SIZE = 25 * 1024 * 1024
MAX_TEXTURE_IMAGE_SIZE = 100 * 1024 * 1024
MAX_TEXTURE_MODEL_SIZE = 100 * 1024 * 1024
MAX_SYNC_IMAGE_SIZE = 10 * 1024 * 1024
MAX_MODEL_SIZE = 512 * 1024 * 1024
MAX_ANIMATION_SIZE = 25 * 1024 * 1024

IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff",
    ".gif", ".avif", ".heic", ".heif", ".bmp", ".jp2",
})
SYNC_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
MODEL_EXTENSIONS = frozenset({
    ".obj", ".stl", ".gltf", ".glb", ".fbx", ".usdz", ".usd", ".usda", ".usdc",
})
STRICT_MODEL_EXTENSIONS = MODEL_EXTENSIONS - {".gltf"}
ANIMATION_EXTENSIONS = frozenset({".fbx"})

TIERS = (
    "Gen-2.5-Extreme-Low",
    "Gen-2.5-Low",
    "Gen-2.5-Medium",
    "Gen-2.5-High",
    "Gen-2.5-Extreme-High",
)
TEXTURE_MODES = ("legacy", "extreme-low", "low", "medium", "high", "extreme-high")
FORMATS = ("glb", "fbx", "obj", "usdz", "stl")
RIGGING_FORMATS = ("glb", "fbx", "usdz")
AGENTIC_FORMATS = ("glb", "usdz", "fbx", "stl")
AGENTIC_TIERS = ("basic", "standard", "pro")
AGENTIC_ROUTES = ("high-poly", "n-gons")
MATERIALS = ("PBR", "Shaded", "Hybrid", "All", "None")
TEXTURE_MATERIALS = ("PBR", "Shaded", "Hybrid", "All")
POLYGEN_MESH_MODES = ("Quad", "Raw", "NGon")
MESH_MODES = ("Raw", "Quad")
RESOLUTIONS = ("Basic", "High")
QUALITIES = ("extra-low", "low", "medium", "high")
SYMMETRY_MODES = ("symmetric", "balanced", "asymmetric", "unknown")
GEOMETRY_INSTRUCT_MODES = ("creative", "faithful")
IMAGE_LABELS = frozenset({"F", "FL", "FR", "B", "BL", "BR", "L", "R", "U", "D", "?"})
BONE_TEMPLATES = ("auto", "human", "quadruped", "bird", "dragon", "kaiju", "custom")
SKINNING_METHODS = ("shell", "core")
ENGINE_SKELETONS = frozenset({"ue5", "unity", "actorcore"})
EXPORT_SKELETON_TYPES = ("rodin", "ue5", "unity", "actorcore")
ANIMATION_CATEGORIES = (
    "human", "canine", "bird", "fish", "heavyhooved",
    "leanhooved", "lizard", "rodentia", "seaturtle",
)
UPLOAD_KINDS = ("image", "model", "animation")

ALLOWED_REST_ENDPOINTS = frozenset({
    "rodin",
    "rodin_agentic",
    "bang",
    "rodin_texture_only",
    "rigging",
    "polygen",
    "rigging_animations",
    "images",
    "status",
    "download",
    "check_balance",
})


@dataclass(frozen=True)
class ServerConfig:
    """Immutable runtime settings resolved at startup or injected in tests."""

    api_base_url: str = API_BASE_URL
    account_api_base_url: str = ACCOUNT_API_BASE_URL
    official_mcp_url: str = OFFICIAL_MCP_URL
    keychain_api_service: str = KEYCHAIN_API_SERVICE
    keychain_oauth_service: str = KEYCHAIN_OAUTH_SERVICE
    keychain_oauth_account: str = KEYCHAIN_OAUTH_ACCOUNT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT

    def output_dir(self, override: str | None = None) -> Path:
        raw = override.strip() if isinstance(override, str) and override.strip() else os.getenv("RODIN_OUTPUT_DIR", DEFAULT_OUTPUT_DIR)
        return Path(raw).expanduser().resolve()


class ApiKeyProvider:
    """Reads the Business API key from env or macOS Keychain without exposing it in args."""

    def __init__(
        self,
        service: str = KEYCHAIN_API_SERVICE,
        env_var: str = "RODIN_API_KEY",
        keychain_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._service = service
        self._env_var = env_var
        self._runner = keychain_runner
        self._cached: str | None = None

    def get(self) -> str:
        env_val = os.getenv(self._env_var, "").strip()
        if env_val:
            return env_val
        if self._cached:
            return self._cached
        if os.name == "posix" and Path("/usr/bin/security").exists():
            result = self._runner(
                ["/usr/bin/security", "find-generic-password", "-s", self._service, "-w"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                self._cached = result.stdout.strip()
                return self._cached
        raise RuntimeError(
            f"Missing Business API key: set {self._env_var} or store item "
            f"'{self._service}' in macOS Keychain."
        )

    def is_configured(self) -> bool:
        try:
            return bool(self.get())
        except RuntimeError:
            return False

    def clear_cache(self) -> None:
        self._cached = None
