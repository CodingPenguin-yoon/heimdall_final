from fastapi import FastAPI
from fastapi.testclient import TestClient

from heimdall.common.errors import install_error_handlers
from heimdall.projects.schemas import EnvironmentVariableInput


def test_validation_problem_does_not_echo_secret_input() -> None:
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/environment")
    def environment(payload: EnvironmentVariableInput) -> dict:
        return payload.model_dump()

    response = TestClient(app).post(
        "/environment",
        json={"name": "invalid-name", "kind": "SECRET", "value": "secret-canary"},
    )

    assert response.status_code == 422
    assert response.headers["cache-control"] == "no-store"
    assert "secret-canary" not in response.text
    assert "input" not in response.json()["violations"][0]
