"""Agent route — multi-step tool-use loop with tenant isolation."""

import json

import anthropic
import rate_limiter
from agent_pipeline import AgentPipeline
from evaluator import LLMJudge
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from orchestrator import AgentOrchestrator
from schemas.agent_schemas import AgentRequest, AgentRunResponse
from secret_resolver import resolve_env_secret
from subagents import SubagentRunner
from tenant import TenantConfig, require_tenant

router = APIRouter()

_anthropic_client = anthropic.AsyncAnthropic(
    api_key=resolve_env_secret("ANTHROPIC_API_KEY_SECRET_ARN", "ANTHROPIC_API_KEY")
)

_pipeline = AgentPipeline()
_subagent_runner = SubagentRunner(_anthropic_client)
_judge = LLMJudge(_anthropic_client)
_orchestrator = AgentOrchestrator(_anthropic_client, _pipeline, _subagent_runner, _judge)


def _check_agent_model(tenant: TenantConfig, thinking: bool) -> None:
    """Raise 422 if the models required for this agent run are not in the tenant allow-list.

    Args:
        tenant:   Resolved TenantConfig for the caller.
        thinking: When True the orchestrator will use the Opus model.
    """
    if tenant.allowed_models is None:
        return
    from orchestrator import _MODEL_DEFAULT, _MODEL_THINKING  # noqa: PLC0415

    model = _MODEL_THINKING if thinking else _MODEL_DEFAULT
    if model not in tenant.allowed_models:
        raise HTTPException(
            status_code=422,
            detail=f"Model '{model}' is not permitted for tenant '{tenant.tenant_id}'",
        )


@router.post("/agent/run", response_model=AgentRunResponse, dependencies=[Depends(rate_limiter.limit("agent"))])
async def agent_run(body: AgentRequest, tenant: TenantConfig = Depends(require_tenant)):
    """Run the agent to completion and return the full result.

    The agent may call tools multiple times before producing its final answer.
    Set thinking=True to enable extended reasoning (uses claude-opus-4-7).

    Args:
        body:   AgentRequest with the question, optional thinking flag, and max_tokens.
        tenant: Resolved TenantConfig from the JWT ``tenant_id`` claim.

    Returns:
        AgentRunResponse containing the answer, tool trace, optional thinking blocks,
        LLM-as-judge evaluation, and step count.
    """
    _check_agent_model(tenant, body.thinking)
    result = await _orchestrator.run(body.question, body.thinking, body.max_tokens)
    return AgentRunResponse(**result)


@router.post("/agent/stream", dependencies=[Depends(rate_limiter.limit("agent"))])
async def agent_stream(body: AgentRequest, tenant: TenantConfig = Depends(require_tenant)):
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
        body:   AgentRequest with the question, optional thinking flag, and max_tokens.
        tenant: Resolved TenantConfig from the JWT ``tenant_id`` claim.

    Returns:
        StreamingResponse with media_type text/event-stream.

    Note:
        When deployed on AWS Lambda via Mangum, streaming is buffered — the full
        response is returned at once rather than token-by-token. True streaming
        requires a persistent runtime (uvicorn, ECS, or Lambda response streaming).
    """
    _check_agent_model(tenant, body.thinking)

    async def event_generator():
        async for event in _orchestrator.stream(body.question, body.thinking, body.max_tokens):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")
