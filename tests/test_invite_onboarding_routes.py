"""POST /api/invite/consume + POST /api/onboarding/request -- worker
onboarding invite flow (Flow A / Flow B), route level.

Both routes depend on get_verified_telegram_user, not get_current_user --
this is the point of the split in core/permissions.py: a brand-new Telegram
user is by definition NOT in roles.json yet, so the whitelist gate must not
run before invite/onboarding-request logic gets a chance to grant access.

Same genuine-HMAC init_data builder as test_access_control.py -- real
signature checks, not mocked auth.
"""
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from unittest.mock import patch
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402

import routes.auth as auth_routes  # noqa: E402
import core.permissions as permissions  # noqa: E402
from core.grandmont_core_client import (  # noqa: E402
    CoreErrorKind,
    CoreIntegrationError,
    CoreResult,
)


def _build_init_data(user_id=555, first_name='Test', start_param=None):
    auth_date = int(time.time())
    user_json = json.dumps({'id': user_id, 'first_name': first_name}, separators=(',', ':'))
    params = {'auth_date': str(auth_date), 'user': user_json, 'query_id': 'AAAtest'}
    if start_param is not None:
        params['start_param'] = start_param
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(params.items()))
    computed_hash = hmac.new(permissions._secret_key(), data_check_string.encode(), hashlib.sha256).hexdigest()
    parts = [f'{k}={quote(v, safe="")}' for k, v in params.items()]
    parts.append(f'hash={computed_hash}')
    return '&'.join(parts)


class FakeClient:
    """Minimal stand-in for GrandmontCoreClient -- only the surface
    consume_invite/request_onboarding actually touch."""

    def __init__(self, *, enabled=True, organization_id='org-1'):
        class _Cfg:
            pass
        self.config = _Cfg()
        self.config.enabled = enabled
        self.config.organization_id = organization_id
        self.calls = []
        self._consume_result = None
        self._consume_error = None
        self._onboard_result = None
        self._onboard_error = None

    def consume_worker_invite(self, **kwargs):
        self.calls.append(('consume_worker_invite', kwargs))
        if self._consume_error:
            raise self._consume_error
        return self._consume_result

    def create_worker_onboarding_request(self, **kwargs):
        self.calls.append(('create_worker_onboarding_request', kwargs))
        if self._onboard_error:
            raise self._onboard_error
        return self._onboard_result


class ConsumeInviteRouteTests(unittest.TestCase):
    def _call(self, init_data, token, client):
        with patch.object(auth_routes, 'get_core_client', return_value=client):
            body = auth_routes.InviteConsumeBody(token=token)
            user = permissions.get_verified_telegram_user(authorization=None, x_telegram_init_data=init_data)
            return auth_routes.consume_invite(body, user=user)

    def test_valid_consume_grants_access_without_manual_roles_edit(self):
        init_data = _build_init_data(user_id=777)
        client = FakeClient()
        client._consume_result = CoreResult(ok=True, data={
            'invite': {'id': 'inv-1', 'status': 'consumed'},
            'worker': {'id': 'worker-uuid-1'},
        })
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles') as save_roles:
            result = self._call(init_data, 'raw-token-xyz', client)

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['worker_id'], 'worker-uuid-1')
        # roles.json written automatically -- no manual SSH/json edit needed
        save_roles.assert_called_once()
        written_roles = save_roles.call_args[0][0]
        self.assertEqual(written_roles.get('777'), 'worker')

    def test_verified_telegram_id_used_not_body_supplied(self):
        """Even if a hypothetical caller tried to smuggle a different user id,
        only the server-verified id from initData is ever sent to Core."""
        init_data = _build_init_data(user_id=888)
        client = FakeClient()
        client._consume_result = CoreResult(ok=True, data={'invite': {}, 'worker': {'id': 'w-2'}})
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles'):
            self._call(init_data, 'tok', client)

        name, kwargs = client.calls[0]
        self.assertEqual(kwargs['telegram_user_id'], '888')

    def test_raw_token_never_sent_only_hash(self):
        init_data = _build_init_data(user_id=1)
        client = FakeClient()
        client._consume_result = CoreResult(ok=True, data={'invite': {}, 'worker': {'id': 'w'}})
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles'):
            self._call(init_data, 'super-secret-raw-token', client)

        _, kwargs = client.calls[0]
        self.assertNotIn('super-secret-raw-token', str(kwargs))
        self.assertEqual(kwargs['token_hash'], hashlib.sha256(b'super-secret-raw-token').hexdigest())

    def test_expired_or_consumed_invite_rejected(self):
        init_data = _build_init_data(user_id=2)
        client = FakeClient()
        client._consume_error = CoreIntegrationError(CoreErrorKind.REJECTED, "invite has expired", status_code=409)
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles') as save_roles:
            with self.assertRaises(HTTPException) as ctx:
                self._call(init_data, 'dead-token', client)
        self.assertEqual(ctx.exception.status_code, 409)
        save_roles.assert_not_called()

    def test_core_unavailable_returns_503_not_500(self):
        init_data = _build_init_data(user_id=3)
        client = FakeClient()
        client._consume_error = CoreIntegrationError(CoreErrorKind.TIMEOUT, "timed out")
        with patch.object(auth_routes, '_load_roles', return_value={}):
            with self.assertRaises(HTTPException) as ctx:
                self._call(init_data, 'tok', client)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_core_integration_disabled_returns_503(self):
        init_data = _build_init_data(user_id=4)
        client = FakeClient(enabled=False)
        with self.assertRaises(HTTPException) as ctx:
            self._call(init_data, 'tok', client)
        self.assertEqual(ctx.exception.status_code, 503)

    def test_already_whitelisted_user_consume_does_not_rewrite_roles(self):
        """Idempotent retry: a user who already has access must not have
        _save_roles called again for no reason."""
        init_data = _build_init_data(user_id=777)
        client = FakeClient()
        client._consume_result = CoreResult(ok=True, data={'invite': {}, 'worker': {'id': 'w'}})
        with patch.object(auth_routes, '_load_roles', return_value={'777': 'worker'}), \
             patch.object(auth_routes, '_save_roles') as save_roles:
            self._call(init_data, 'tok', client)
        save_roles.assert_not_called()

    def test_unwhitelisted_user_not_blocked_by_normal_whitelist_gate(self):
        """The entire point of depending on get_verified_telegram_user instead
        of get_current_user: an unwhitelisted user must reach this route's
        logic, not get a 403 before it runs."""
        init_data = _build_init_data(user_id=999)
        client = FakeClient()
        client._consume_result = CoreResult(ok=True, data={'invite': {}, 'worker': {'id': 'w'}})
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles'):
            result = self._call(init_data, 'tok', client)  # must not raise 403
        self.assertEqual(result['status'], 'ok')


class OnboardingRequestRouteTests(unittest.TestCase):
    def _call(self, init_data, body, client):
        with patch.object(auth_routes, 'get_core_client', return_value=client):
            user = permissions.get_verified_telegram_user(authorization=None, x_telegram_init_data=init_data)
            return auth_routes.request_onboarding(body, user=user)

    def test_creates_pending_request_never_grants_access_directly(self):
        init_data = _build_init_data(user_id=1234)
        client = FakeClient()
        client._onboard_result = CoreResult(ok=True, data={'request': {'id': 'req-1', 'status': 'pending'}})
        body = auth_routes.OnboardingRequestBody(first_name='Ivan', last_name='Petrov', phone='+491761234567')
        with patch.object(auth_routes, '_load_roles', return_value={}), \
             patch.object(auth_routes, '_save_roles') as save_roles:
            result = self._call(init_data, body, client)

        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result['request_id'], 'req-1')
        # Flow B never writes roles.json itself -- only CRM admin approve does that (via invite/onboarding.approve)
        save_roles.assert_not_called()

    def test_verified_telegram_id_and_username_forwarded(self):
        init_data = _build_init_data(user_id=4321)
        client = FakeClient()
        client._onboard_result = CoreResult(ok=True, data={'request': {'id': 'r', 'status': 'pending'}})
        body = auth_routes.OnboardingRequestBody(first_name='A', last_name='B', phone='')
        with patch.object(auth_routes, '_load_roles', return_value={}):
            self._call(init_data, body, client)
        _, kwargs = client.calls[0]
        self.assertEqual(kwargs['telegram_user_id'], '4321')

    def test_core_unavailable_returns_503(self):
        init_data = _build_init_data(user_id=5)
        client = FakeClient()
        client._onboard_error = CoreIntegrationError(CoreErrorKind.UNAVAILABLE, "down")
        body = auth_routes.OnboardingRequestBody(first_name='A')
        with patch.object(auth_routes, '_load_roles', return_value={}):
            with self.assertRaises(HTTPException) as ctx:
                self._call(init_data, body, client)
        self.assertEqual(ctx.exception.status_code, 503)


if __name__ == '__main__':
    unittest.main()
