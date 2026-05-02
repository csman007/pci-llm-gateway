import pytest
from model_registry import MODEL_NAMES, get_client


def test_model_names_not_empty():
    assert len(MODEL_NAMES) > 0


def test_claude_model_returns_anthropic_client():
    from anthropic_client import AnthropicClient
    client = get_client("claude-sonnet-4-6")
    assert isinstance(client, AnthropicClient)


def test_gpt_model_returns_openai_client():
    from openai_client import OpenAIClient
    client = get_client("gpt-4o")
    assert isinstance(client, OpenAIClient)


def test_unknown_model_raises():
    with pytest.raises(ValueError, match="No client registered"):
        get_client("unknown-model-xyz")
