from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from middleware.auth import AuthMiddleware
from middleware.logging import LoggingMiddleware
from routes.agent import router as agent_router
from routes.auth import router as auth_router
from routes.inference import router as inference_router
from routes.token import router as token_router

load_dotenv()
app = FastAPI(title="PCI LLM Gateway", version="1.0.0")

app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=["POST"])
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)

app.include_router(auth_router, prefix="/auth")
app.include_router(inference_router, prefix="/v1")
app.include_router(agent_router, prefix="/v1")
app.include_router(token_router, prefix="/dev")


@app.get("/health")
async def health():
    return {"status": "ok"}


# Lambda entrypoint — used by the production Dockerfile (CMD ["main.handler"])
handler = Mangum(app)
