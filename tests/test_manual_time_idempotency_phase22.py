"""Phase 22 structural regression for manual-time post-commit idempotency."""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import routes.execution as execution_routes  # noqa: E402


def test_manual_time_secondary_idempotency_save_is_explicitly_post_commit():
    source = inspect.getsource(execution_routes.create_execution_router)
    assert source.count('deps.idempotency_save(') == 1
    assert 'Manual-time idempotency response persistence failed after durable commit' in source
    assert "deps.idempotency_release(idempotency_key, idempotency_scope)\n            raise\n\n        # The manual-time business fact is already durable" in source
