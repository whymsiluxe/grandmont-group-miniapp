"""Read-only shift/check-in bridge (item 4) -- CRM must never start/stop a
shift; this only exposes canonical data via GET /api/bridge/shift/active and
/api/bridge/shift/history. Hours come from the SAME _hours_from_session /
_session_hours_live functions checkin.py's own consumers use (injected via
ServiceBridgeRouteDeps), never reimplemented here.

Router + deps are constructed directly (not through main.py's live wiring)
so tests are hermetic -- no real checkin_meta.json, no env-var token needed
except where the token dependency itself is under test.

Run:
    /home/promonta/agent/miniapp/.venv/bin/python3 -m unittest tests.test_service_bridge_shift -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402

from routes.service_bridge import (  # noqa: E402
    ServiceBridgeRouteDeps,
    create_service_bridge_router,
    verify_service_token,
)


def _session(session_id='S1', user_id='10', object_id='OBJ-1', date='2026-09-29',
             start_at=1000, finish_at=None, pause_accumulated_seconds=0,
             daily_plan_id=None, daily_plan_version=None, daily_plan_acceptance_id=None,
             pause_started_at=None):
    return {
        'id': session_id, 'user_id': user_id, 'object_id': object_id, 'date': date,
        'start_at': start_at, 'finish_at': finish_at,
        'pause_accumulated_seconds': pause_accumulated_seconds, 'pause_started_at': pause_started_at,
        'daily_plan_id': daily_plan_id, 'daily_plan_version': daily_plan_version,
        'daily_plan_acceptance_id': daily_plan_acceptance_id,
        # fields that must NEVER reach the CRM bridge response
        'start_lat': '52.5', 'start_lon': '13.4', 'start_accuracy': '5',
        'start_geo_timestamp': '1000', 'start_photos': ['/data/photo1.jpg'],
        'finish_lat': '52.5', 'finish_lon': '13.4', 'finish_photos': ['/data/photo2.jpg'],
    }


def _make_router(sessions, roles=None):
    roles = roles if roles is not None else {'10': 'worker'}
    deps = ServiceBridgeRouteDeps(
        business_today=lambda: None,
        load_roles=lambda: roles,
        load_checkin_meta=lambda: sessions,
        is_active_photo_checkin_session=lambda s: bool(s) and s.get('finish_at') is None,
        hours_from_session=lambda s: max(0, (s['finish_at'] - s['start_at']) - s.get('pause_accumulated_seconds', 0)) / 3600.0 if s.get('finish_at') else 0.0,
        session_hours_live=lambda s: max(0, (2000 - s['start_at']) - s.get('pause_accumulated_seconds', 0)) / 3600.0,
    )
    _, handlers = create_service_bridge_router(deps)
    return handlers


class TestServiceTokenGate(unittest.TestCase):
    def test_no_token_configured_returns_503(self):
        os.environ.pop('MINIAPP_SERVICE_TOKEN', None)
        with self.assertRaises(HTTPException) as ctx:
            verify_service_token(x_miniapp_service_token=None)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_wrong_token_returns_401(self):
        os.environ['MINIAPP_SERVICE_TOKEN'] = 'secret-1'
        try:
            with self.assertRaises(HTTPException) as ctx:
                verify_service_token(x_miniapp_service_token='wrong')
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            os.environ.pop('MINIAPP_SERVICE_TOKEN', None)

    def test_correct_token_passes(self):
        os.environ['MINIAPP_SERVICE_TOKEN'] = 'secret-1'
        try:
            verify_service_token(x_miniapp_service_token='secret-1')  # no raise
        finally:
            os.environ.pop('MINIAPP_SERVICE_TOKEN', None)


class TestBridgeShiftActive(unittest.TestCase):
    def test_no_active_shift(self):
        handlers = _make_router([_session(finish_at=1500)])
        result = handlers.bridge_shift_active('10')
        self.assertEqual(result, {"has_active_shift": False})

    def test_active_shift_positive_case(self):
        handlers = _make_router([_session(session_id='S-active', finish_at=None, start_at=1000)])
        result = handlers.bridge_shift_active('10')
        self.assertTrue(result["has_active_shift"])
        shift = result["shift"]
        self.assertEqual(shift["id"], 'S-active')
        self.assertTrue(shift["is_active"])
        self.assertIsNone(shift["finish_at"])
        self.assertIn("net_hours", shift)
        self.assertIn("net_seconds", shift)

    def test_response_excludes_gps_and_photos(self):
        handlers = _make_router([_session(session_id='S-active', finish_at=None)])
        shift = handlers.bridge_shift_active('10')["shift"]
        for leaked_field in ('start_lat', 'start_lon', 'start_accuracy', 'start_geo_timestamp',
                              'start_photos', 'finish_lat', 'finish_lon', 'finish_photos'):
            self.assertNotIn(leaked_field, shift)

    def test_unknown_worker_404(self):
        handlers = _make_router([], roles={})
        with self.assertRaises(HTTPException) as ctx:
            handlers.bridge_shift_active('999')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_worker_identity_isolation(self):
        """Only the requested telegram_user_id's session is ever returned --
        confirms no cross-worker leakage through this endpoint."""
        handlers = _make_router(
            [_session(session_id='S-mine', user_id='10', finish_at=None),
             _session(session_id='S-other', user_id='20', finish_at=None)],
            roles={'10': 'worker', '20': 'worker'},
        )
        result = handlers.bridge_shift_active('10')
        self.assertEqual(result["shift"]["id"], 'S-mine')


class TestBridgeShiftHistory(unittest.TestCase):
    def test_date_filtering(self):
        sessions = [
            _session(session_id='S1', date='2026-09-01', finish_at=2000),
            _session(session_id='S2', date='2026-09-15', finish_at=2000),
            _session(session_id='S3', date='2026-09-30', finish_at=2000),
        ]
        handlers = _make_router(sessions)
        result = handlers.bridge_shift_history('10', date_from='2026-09-10', date_to='2026-09-20')
        ids = [s['id'] for s in result['shifts']]
        self.assertEqual(ids, ['S2'])

    def test_no_cross_worker_leakage_in_history(self):
        handlers = _make_router(
            [_session(session_id='S-mine', user_id='10', finish_at=2000),
             _session(session_id='S-other', user_id='20', finish_at=2000)],
            roles={'10': 'worker', '20': 'worker'},
        )
        result = handlers.bridge_shift_history('10')
        ids = [s['id'] for s in result['shifts']]
        self.assertEqual(ids, ['S-mine'])

    def test_finished_session_hours_use_canonical_formula(self):
        # start_at=1000, finish_at=4600 -> 3600s gross, pause 600s -> net 3000s = 0.833h
        handlers = _make_router([_session(session_id='S1', start_at=1000, finish_at=4600, pause_accumulated_seconds=600)])
        result = handlers.bridge_shift_history('10')
        shift = result['shifts'][0]
        self.assertAlmostEqual(shift['net_seconds'], 3000, delta=1)
        self.assertFalse(shift['is_active'])

    def test_unknown_worker_404(self):
        handlers = _make_router([], roles={})
        with self.assertRaises(HTTPException) as ctx:
            handlers.bridge_shift_history('999')
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == '__main__':
    unittest.main()
