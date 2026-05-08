from detector import PIIDetector
from fastapi import APIRouter, HTTPException
from leakage_detector import LeakageDetector
from model_registry import MODEL_NAMES, get_client
from policy_engine import PolicyEngine
from redactor import Redactor
from schemas.request import InferenceRequest, InferenceResponse
from validator import OutputValidator

router = APIRouter()


@router.get("/models")
def list_models():
    return {"models": MODEL_NAMES}


detector = PIIDetector()
redactor = Redactor()
policy = PolicyEngine()
validator = OutputValidator()
leakage = LeakageDetector()


@router.post("/inference", response_model=InferenceResponse)
async def inference(request: InferenceRequest):
    findings = detector.scan(request.prompt)
    policy.enforce(findings)

    redacted_prompt, token_map = redactor.redact(request.prompt, findings)
    response_text = await get_client(request.model).complete(redacted_prompt, request.model, request.max_tokens)

    if not validator.is_valid(response_text):
        raise HTTPException(status_code=502, detail="LLM response failed validation")

    if leakage.detected(response_text, token_map):
        raise HTTPException(status_code=502, detail="PII leakage detected in response")

    restored = redactor.restore(response_text, token_map)
    return InferenceResponse(response=restored, pii_findings=len(findings))
