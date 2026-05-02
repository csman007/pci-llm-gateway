from fastapi import HTTPException

# Entity types that must block the request outright rather than just redact.
BLOCK_ON_DETECTION = {"PAN", "CVV", "SSN", "EXPIRY"}
# Maximum PII entities allowed per request before blocking.
MAX_PII_THRESHOLD = 5


class PolicyEngine:
    def enforce(self, findings) -> None:
        if not findings:
            return

        blocked_types = {f.entity_type for f in findings} & BLOCK_ON_DETECTION
        if blocked_types:
            raise HTTPException(
                status_code=400,
                detail=f"Request blocked: contains restricted PII type(s): {', '.join(blocked_types)}",
            )

        if len(findings) > MAX_PII_THRESHOLD:
            raise HTTPException(
                status_code=400,
                detail=f"Request blocked: exceeds PII entity limit ({len(findings)} > {MAX_PII_THRESHOLD})",
            )
