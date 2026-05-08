"""Generate a JWT for local testing. Never use in production."""
import os
import sys

import jwt
from dotenv import load_dotenv

load_dotenv()

secret = os.environ.get("JWT_SECRET")
if not secret:
    sys.exit("JWT_SECRET not set in .env")

sub = sys.argv[1] if len(sys.argv) > 1 else "dev-user"
token = jwt.encode({"sub": sub}, secret, algorithm="HS256")
print(token)
