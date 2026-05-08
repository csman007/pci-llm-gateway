import json

import anthropic
from agent_pipeline import AgentPipeline
from evaluator import LLMJudge
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from orchestrator import AgentOrchestrator
from schemas.agent_schemas import AgentRequest, AgentRunResponse
from secret_resolver import resolve_env_secret
from subagents import SubagentRunner

router = APIRouter()

# Shared AsyncAnthropic client — one instance for the whole agent layer.
_anthropic_client = anthropic.AsyncAnthropic(
    api_key=resolve_env_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY")
)

# Module-level singletons, same pattern as routes/inference.py.
_pipeline = AgentPipeline()
_subagent_runner = SubagentRunner(_anthropic_client)
_judge = LLMJudge(_anthropic_client)
_orchestrator = AgentOrchestrator(_anthropic_client, _pipeline, _subagent_runner, _judge)


@router.post("/agent/run", response_model=AgentRunResponse)
async def agent_run(request: AgentRequest):
    """Run the agent to completion and return the full result.

    The agent may call tools multiple times before producing its final answer.
    Set thinking=True to enable extended reasoning (uses claude-opus-4-7).

    Args:
        request: AgentRequest with the question, optional thinking flag, and max_tokens.

    Returns:
        AgentRunResponse containing the answer, tool trace, optional thinking blocks,
        LLM-as-judge evaluation, and step count.
    """
    result = await _orchestrator.run(
        request.question, request.thinking, request.max_tokens
    )
    return AgentRunResponse(**result)


@router.post("/agent/stream")
async def agent_stream(request: AgentRequest):
    """Stream the agent's work as Server-Sent Events.

    Each event is a JSON object on a ``data:`` line. Event types:
    - ``thinking`` — extended thinking chunk (only when thinking=True)
    - ``tool_call`` — a tool is about to be invoked
    - ``tool_result`` — the result returned by the tool
    - ``text_delta`` — a chunk of the final response text
    - ``evaluation`` — LLM-as-judge score after completion
    - ``error`` — safety check failure
    - ``done`` — stream terminator

    Args:
        request: AgentRequest with the question, optional thinking flag, and max_tokens.

    Returns:
        StreamingResponse with media_type text/event-stream.

    Note:
        When deployed on AWS Lambda via Mangum, streaming is buffered — the full
        response is returned at once rather than token-by-token. True streaming
        requires a persistent runtime (uvicorn, ECS, or Lambda response streaming).
    """
    async def event_generator():
        async for event in _orchestrator.stream(
            request.question, request.thinking, request.max_tokens
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
