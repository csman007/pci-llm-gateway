from pydantic import BaseModel, Field


class ToolCallRecord(BaseModel):
    """Record of a single tool invocation made during an agent run."""

    tool_name: str
    tool_input: dict
    tool_result: str
    error: bool = Field(description="True if the tool returned an ERROR: prefixed result.")


class EvaluationResult(BaseModel):
    """LLM-as-judge assessment of the agent's final response."""

    score: float = Field(ge=0.0, le=1.0, description="Quality score from 0.0 (poor) to 1.0 (excellent).")
    reasoning: str = Field(description="One-sentence explanation of the score.")


class AgentRequest(BaseModel):
    """Request body for POST /v1/agent/run and POST /v1/agent/stream."""

    question: str = Field(..., min_length=1, max_length=32_000)
    thinking: bool = Field(
        default=False,
        description="Enable extended thinking. Automatically selects Opus model.",
    )
    max_tokens: int = Field(
        default=2048,
        ge=256,
        le=8192,
        description="Maximum tokens for each orchestrator response turn.",
    )


class AgentRunResponse(BaseModel):
    """Response body for POST /v1/agent/run."""

    response: str = Field(description="The agent's final answer.")
    tool_trace: list[ToolCallRecord] = Field(description="Ordered list of all tool calls made.")
    thinking: list[str] | None = Field(
        default=None,
        description="Extended thinking block texts, populated only when thinking=True.",
    )
    evaluation: EvaluationResult
    steps_taken: int = Field(description="Number of tool-use iterations completed.")
