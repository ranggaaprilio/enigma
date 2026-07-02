from decimal import Decimal

from app.core.pricing import DEFAULT_PRICING, estimate_cost, merge_pricing


class TestEstimateCost:
    def test_input_output_split_known_model(self):
        # gpt-4.1-mini: 0.40 input + 1.60 output per 1M tokens.
        cost = estimate_cost(
            "openai", "gpt-4.1-mini", input_tokens=1_000_000, output_tokens=1_000_000
        )
        assert cost == Decimal("2.000000")

    def test_partial_split_uses_present_side(self):
        cost = estimate_cost("openai", "gpt-4.1-mini", input_tokens=1_000_000, output_tokens=0)
        assert cost == Decimal("0.400000")

    def test_total_only_falls_back_to_output_rate(self):
        cost = estimate_cost("openai", "gpt-4.1-mini", total_tokens=1_000_000)
        assert cost == Decimal("1.600000")

    def test_split_takes_priority_over_total(self):
        cost = estimate_cost(
            "openai",
            "gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            total_tokens=5_000_000,
        )
        assert cost == Decimal("2.000000")

    def test_unknown_model_uses_provider_fallback(self):
        cost = estimate_cost("openai", "brand-new-model", total_tokens=1_000_000)
        # openai fallback output rate is 1.60/1M.
        assert cost == Decimal("1.600000")

    def test_unknown_provider_returns_none(self):
        assert estimate_cost("nonexistent", "whatever", total_tokens=100) is None

    def test_mock_provider_is_free(self):
        cost = estimate_cost("mock", "mock-agent", input_tokens=10, output_tokens=5)
        assert cost == Decimal("0.000000")

    def test_no_token_counts_returns_none(self):
        assert estimate_cost("openai", "gpt-4.1-mini") is None

    def test_case_insensitive_provider(self):
        cost = estimate_cost("OpenAI", "gpt-4.1-mini", total_tokens=1_000_000)
        assert cost == Decimal("1.600000")

    def test_small_token_counts_quantized(self):
        cost = estimate_cost("openai", "gpt-4.1-mini", input_tokens=100, output_tokens=50)
        # (100 * 0.40 + 50 * 1.60) / 1_000_000 = 0.00012
        assert cost == Decimal("0.000120")


class TestOverrides:
    def test_override_existing_model(self):
        overrides = {"openai": {"gpt-4.1-mini": {"input_per_1m": 1.0, "output_per_1m": 2.0}}}
        cost = estimate_cost(
            "openai",
            "gpt-4.1-mini",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            overrides=overrides,
        )
        assert cost == Decimal("3.000000")

    def test_override_adds_new_provider(self):
        overrides = {"anthropic": {"claude": {"input_per_1m": 3.0, "output_per_1m": 15.0}}}
        cost = estimate_cost(
            "anthropic", "claude", input_tokens=1_000_000, output_tokens=0, overrides=overrides
        )
        assert cost == Decimal("3.000000")

    def test_merge_does_not_mutate_defaults(self):
        overrides = {"openai": {"gpt-4.1-mini": {"input_per_1m": 99.0, "output_per_1m": 99.0}}}
        merge_pricing(overrides)
        # Original default is untouched.
        assert DEFAULT_PRICING["openai"]["gpt-4.1-mini"].input_per_1m == Decimal("0.40")

    def test_empty_overrides_returns_defaults(self):
        assert merge_pricing(None) is DEFAULT_PRICING
        assert merge_pricing({}) is DEFAULT_PRICING
