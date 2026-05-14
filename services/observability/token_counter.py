"""Token usage cost calculator for LLM API calls.

Pricing table defaults are USD per million tokens (input then output).
Every price can be overridden via environment variables so pricing can be
updated without a code change (e.g. COST_PER_M_CLAUDE_SONNET_INPUT=3.00).
"""

import os

# ---------------------------------------------------------------------------
# Default pricing table: USD per million tokens (input, output)
# ---------------------------------------------------------------------------
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
    "claude-haiku-4-5-20251001": (0.80, 4.00),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "text-embedding-3-small": (0.02, 0.00),
    "text-embedding-3-large": (0.13, 0.00),
}

# Env-var name fragments per model — maps model key → env-var infix used to
# build the override variable names (COST_PER_M_{INFIX}_INPUT / _OUTPUT).
_ENV_INFIXES: dict[str, str] = {
    "claude-sonnet-4-6": "CLAUDE_SONNET",
    "claude-opus-4-7": "CLAUDE_OPUS",
    "claude-haiku-4-5-20251001": "CLAUDE_HAIKU",
    "gpt-4o": "GPT4O",
    "gpt-4o-mini": "GPT4O_MINI",
    "text-embedding-3-small": "EMBED_SMALL",
    "text-embedding-3-large": "EMBED_LARGE",
}


def _get_prices(model: str) -> tuple[float, float]:
    """Resolve input and output price-per-million for *model*.

    Checks env vars first (COST_PER_M_{INFIX}_INPUT / _OUTPUT), then falls
    back to the built-in pricing table, then returns (0.0, 0.0) for unknown
    models.

    Args:
        model: LLM model identifier string.

    Returns:
        Tuple of (input_price_per_million, output_price_per_million) in USD.
    """
    infix = _ENV_INFIXES.get(model)
    defaults = _DEFAULT_PRICING.get(model, (0.0, 0.0))

    if infix:
        input_price = float(os.environ.get(f"COST_PER_M_{infix}_INPUT", defaults[0]))
        output_price = float(os.environ.get(f"COST_PER_M_{infix}_OUTPUT", defaults[1]))
    else:
        input_price, output_price = defaults

    return input_price, output_price


def calculate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int = 0,
) -> dict:
    """Calculate the USD cost of a single LLM API call.

    Args:
        model:             LLM model identifier (e.g. "claude-sonnet-4-6").
        prompt_tokens:     Number of input tokens consumed.
        completion_tokens: Number of output tokens generated (default 0 for
                           embedding models that produce no completion).

    Returns:
        Dict with keys:
            model             — model identifier (str)
            prompt_tokens     — input token count (int)
            completion_tokens — output token count (int)
            total_tokens      — sum of prompt + completion (int)
            input_cost_usd    — cost of input tokens rounded to 8 d.p. (float)
            output_cost_usd   — cost of output tokens rounded to 8 d.p. (float)
            total_cost_usd    — sum of input + output costs (float)
    """
    input_price, output_price = _get_prices(model)

    input_cost = round(prompt_tokens * input_price / 1_000_000, 8)
    output_cost = round(completion_tokens * output_price / 1_000_000, 8)
    total_cost = round(input_cost + output_cost, 8)

    return {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "input_cost_usd": input_cost,
        "output_cost_usd": output_cost,
        "total_cost_usd": total_cost,
    }
