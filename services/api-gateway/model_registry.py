from pathlib import Path
import yaml
from anthropic_client import AnthropicClient
from openai_client import OpenAIClient

_config_path = Path(__file__).parent.parent.parent / "models.yaml"

with open(_config_path) as f:
    _data = yaml.safe_load(f)

MODELS: list[dict] = _data["models"]
MODEL_NAMES: list[str] = [m["name"] for m in MODELS]

_PROVIDER_CLIENTS = {
    "claude": AnthropicClient(),
    "gpt": OpenAIClient(),
}


def get_client(model: str):
    for entry in MODELS:
        if entry["name"] == model:
            return _PROVIDER_CLIENTS[entry["provider"]]
    raise ValueError(f"No client registered for model '{model}'")
