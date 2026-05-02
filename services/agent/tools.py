import ast
import json
import os
import boto3
from botocore.exceptions import ClientError
from detector import PIIDetector

_pii_detector = PIIDetector()

TOOL_DEFINITIONS = [
    {
        "name": "query_audit_log",
        "description": (
            "Query the DynamoDB audit log for recent inference events. "
            "Use to analyse usage patterns, find anomalies, or review activity. "
            "Returns up to `limit` records as a JSON array."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of records to return (1–100).",
                    "default": 20,
                },
                "user_id": {
                    "type": "string",
                    "description": "Filter records to a specific user ID (optional).",
                },
            },
            "required": [],
        },
    },
    {
        "name": "analyze_pii_risk",
        "description": (
            "Scan a text snippet for PII entities and return a structured risk report. "
            "Reports entity types found, count, and overall risk level. "
            "Does NOT block or redact — use for assessment only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to analyse for PII."},
            },
            "required": ["text"],
        },
    },
    {
        "name": "calculator",
        "description": (
            "Evaluate a safe arithmetic expression and return the numeric result. "
            "Supports +, -, *, /, **, parentheses, and integer/float literals."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "Arithmetic expression, e.g. '1234 * 0.029 + 15'.",
                },
            },
            "required": ["expression"],
        },
    },
    {
        "name": "call_subagent",
        "description": (
            "Delegate a specialised question to a domain expert subagent. "
            "Use 'compliance' for PCI DSS regulatory/policy questions. "
            "Use 'analyst' for interpreting patterns in audit log data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "enum": ["compliance", "analyst"],
                    "description": "Which specialist to invoke.",
                },
                "question": {
                    "type": "string",
                    "description": "The specific question or task for the subagent.",
                },
            },
            "required": ["agent", "question"],
        },
    },
]


def query_audit_log(limit: int = 20, user_id: str | None = None) -> str:
    """Scan the DynamoDB audit table and return matching records as a JSON string.

    Args:
        limit: Maximum number of records to return (clamped to 1–100).
        user_id: Optional filter — only return records for this user.

    Returns:
        JSON-serialised list of audit records, or an ERROR: prefixed string on failure.
    """
    limit = max(1, min(limit, 100))
    table_name = os.environ.get("AUDIT_LOG_TABLE", "pci-llm-gateway-audit")
    try:
        table = boto3.resource("dynamodb").Table(table_name)
        kwargs: dict = {"Limit": limit}
        if user_id:
            from boto3.dynamodb.conditions import Attr
            kwargs["FilterExpression"] = Attr("user_id").eq(user_id)
        result = table.scan(**kwargs)
        return json.dumps(result.get("Items", []), default=str)
    except ClientError as exc:
        return f"ERROR: DynamoDB query failed — {exc.response['Error']['Message']}"


def analyze_pii_risk(text: str) -> str:
    """Scan *text* for PII and return a JSON risk report.

    Args:
        text: The text to analyse.

    Returns:
        JSON string with keys: entity_count, entities (list of type/confidence),
        and risk_level (HIGH, MEDIUM, LOW, or NONE).
    """
    BLOCK_TYPES = {"PAN", "CVV", "SSN", "EXPIRY"}
    findings = _pii_detector.scan(text)
    types_found = {f.entity_type for f in findings}

    if types_found & BLOCK_TYPES:
        risk_level = "HIGH"
    elif types_found:
        risk_level = "MEDIUM"
    else:
        risk_level = "NONE"

    report = {
        "entity_count": len(findings),
        "entities": [
            {"type": f.entity_type, "confidence": f.confidence} for f in findings
        ],
        "risk_level": risk_level,
    }
    return json.dumps(report)


# Allowlist of AST node types permitted in calculator expressions.
_SAFE_NODES = (
    ast.Expression,
    ast.BinOp, ast.UnaryOp, ast.Constant,
    ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
    ast.USub, ast.UAdd,
)


def calculator(expression: str) -> str:
    """Safely evaluate an arithmetic expression using AST inspection.

    Args:
        expression: A string containing only arithmetic operators and numeric literals.

    Returns:
        The numeric result as a string, or an ERROR: prefixed string if the
        expression is unsafe or invalid.
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
    except SyntaxError:
        return f"ERROR: invalid expression syntax"

    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            return f"ERROR: unsafe expression — only arithmetic operations are allowed"

    try:
        result = eval(compile(tree, "<calc>", "eval"))  # noqa: S307 — AST-validated above
        return str(result)
    except ZeroDivisionError:
        return "ERROR: division by zero"
    except Exception as exc:
        return f"ERROR: evaluation failed — {exc}"


async def execute_tool(
    name: str,
    inputs: dict,
    subagent_runner=None,
    pipeline=None,
) -> str:
    """Dispatch a tool call by name and return the result as a string.

    Args:
        name: Tool name — one of query_audit_log, analyze_pii_risk, calculator, call_subagent.
        inputs: Tool input parameters as a dict matching the tool's input_schema.
        subagent_runner: SubagentRunner instance, required for call_subagent.
        pipeline: AgentPipeline instance, required for call_subagent.

    Returns:
        Tool result as a plain string. Errors are returned as ERROR: prefixed strings
        rather than raised exceptions so the orchestrator can relay them to Claude.
    """
    if name == "query_audit_log":
        return query_audit_log(
            limit=inputs.get("limit", 20),
            user_id=inputs.get("user_id"),
        )
    if name == "analyze_pii_risk":
        return analyze_pii_risk(inputs["text"])
    if name == "calculator":
        return calculator(inputs["expression"])
    if name == "call_subagent":
        if subagent_runner is None or pipeline is None:
            return "ERROR: subagent runner not available"
        return await subagent_runner.run(inputs["agent"], inputs["question"], pipeline)
    return f"ERROR: unknown tool '{name}'"
