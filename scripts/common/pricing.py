#!/usr/bin/env python3
"""Provider pricing configuration and normalized cost estimation (stdlib only).

Pricing is data, not code: ``config/pricing.yaml`` maps provider/model to
per-million-token prices. When no price is configured, every cost stays ``None``
and is never guessed. Providers change prices, so a JSONL row keeps the price
profile and currency that produced its estimate instead of being restated later.

This module performs no routing and calls no provider; it only turns normalized
token counts into an optional estimate. Local (Ollama) usage with no configured
price simply reports unknown cost.
"""

from __future__ import annotations

import copy
import pathlib
from typing import Any, Optional

import infra
import learning

SCHEMA_VERSION = 1
PRICING_VERSION = "0.1.0"

PRICE_FIELDS = (
    "normal_input_price_per_million",
    "cached_input_price_per_million",
    "output_price_per_million",
)
ENTRY_FIELDS = PRICE_FIELDS + ("profile",)
PROVIDER_FIELDS = ("models",)
TOP_FIELDS = ("schema_version", "pricing_version", "currency", "providers", "note", "as_of", "source")

COST_FIELDS = (
    "estimated_input_cost",
    "estimated_cached_input_cost",
    "estimated_output_cost",
    "estimated_total_cost",
    "pricing_profile",
    "pricing_currency",
)

DEFAULT_PRICING: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "pricing_version": PRICING_VERSION,
    "currency": None,
    "providers": {},
}


def empty_cost() -> dict[str, Any]:
    """A cost result with every field unknown."""
    return {field: None for field in COST_FIELDS}


def _check_price(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise infra.InfraError(f"{label} must be a non-negative number")


def _validate_pricing(config: dict[str, Any]) -> None:
    if config.get("schema_version") != SCHEMA_VERSION:
        raise infra.InfraError("Unsupported pricing schema_version")
    unknown = sorted(set(config) - set(TOP_FIELDS))
    if unknown:
        raise infra.InfraError(f"Pricing config has unknown fields: {', '.join(unknown)}")
    currency = config.get("currency")
    if currency is not None and (not isinstance(currency, str) or not currency.strip()):
        raise infra.InfraError("Pricing currency must be a non-empty string")
    providers = config.get("providers")
    if not isinstance(providers, dict):
        raise infra.InfraError("Pricing config must define a providers object")
    for provider, entry in providers.items():
        if not isinstance(provider, str) or not provider.strip():
            raise infra.InfraError("Pricing provider names must be non-empty strings")
        if not isinstance(entry, dict):
            raise infra.InfraError(f"Pricing provider '{provider}' must be an object")
        unexpected = sorted(set(entry) - set(PROVIDER_FIELDS))
        if unexpected:
            raise infra.InfraError(f"Pricing provider '{provider}' has unknown fields: {', '.join(unexpected)}")
        models = entry.get("models")
        if not isinstance(models, dict) or not models:
            raise infra.InfraError(f"Pricing provider '{provider}' must define a non-empty models object")
        for model, prices in models.items():
            if not isinstance(model, str) or not model.strip():
                raise infra.InfraError(f"Pricing provider '{provider}' has a non-string model id")
            if not isinstance(prices, dict):
                raise infra.InfraError(f"Pricing entry for '{provider}/{model}' must be an object")
            unexpected = sorted(set(prices) - set(ENTRY_FIELDS))
            if unexpected:
                raise infra.InfraError(
                    f"Pricing entry for '{provider}/{model}' has unknown fields: {', '.join(unexpected)}")
            if learning.contains_secret_like(str(prices)):
                raise infra.InfraError("Pricing config must not contain credentials")
            for field in PRICE_FIELDS:
                if field not in prices:
                    raise infra.InfraError(f"Pricing entry for '{provider}/{model}' is missing {field}")
                _check_price(prices[field], f"Pricing '{provider}/{model}' {field}")


def load_pricing(path: Optional[Any] = None) -> dict[str, Any]:
    """Load and validate pricing, falling back to an empty (all-unknown) profile."""
    if path is None:
        path = infra.ROOT / "config/pricing.yaml"
    path = pathlib.Path(path)
    if not path.exists():
        return copy.deepcopy(DEFAULT_PRICING)
    config = infra.load_json(path)
    _validate_pricing(config)
    return config


def resolve_price(pricing: dict[str, Any], provider: Optional[str], model: Optional[str]) -> Optional[dict[str, Any]]:
    """Return the price entry for a provider/model, or ``None`` when unconfigured."""
    if not provider or not model:
        return None
    entry = (pricing.get("providers") or {}).get(provider)
    if not isinstance(entry, dict):
        return None
    prices = (entry.get("models") or {}).get(model)
    return prices if isinstance(prices, dict) else None


def _token(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def estimate_usage_cost(
    provider: Optional[str],
    model: Optional[str],
    *,
    input_tokens: Optional[int] = None,
    cached_input_tokens: Optional[int] = None,
    cache_miss_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    pricing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Estimate provider cost from normalized tokens, or all-unknown when unpriced.

    ``estimated_input_cost`` prices cache-miss (normal) input tokens at the normal
    rate, matching provider cache-miss billing. A total is reported only when all
    three components are known; partial totals are never invented.
    """
    result = empty_cost()
    config = pricing if pricing is not None else load_pricing()
    prices = resolve_price(config, provider, model)
    if prices is None:
        return result
    result["pricing_profile"] = prices.get("profile") or f"{provider}/{model}"
    result["pricing_currency"] = config.get("currency")

    miss = _token(cache_miss_tokens)
    cached = _token(cached_input_tokens)
    inp = _token(input_tokens)
    output = _token(output_tokens)
    if miss is None and inp is not None and cached is not None and inp >= cached:
        miss = inp - cached

    input_cost = round(miss / 1_000_000 * prices["normal_input_price_per_million"], 8) if miss is not None else None
    cached_cost = (round(cached / 1_000_000 * prices["cached_input_price_per_million"], 8)
                   if cached is not None else None)
    output_cost = (round(output / 1_000_000 * prices["output_price_per_million"], 8)
                   if output is not None else None)
    result["estimated_input_cost"] = input_cost
    result["estimated_cached_input_cost"] = cached_cost
    result["estimated_output_cost"] = output_cost
    if input_cost is not None and cached_cost is not None and output_cost is not None:
        result["estimated_total_cost"] = round(input_cost + cached_cost + output_cost, 8)
    return result
