import os
import boto3
from functools import lru_cache

_client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION_NAME", "us-east-1"))


@lru_cache(maxsize=None)
def get_secret(arn: str) -> str:
    return _client.get_secret_value(SecretId=arn)["SecretString"]


def resolve_env_secret(env_arn_key: str, env_plain_key: str) -> str:
    """Return plain env var value in dev, fetch from Secrets Manager in prod."""
    arn = os.environ.get(env_arn_key)
    if arn:
        return get_secret(arn)
    return os.environ[env_plain_key]
