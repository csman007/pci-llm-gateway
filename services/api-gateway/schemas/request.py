from model_registry import MODEL_NAMES
from pydantic import BaseModel, Field, field_validator


class InferenceRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=32_000)
    model: str = MODEL_NAMES[0]
    max_tokens: int = Field(default=1024, ge=1, le=4096)
    system: str | None = None

    @field_validator("model")
    @classmethod
    def model_must_be_supported(cls, v: str) -> str:
        if v not in MODEL_NAMES:
            raise ValueError(f"Unsupported model '{v}'. Available: {MODEL_NAMES}")
        return v


class InferenceResponse(BaseModel):
    response: str
    pii_findings: int
