import hmac
import os
import jwt
import httpx
from jose import jwt as jose_jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from secret_resolver import get_secret, resolve_env_secret

_IS_DEV = os.environ.get("ENV") == "dev"

# Dev: HS256 — plain JWT_SECRET from env.
# Prod: fetched from Secrets Manager via JWT_SECRET_ARN.
_JWT_SECRET = resolve_env_secret("JWT_SECRET_ARN", "JWT_SECRET") if not _IS_DEV else os.environ.get("JWT_SECRET")

# API key — required on all non-exempt paths when configured.
# In local dev without API_KEY set, enforcement is disabled.
_api_key_arn = os.environ.get("API_KEY_SECRET_ARN")
if _api_key_arn:
    _API_KEY: str | None = get_secret(_api_key_arn)
elif "API_KEY" in os.environ:
    _API_KEY = os.environ["API_KEY"]
else:
    _API_KEY = None

_API_KEY_EXEMPT = {"/health", "/dev/token"}

# Prod: Cognito RS256 via JWKS
_REGION = os.environ.get("AWS_REGION")
_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID")
_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID")
_JWKS_URL = (
    f"https://cognito-idp.{_REGION}.amazonaws.com/{_USER_POOL_ID}/.well-known/jwks.json"
    if _REGION and _USER_POOL_ID
    else None
)

_jwks_cache: dict | None = None


async def _get_jwks() -> dict:
    global _jwks_cache
    if _jwks_cache is None:
        async with httpx.AsyncClient() as client:
            resp = await client.get(_JWKS_URL)
            resp.raise_for_status()
            _jwks_cache = resp.json()
    return _jwks_cache


async def _decode_dev(token: str) -> str:
    payload = jwt.decode(token, _JWT_SECRET, algorithms=["HS256"])
    return payload["sub"]


async def _decode_cognito(token: str) -> str:
    jwks = await _get_jwks()
    headers = jose_jwt.get_unverified_headers(token)
    key = next((k for k in jwks["keys"] if k["kid"] == headers["kid"]), None)
    if not key:
        raise JWTError("Public key not found")
    claims = jose_jwt.decode(token, key, algorithms=["RS256"], audience=_CLIENT_ID)
    return claims["sub"]


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # API key check — runs first, before JWT, for fast rejection
        if _API_KEY is not None and request.url.path not in _API_KEY_EXEMPT:
            provided = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(provided, _API_KEY):
                return JSONResponse({"detail": "Missing or invalid API key"}, status_code=401)

        if request.url.path in ("/health", "/dev/token", "/v1/models", "/auth/login", "/auth/change-password"):
            return await call_next(request)

        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token:
            return JSONResponse({"detail": "Missing token"}, status_code=401)

        try:
            if _IS_DEV:
                request.state.user = await _decode_dev(token)
            else:
                request.state.user = await _decode_cognito(token)
        except Exception:
            return JSONResponse({"detail": "Invalid token"}, status_code=401)

        return await call_next(request)
