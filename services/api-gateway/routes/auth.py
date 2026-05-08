import os

import boto3
from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

def _client_id() -> str | None:
    return os.environ.get("COGNITO_CLIENT_ID")


def _region() -> str:
    return os.environ.get("AWS_REGION_NAME", os.environ.get("AWS_REGION", "us-east-1"))


def _cognito():
    return boto3.client("cognito-idp", region_name=_region())


def _require_cognito() -> str:
    cid = _client_id()
    if not cid:
        raise HTTPException(status_code=404)
    return cid


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ChallengeResponse(BaseModel):
    challenge: str
    session: str


class ChangePasswordRequest(BaseModel):
    username: str
    new_password: str
    session: str


@router.post("/login")
def login(body: LoginRequest):
    client_id = _require_cognito()
    try:
        result = _cognito().initiate_auth(
            AuthFlow="USER_PASSWORD_AUTH",
            ClientId=client_id,
            AuthParameters={"USERNAME": body.username, "PASSWORD": body.password},
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NotAuthorizedException", "UserNotFoundException"):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        if code == "PasswordResetRequiredException":
            raise HTTPException(status_code=403, detail="Password reset required")
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    if result.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
        return ChallengeResponse(
            challenge="NEW_PASSWORD_REQUIRED",
            session=result["Session"],
        )

    return LoginResponse(access_token=result["AuthenticationResult"]["AccessToken"])


@router.post("/change-password", response_model=LoginResponse)
def change_password(body: ChangePasswordRequest):
    client_id = _require_cognito()
    try:
        result = _cognito().respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName="NEW_PASSWORD_REQUIRED",
            Session=body.session,
            ChallengeResponses={
                "USERNAME": body.username,
                "NEW_PASSWORD": body.new_password,
            },
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "InvalidPasswordException":
            raise HTTPException(status_code=422, detail=e.response["Error"]["Message"])
        if code in ("NotAuthorizedException", "ExpiredCodeException"):
            raise HTTPException(status_code=401, detail="Session expired — please log in again")
        raise HTTPException(status_code=502, detail=f"Cognito error: {code}")

    return LoginResponse(access_token=result["AuthenticationResult"]["AccessToken"])
