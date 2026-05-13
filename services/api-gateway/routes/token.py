import os

import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class TokenRequest(BaseModel):
    sub: str = "dev-user"


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/token", response_model=TokenResponse)
def generate_token(body: TokenRequest):
    if os.environ.get("ENV") != "dev":
        raise HTTPException(status_code=404)
    token = jwt.encode({"sub": body.sub}, os.environ["JWT_SECRET"], algorithm="HS256")
    return TokenResponse(access_token=token)
