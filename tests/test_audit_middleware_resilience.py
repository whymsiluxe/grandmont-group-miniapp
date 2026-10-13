import builtins
import os
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main as backend


def test_audit_write_failure_does_not_change_committed_response():
    app = FastAPI()
    app.middleware("http")(backend.audit_log_middleware)

    @app.post("/mutate")
    def mutate():
        return {"status": "committed"}

    real_open = builtins.open

    def fail_audit_only(path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if os.fspath(path) == backend.AUDIT_FILE and "a" in mode:
            raise OSError("simulated audit storage failure")
        return real_open(path, *args, **kwargs)

    with patch("builtins.open", side_effect=fail_audit_only):
        response = TestClient(app).post("/mutate", json={"x": 1})

    assert response.status_code == 200
    assert response.json() == {"status": "committed"}
