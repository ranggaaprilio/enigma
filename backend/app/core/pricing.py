"""Per-provider/model pricing used to derive an estimated cost from token counts.

Rates are expressed in USD per 1,000,000 tokens. They are **estimates** for cost
attribution/analytics and may drift from a provider's live rates — override them via the
``MODEL_PRICING_OVERRIDES`` env var (JSON) parsed into ``Settings.model_pricing_overrides``.

Override JSON shape (merged over the defaults below)::

    {"openai": {"gpt-4.1-mini": {"input_per_1m": 0.4, "output_per_1m": 1.6}}}
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# A per-provider fallback for models we do not price explicitly.
FALLBACK_MODEL_KEY = "*"
_TOKENS_PER_UNIT = Decimal(1_000_000)
_COST_QUANTUM = Decimal("0.000001")


@dataclass(frozen=True)
class ModelPrice:
    input_per_1m: Decimal
    output_per_1m: Decimal


# provider -> model -> price. ``"*"`` is the per-provider fallback for unlisted models.
DEFAULT_PRICING: dict[str, dict[str, ModelPrice]] = {
    "openai": {
        "gpt-4.1-mini": ModelPrice(Decimal("0.40"), Decimal("1.60")),
        "gpt-4.1": ModelPrice(Decimal("2.00"), Decimal("8.00")),
        "gpt-4o-mini": ModelPrice(Decimal("0.15"), Decimal("0.60")),
        "gpt-4o": ModelPrice(Decimal("2.50"), Decimal("10.00")),
        FALLBACK_MODEL_KEY: ModelPrice(Decimal("0.40"), Decimal("1.60")),
    },
    # OpenHands proxies an arbitrary underlying model; keep a generic fallback.
    "openhands": {
        FALLBACK_MODEL_KEY: ModelPrice(Decimal("0.40"), Decimal("1.60")),
    },
    "mock": {
        FALLBACK_MODEL_KEY: ModelPrice(Decimal("0"), Decimal("0")),
    },
}


def merge_pricing(
    overrides: dict | None,
) -> dict[str, dict[str, ModelPrice]]:
    """Return the default pricing table with ``overrides`` merged over it."""
    if not overrides:
        return DEFAULT_PRICING
    merged: dict[str, dict[str, ModelPrice]] = {
        provider: dict(models) for provider, models in DEFAULT_PRICING.items()
    }
    for provider, models in overrides.items():
        target = merged.setdefault(provider.lower(), {})
        for model, price in models.items():
            target[model] = ModelPrice(
                Decimal(str(price["input_per_1m"])),
                Decimal(str(price["output_per_1m"])),
            )
    return merged


def _lookup(
    table: dict[str, dict[str, ModelPrice]], provider: str, model: str
) -> ModelPrice | None:
    provider_table = table.get((provider or "").lower())
    if provider_table is None:
        return None
    return provider_table.get(model) or provider_table.get(FALLBACK_MODEL_KEY)


def estimate_cost(
    provider: str,
    model: str,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
    overrides: dict | None = None,
) -> Decimal | None:
    """Estimate the USD cost of a run.

    Uses the input/output split when either is present; otherwise falls back to applying the
    output rate to ``total_tokens``. Returns ``None`` when the provider is unknown or no token
    counts are supplied.
    """
    price = _lookup(merge_pricing(overrides), provider, model)
    if price is None:
        return None
    if input_tokens is not None or output_tokens is not None:
        cost = (
            Decimal(input_tokens or 0) * price.input_per_1m
            + Decimal(output_tokens or 0) * price.output_per_1m
        ) / _TOKENS_PER_UNIT
    elif total_tokens is not None:
        cost = Decimal(total_tokens) * price.output_per_1m / _TOKENS_PER_UNIT
    else:
        return None
    return cost.quantize(_COST_QUANTUM)
