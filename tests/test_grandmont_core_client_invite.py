"""consume_worker_invite / create_worker_onboarding_request -- the two
outbound Core calls the worker-onboarding-invite flow needs from Miniapp.
Same fake-transport pattern as test_core_integration_seam.py's
TestEnabledModeWithFakeTransport: no real network, verifies the command
name/payload shape sent to transport, not Core's actual behavior (that's
covered on the Core side by test_worker_invite.py).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.grandmont_core_client import (  # noqa: E402
    CoreIntegrationConfig,
    GrandmontCoreClient,
)


def _enabled_client(transport):
    config = CoreIntegrationConfig(
        enabled=True, base_url='https://core.example.internal',
        service_credential='test-credential', timeout_seconds=1.0,
    )
    return GrandmontCoreClient(config=config, transport=transport)


class TestConsumeWorkerInvite:
    def test_sends_correct_command_name_and_payload(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['name'] = name
            seen['payload'] = payload
            return {"invite": {"id": "inv-1", "status": "consumed"}, "worker": {"id": "w-1"}}

        client = _enabled_client(fake_transport)
        result = client.consume_worker_invite(
            organization_id='org-1', token_hash='a' * 64,
            telegram_user_id='555', idempotency_key='consume-key-1',
        )
        assert seen['name'] == 'worker-invite/consume'
        assert seen['payload'] == {
            'organization_id': 'org-1', 'token_hash': 'a' * 64, 'telegram_user_id': '555',
        }
        assert result.ok is True
        assert result.data['worker']['id'] == 'w-1'

    def test_never_sends_raw_token_only_hash(self):
        """The caller (routes/service_bridge.py) is responsible for hashing
        before calling this -- this test locks the payload shape so that
        contract can never regress to sending a raw token field."""
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['payload'] = payload
            return {"invite": {}, "worker": {}}

        client = _enabled_client(fake_transport)
        client.consume_worker_invite(
            organization_id='org-1', token_hash='b' * 64,
            telegram_user_id='555', idempotency_key='k',
        )
        assert 'raw_token' not in seen['payload']
        assert 'token' not in seen['payload']
        assert seen['payload']['token_hash'] == 'b' * 64

    def test_idempotency_key_propagates_unchanged(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['key'] = key
            return {}

        client = _enabled_client(fake_transport)
        client.consume_worker_invite(
            organization_id='org-1', token_hash='c' * 64,
            telegram_user_id='1', idempotency_key='caller-key-42',
        )
        assert seen['key'] == 'caller-key-42'


class TestCreateWorkerOnboardingRequest:
    def test_sends_correct_command_name_and_payload(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['name'] = name
            seen['payload'] = payload
            return {"request": {"id": "req-1", "status": "pending"}}

        client = _enabled_client(fake_transport)
        result = client.create_worker_onboarding_request(
            organization_id='org-1', telegram_user_id='999', telegram_username='ivan',
            first_name='Ivan', last_name='Petrov', phone='+491761234567',
            idempotency_key='onboard-key-1',
        )
        assert seen['name'] == 'worker-onboarding/request'
        assert seen['payload'] == {
            'organization_id': 'org-1', 'telegram_user_id': '999', 'telegram_username': 'ivan',
            'first_name': 'Ivan', 'last_name': 'Petrov', 'phone': '+491761234567',
        }
        assert result.data['request']['status'] == 'pending'

    def test_disabled_client_never_calls_transport(self):
        """Same disabled-by-default safety net every other command on this
        client already has -- verified here so the new method can't
        accidentally skip _ensure_enabled()."""
        from core.grandmont_core_client import CoreIntegrationError

        config = CoreIntegrationConfig(enabled=False)
        client = GrandmontCoreClient(config=config)
        with pytest.raises(CoreIntegrationError):
            client.create_worker_onboarding_request(
                organization_id='org-1', telegram_user_id='1', telegram_username=None,
                first_name='A', last_name='B', phone='+1', idempotency_key='k',
            )
