"""
Locust load test for the PCI LLM Gateway.

Run with:
    locust -f scripts/load_test.py --host http://localhost:8000
"""

import os

import jwt
from locust import HttpUser, between, task

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret")
_TOKEN = jwt.encode({"sub": "load-test-user"}, JWT_SECRET, algorithm="HS256")

SAFE_PROMPTS = [
    "What is machine learning?",
    "Summarize the water cycle in two sentences.",
    "List three benefits of cloud computing.",
    "Explain REST APIs to a beginner.",
    "What is the difference between SQL and NoSQL?",
]


class GatewayUser(HttpUser):
    wait_time = between(1, 3)
    headers = {"Authorization": f"Bearer {_TOKEN}"}

    @task(10)
    def inference_safe(self):
        import random

        self.client.post(
            "/v1/inference",
            json={"prompt": random.choice(SAFE_PROMPTS), "max_tokens": 256},
            headers=self.headers,
        )

    @task(1)
    def health_check(self):
        self.client.get("/health")
