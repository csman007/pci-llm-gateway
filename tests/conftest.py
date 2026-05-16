import sys
import os
from pathlib import Path

_root = Path(__file__).parent.parent

for service in ["pii-detector", "prompt-processor", "llm-client", "output-filter", "api-gateway", "agent", "rag", "observability"]:
    sys.path.insert(0, str(_root / "services" / service))

os.environ.setdefault("ENV", "dev")
os.environ.setdefault("JWT_SECRET", "test-secret-that-is-long-enough-for-hs256")
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
