"""Credit estimation and explicit spend-approval guards for paid Rodin operations."""

from __future__ import annotations

import math
from typing import Literal

from .config import AGENTIC_TIERS, TEXTURE_MODES, TIERS

OperationKind = Literal[
    "gen2.5", "agentic", "bang", "texture", "rigging", "polygen", "images",
]

AGENTIC_COSTS: dict[str, float] = {
    "basic": 0.5,
    "standard": 1.0,
    "pro": 2.0,
}

BASE_CREDIT_COST = 0.5
EXTREME_HIGH_TIER_SURCHARGE = 0.5
EXTREME_HIGH_TEXTURE_SURCHARGE = 2.0


def estimate_gen25_credits(tier: str, texture_mode: str) -> float:
    """Return estimated credits for a Gen-2.5 generation request."""
    if tier not in TIERS:
        raise ValueError(f"Invalid tier: {tier}")
    if texture_mode not in TEXTURE_MODES:
        raise ValueError(f"Invalid texture_mode: {texture_mode}")
    extra_tier = EXTREME_HIGH_TIER_SURCHARGE if tier == "Gen-2.5-Extreme-High" else 0.0
    extra_tex = EXTREME_HIGH_TEXTURE_SURCHARGE if texture_mode == "extreme-high" else 0.0
    return BASE_CREDIT_COST + extra_tier + extra_tex


def estimate_operation_credits(
    operation: str = "gen2.5",
    *,
    tier: str = "Gen-2.5-High",
    texture_mode: str = "high",
    agentic_tier: str = "standard",
) -> float:
    """Estimate credits locally for any documented paid REST v2 operation."""
    if operation == "gen2.5":
        return estimate_gen25_credits(tier, texture_mode)
    if operation in {"bang", "rigging", "polygen", "images"}:
        return BASE_CREDIT_COST
    if operation == "texture":
        if texture_mode not in TEXTURE_MODES:
            raise ValueError(f"Invalid texture_mode: {texture_mode}")
        return 2.0 if texture_mode == "extreme-high" else BASE_CREDIT_COST
    if operation == "agentic":
        if agentic_tier not in AGENTIC_TIERS:
            raise ValueError(f"Invalid agentic_tier: {agentic_tier}")
        return AGENTIC_COSTS[agentic_tier]
    raise ValueError(f"Unsupported operation: {operation}")


def verify_spend_budget(estimated_credits: float, max_credits: float) -> None:
    """Reject non-finite, non-positive, or insufficient budgets before any HTTP call."""
    if isinstance(max_credits, bool) or not isinstance(max_credits, (int, float)):
        raise ValueError("max_credits must be a finite number")
    if not math.isfinite(max_credits) or max_credits <= 0 or max_credits < estimated_credits:
        raise ValueError(
            f"Estimated cost {estimated_credits} exceeds finite budget max_credits={max_credits}"
        )
