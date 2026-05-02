import json
import os
import pytest
from botocore.exceptions import ClientError
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport
import jwt


def _make_token() -> str:
    return jwt.encode({"sub": "user-1"}, os.environ["JWT_SECRET"], algorithm="HS256")


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {_make_token()}"}


def _run_result(
    response: str = "A safe answer.",
    tool_trace: list | None = None,
    thinking: list | None = None,
    score: float = 0.9,
    steps: int = 1,
) -> dict:
    """Build a minimal AgentRunResult dict for mocking orchestrator.run."""
    return {
        "response": response,
        "tool_trace": tool_trace or [],
        "thinking": thinking,
        "evaluation": {"score": score, "reasoning": "Test evaluation."},
        "steps_taken": steps,
    }


# ── Unit tests: agent_pipeline.py ────────────────────────────────────────────

def test_pipeline_check_and_redact_no_pii():
    from agent_pipeline import AgentPipeline
    pipeline = AgentPipeline()
    redacted, token_map = pipeline.check_and_redact("hello world")
    assert redacted == "hello world"
    assert token_map == {}


def test_pipeline_validate_and_restore_clean():
    from agent_pipeline import AgentPipeline
    pipeline = AgentPipeline()
    restored = pipeline.validate_and_restore("A normal helpful response.", {})
    assert restored == "A normal helpful response."


def test_pipeline_validate_and_restore_fails_validation():
    from agent_pipeline import AgentPipeline
    from fastapi import HTTPException
    pipeline = AgentPipeline()
    with pytest.raises(HTTPException) as exc:
        pipeline.validate_and_restore("I cannot assist with that request.", {})
    assert exc.value.status_code == 502


def test_pipeline_validate_and_restore_fails_leakage():
    from agent_pipeline import AgentPipeline
    from fastapi import HTTPException
    pipeline = AgentPipeline()
    with pytest.raises(HTTPException) as exc:
        pipeline.validate_and_restore("Contact leaker@evil.com for details.", {})
    assert exc.value.status_code == 502


def test_pipeline_restore_replaces_tokens():
    from agent_pipeline import AgentPipeline
    pipeline = AgentPipeline()
    token_map = {"[EMAIL_ABCD1234]": "user@example.com"}
    restored = pipeline.validate_and_restore("Reach [EMAIL_ABCD1234] directly.", token_map)
    assert "user@example.com" in restored


# ── Unit tests: calculator tool ───────────────────────────────────────────────

def test_calculator_basic():
    from tools import calculator
    assert calculator("2 + 2 * 3") == "8"


def test_calculator_float():
    from tools import calculator
    assert float(calculator("1234 * 0.029")) == pytest.approx(35.786)


def test_calculator_rejects_import():
    from tools import calculator
    assert calculator("__import__('os').system('ls')").startswith("ERROR:")


def test_calculator_rejects_call_node():
    from tools import calculator
    assert calculator("abs(-1)").startswith("ERROR:")


def test_calculator_division_by_zero():
    from tools import calculator
    assert calculator("1 / 0").startswith("ERROR:")


# ── Unit tests: analyze_pii_risk tool ────────────────────────────────────────

def test_analyze_pii_risk_no_pii():
    from tools import analyze_pii_risk
    report = json.loads(analyze_pii_risk("hello world"))
    assert report["risk_level"] == "NONE"
    assert report["entity_count"] == 0


def test_analyze_pii_risk_pan_is_high():
    from tools import analyze_pii_risk
    report = json.loads(analyze_pii_risk("card 4111111111111111"))
    assert report["risk_level"] == "HIGH"
    assert report["entity_count"] >= 1


def test_analyze_pii_risk_email_is_medium():
    from tools import analyze_pii_risk
    report = json.loads(analyze_pii_risk("contact user@example.com"))
    assert report["risk_level"] == "MEDIUM"


# ── Unit tests: query_audit_log tool ─────────────────────────────────────────

def test_query_audit_log_client_error():
    from tools import query_audit_log
    error_response = {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}}
    with patch("tools.boto3") as mock_boto3:
        mock_table = MagicMock()
        mock_table.scan.side_effect = ClientError(error_response, "Scan")
        mock_boto3.resource.return_value.Table.return_value = mock_table
        result = query_audit_log(limit=5)
    assert result.startswith("ERROR:")
    assert "Table not found" in result


def test_query_audit_log_success():
    from tools import query_audit_log
    with patch("tools.boto3") as mock_boto3:
        mock_table = MagicMock()
        mock_table.scan.return_value = {"Items": [{"request_id": "abc", "user_id": "u1"}]}
        mock_boto3.resource.return_value.Table.return_value = mock_table
        result = query_audit_log(limit=1)
    records = json.loads(result)
    assert len(records) == 1
    assert records[0]["request_id"] == "abc"


# ── Unit tests: execute_tool dispatcher ──────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_tool_calculator_dispatch():
    from tools import execute_tool
    result = await execute_tool("calculator", {"expression": "3 * 7"})
    assert result == "21"


@pytest.mark.asyncio
async def test_execute_tool_analyze_pii_dispatch():
    from tools import execute_tool
    result = await execute_tool("analyze_pii_risk", {"text": "hello"})
    data = json.loads(result)
    assert data["risk_level"] == "NONE"


@pytest.mark.asyncio
async def test_execute_tool_unknown_returns_error():
    from tools import execute_tool
    result = await execute_tool("nonexistent_tool", {})
    assert result.startswith("ERROR:")


@pytest.mark.asyncio
async def test_execute_tool_call_subagent_no_runner():
    from tools import execute_tool
    result = await execute_tool("call_subagent", {"agent": "compliance", "question": "Q"})
    assert result.startswith("ERROR:")


# ── Unit tests: subagents ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_subagent_runner_unknown_agent():
    from subagents import SubagentRunner
    from agent_pipeline import AgentPipeline
    runner = SubagentRunner(MagicMock())
    result = await runner.run("nonexistent", "question", AgentPipeline())
    assert result.startswith("ERROR:")


@pytest.mark.asyncio
async def test_subagent_runner_compliance_happy_path():
    from subagents import SubagentRunner
    from agent_pipeline import AgentPipeline
    mock_client = MagicMock()
    text_block = MagicMock()
    text_block.text = "PCI DSS Req 3.3 prohibits storing SAD after authorisation."
    resp = MagicMock()
    resp.content = [text_block]
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(return_value=resp)

    runner = SubagentRunner(mock_client)
    result = await runner.run("compliance", "What does Req 3.3 say?", AgentPipeline())
    assert "3.3" in result or "SAD" in result


@pytest.mark.asyncio
async def test_subagent_runner_api_error():
    from subagents import SubagentRunner
    from agent_pipeline import AgentPipeline
    import anthropic as anthropic_mod
    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=anthropic_mod.APIConnectionError(request=MagicMock())
    )
    runner = SubagentRunner(mock_client)
    result = await runner.run("analyst", "Analyse logs.", AgentPipeline())
    assert result.startswith("ERROR:")


# ── Unit tests: orchestrator.run() ───────────────────────────────────────────

def _mock_text_response(text: str, stop_reason: str = "end_turn"):
    block = MagicMock()
    block.type = "text"
    block.text = text
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = [block]
    return resp


def _mock_tool_response(tool_name: str, tool_id: str, tool_input: dict):
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = tool_name
    block.input = tool_input
    resp = MagicMock()
    resp.stop_reason = "tool_use"
    resp.content = [block]
    return resp


def _mock_judge_response(score: float = 0.9):
    block = MagicMock()
    block.type = "text"
    block.text = f'{{"score": {score}, "reasoning": "Test."}}'
    resp = MagicMock()
    resp.content = [block]
    return resp


@pytest.mark.asyncio
async def test_orchestrator_run_no_tools():
    from orchestrator import AgentOrchestrator
    from agent_pipeline import AgentPipeline
    from evaluator import LLMJudge
    from subagents import SubagentRunner

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[
            _mock_text_response("PCI DSS is a security standard."),
            _mock_judge_response(0.9),
        ]
    )
    pipeline = AgentPipeline()
    orchestrator = AgentOrchestrator(
        mock_client, pipeline, SubagentRunner(mock_client), LLMJudge(mock_client)
    )
    result = await orchestrator.run("What is PCI DSS?")

    assert result["response"] == "PCI DSS is a security standard."
    assert result["steps_taken"] == 1
    assert result["tool_trace"] == []
    assert result["thinking"] is None
    assert result["evaluation"]["score"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_orchestrator_run_with_tool_call():
    from orchestrator import AgentOrchestrator
    from agent_pipeline import AgentPipeline
    from evaluator import LLMJudge
    from subagents import SubagentRunner

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[
            _mock_tool_response("calculator", "t1", {"expression": "2 * 3"}),
            _mock_text_response("The result is 6."),
            _mock_judge_response(1.0),
        ]
    )
    pipeline = AgentPipeline()
    orchestrator = AgentOrchestrator(
        mock_client, pipeline, SubagentRunner(mock_client), LLMJudge(mock_client)
    )
    result = await orchestrator.run("What is 2 times 3?")

    assert result["response"] == "The result is 6."
    assert result["steps_taken"] == 2
    assert len(result["tool_trace"]) == 1
    assert result["tool_trace"][0]["tool_name"] == "calculator"
    assert result["tool_trace"][0]["tool_result"] == "6"
    assert not result["tool_trace"][0]["error"]


@pytest.mark.asyncio
async def test_orchestrator_uses_opus_when_thinking():
    from orchestrator import AgentOrchestrator, _MODEL_THINKING
    from agent_pipeline import AgentPipeline
    from evaluator import LLMJudge
    from subagents import SubagentRunner

    thinking_block = MagicMock()
    thinking_block.type = "thinking"
    thinking_block.thinking = "Deep thought..."
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Here is my answer."
    resp = MagicMock()
    resp.stop_reason = "end_turn"
    resp.content = [thinking_block, text_block]

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[resp, _mock_judge_response(0.8)]
    )
    pipeline = AgentPipeline()
    orchestrator = AgentOrchestrator(
        mock_client, pipeline, SubagentRunner(mock_client), LLMJudge(mock_client)
    )
    result = await orchestrator.run("Question", use_thinking=True)

    assert result["thinking"] == ["Deep thought..."]
    called_model = mock_client.messages.create.call_args_list[0].kwargs["model"]
    assert called_model == _MODEL_THINKING


# ── Unit tests: evaluator ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_judge_returns_score():
    from evaluator import LLMJudge

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = '{"score": 0.9, "reasoning": "Correct and complete."}'
    resp = MagicMock()
    resp.content = [text_block]
    mock_client.messages.create = AsyncMock(return_value=resp)

    judge = LLMJudge(mock_client)
    result = await judge.score("What is PCI DSS?", "PCI DSS is a security standard.")
    assert result["score"] == pytest.approx(0.9)
    assert "reasoning" in result


@pytest.mark.asyncio
async def test_judge_falls_back_on_parse_error():
    from evaluator import LLMJudge

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "not valid json"
    resp = MagicMock()
    resp.content = [text_block]
    mock_client.messages.create = AsyncMock(return_value=resp)

    judge = LLMJudge(mock_client)
    result = await judge.score("Q", "A")
    assert result["score"] == 0.5
    assert result["reasoning"] == "judge parse error"


@pytest.mark.asyncio
async def test_judge_no_text_block_in_response():
    """Judge returns a safe fallback when the response contains no text block."""
    from evaluator import LLMJudge

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    thinking_block = MagicMock()
    thinking_block.type = "thinking"  # no .text attribute
    resp = MagicMock()
    resp.content = [thinking_block]
    mock_client.messages.create = AsyncMock(return_value=resp)

    judge = LLMJudge(mock_client)
    result = await judge.score("Q", "A")
    assert result["score"] == 0.5
    assert result["reasoning"] == "judge returned no text block"


@pytest.mark.asyncio
async def test_judge_empty_content_list():
    """Judge returns a safe fallback when content is an empty list."""
    from evaluator import LLMJudge

    mock_client = MagicMock()
    mock_client.messages = MagicMock()
    resp = MagicMock()
    resp.content = []
    mock_client.messages.create = AsyncMock(return_value=resp)

    judge = LLMJudge(mock_client)
    result = await judge.score("Q", "A")
    assert result["score"] == 0.5
    assert result["reasoning"] == "judge returned no text block"


# ── Integration tests: POST /v1/agent/run ────────────────────────────────────
# Patch _orchestrator directly — the singleton holds the real client reference
# so patching the underlying client after construction has no effect.

@pytest.mark.asyncio
async def test_agent_run_no_tools(auth_headers):
    from main import app

    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.run = AsyncMock(return_value=_run_result("PCI DSS is a security standard."))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/run",
                json={"question": "What is PCI DSS?"},
                headers=auth_headers,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "PCI DSS" in body["response"]
    assert body["tool_trace"] == []
    assert body["thinking"] is None
    assert body["steps_taken"] == 1
    assert 0.0 <= body["evaluation"]["score"] <= 1.0


@pytest.mark.asyncio
async def test_agent_run_with_calculator_tool(auth_headers):
    from main import app

    trace = [
        {
            "tool_name": "calculator",
            "tool_input": {"expression": "100 * 0.029"},
            "tool_result": "2.9",
            "error": False,
        }
    ]
    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.run = AsyncMock(
            return_value=_run_result("The 2.9% fee on $100 is $2.90.", tool_trace=trace, steps=2)
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/run",
                json={"question": "What is 2.9% of 100?"},
                headers=auth_headers,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["tool_trace"]) == 1
    assert body["tool_trace"][0]["tool_name"] == "calculator"
    assert not body["tool_trace"][0]["error"]


@pytest.mark.asyncio
async def test_agent_run_with_thinking_flag(auth_headers):
    """thinking=True should be passed through to orchestrator.run and reflected in the response."""
    from main import app

    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.run = AsyncMock(
            return_value=_run_result(
                "Requirement 3.3 covers SAD storage.",
                thinking=["Let me reason through this carefully..."],
            )
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/run",
                json={"question": "Explain PCI DSS Req 3.3", "thinking": True},
                headers=auth_headers,
            )

    assert resp.status_code == 200
    body = resp.json()
    assert body["thinking"] is not None
    assert len(body["thinking"]) == 1

    # Verify thinking=True was forwarded to the orchestrator.
    mock_orch.run.assert_called_once_with("Explain PCI DSS Req 3.3", True, 2048)


@pytest.mark.asyncio
async def test_agent_run_pan_blocked(auth_headers):
    """A question containing a PAN must be blocked before reaching the orchestrator."""
    from main import app

    # No mock — the real AgentPipeline raises 400 before any API call.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(
            "/v1/agent/run",
            json={"question": "My card is 4111111111111111, is it valid?"},
            headers=auth_headers,
        )
    assert resp.status_code == 400
    assert "PAN" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_agent_run_max_steps_graceful(auth_headers):
    from main import app
    from orchestrator import _MAX_STEPS

    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.run = AsyncMock(
            return_value=_run_result("Summary of findings.", steps=_MAX_STEPS, score=0.5)
        )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/run",
                json={"question": "Keep calculating forever."},
                headers=auth_headers,
            )

    assert resp.status_code == 200
    assert resp.json()["steps_taken"] == _MAX_STEPS


@pytest.mark.asyncio
async def test_agent_run_missing_token_returns_401():
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/v1/agent/run", json={"question": "Hello"})
    assert resp.status_code == 401


# ── Integration tests: POST /v1/agent/stream ─────────────────────────────────

@pytest.mark.asyncio
async def test_agent_stream_emits_sse_events(auth_headers):
    from main import app

    async def mock_stream(question, use_thinking, max_tokens):
        yield {"type": "text_delta", "text": "PCI DSS "}
        yield {"type": "text_delta", "text": "is a standard."}
        yield {"type": "evaluation", "score": 0.9, "reasoning": "Good."}
        yield {"type": "done"}

    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.stream = mock_stream
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/stream",
                json={"question": "What is PCI DSS?"},
                headers=auth_headers,
            )

    assert resp.status_code == 200
    lines = [l for l in resp.text.split("\n") if l.startswith("data:")]
    events = [json.loads(l[len("data: "):]) for l in lines]
    types = [e["type"] for e in events]
    assert "text_delta" in types
    assert "evaluation" in types
    assert types[-1] == "done"


@pytest.mark.asyncio
async def test_agent_stream_includes_tool_events(auth_headers):
    from main import app

    async def mock_stream(question, use_thinking, max_tokens):
        yield {"type": "tool_call", "name": "calculator", "input": {"expression": "2+2"}}
        yield {"type": "tool_result", "name": "calculator", "result": "4"}
        yield {"type": "text_delta", "text": "The answer is 4."}
        yield {"type": "evaluation", "score": 1.0, "reasoning": "Correct."}
        yield {"type": "done"}

    with patch("routes.agent._orchestrator") as mock_orch:
        mock_orch.stream = mock_stream
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/v1/agent/stream",
                json={"question": "What is 2+2?"},
                headers=auth_headers,
            )

    lines = [l for l in resp.text.split("\n") if l.startswith("data:")]
    events = [json.loads(l[len("data: "):]) for l in lines]
    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
