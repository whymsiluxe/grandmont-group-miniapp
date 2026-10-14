import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
from routes import service_bridge  # noqa: E402
from routes.service_bridge import ServiceBridgeRouteDeps, create_service_bridge_router  # noqa: E402


def _handlers():
    deps = ServiceBridgeRouteDeps(
        business_today=lambda: None,
        load_roles=lambda: {"10": "worker"},
        load_checkin_meta=lambda: [],
        is_active_photo_checkin_session=lambda s: False,
        hours_from_session=lambda s: 0.0,
        session_hours_live=lambda s: 0.0,
    )
    _, handlers = create_service_bridge_router(deps)
    return handlers


class TestDailyPlanBridgeAcceptance(unittest.TestCase):
    @patch("routes.service_bridge.dpl.accept_plan")
    @patch("routes.service_bridge.dpl.get_plan")
    def test_supplied_plan_version_is_forwarded_exactly(self, get_plan, accept_plan):
        get_plan.return_value = {"version": 4}
        accept_plan.return_value = {"plan_version": 3, "worker_id": "10"}

        result = _handlers().bridge_accept_plan("PLAN-1", "10", plan_version=3)

        accept_plan.assert_called_once_with("PLAN-1", 3, "10")
        self.assertEqual(result["acceptance"]["plan_version"], 3)

    @patch("routes.service_bridge.dpl.accept_plan")
    @patch("routes.service_bridge.dpl.get_plan")
    def test_stale_plan_version_becomes_http_409(self, get_plan, accept_plan):
        get_plan.return_value = {"version": 4}
        accept_plan.side_effect = service_bridge.dpl.StaleAcceptanceError(
            "Plan PLAN-1 is now at version 4, but acceptance was requested for version 3"
        )

        with self.assertRaises(HTTPException) as ctx:
            _handlers().bridge_accept_plan("PLAN-1", "10", plan_version=3)

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("version 4", str(ctx.exception.detail))

    @patch("routes.service_bridge.dpl.accept_plan")
    @patch("routes.service_bridge.dpl.get_plan")
    def test_legacy_caller_without_version_still_uses_current_version(self, get_plan, accept_plan):
        get_plan.return_value = {"version": 4}
        accept_plan.return_value = {"plan_version": 4, "worker_id": "10"}

        _handlers().bridge_accept_plan("PLAN-1", "10")

        accept_plan.assert_called_once_with("PLAN-1", 4, "10")


if __name__ == "__main__":
    unittest.main()
