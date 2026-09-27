"""Tests for the real HTTP transport (backend/core/grandmont_core_client.py
CoreHttpTransport) against the authoritative Core contract: auth header,
error classification, and credential-safety.

Uses httpx.MockTransport so no real network call is ever made.
"""
import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.grandmont_core_client import (  # noqa: E402
    CoreErrorKind,
    CoreHttpTransport,
    CoreIntegrationConfig,
    CoreIntegrationError,
    GrandmontCoreClient,
    get_core_client,
    reset_core_client_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_core_client_for_tests()
    yield
    reset_core_client_for_tests()


def _config(**overrides):
    defaults = dict(
        enabled=True,
        base_url='https://core.example.internal',
        service_credential='super-secret-token',
        timeout_seconds=1.0,
    )
    defaults.update(overrides)
    return CoreIntegrationConfig(**defaults)


def _transport_with(handler):
    config = _config()
    mock = httpx.MockTransport(handler)
    client = httpx.Client(base_url=config.base_url, transport=mock)
    return CoreHttpTransport(config, client=client)


class TestAuthHeader:
    def test_command_sends_service_token_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['token'] = request.headers.get('X-Core-Service-Token')
            return httpx.Response(200, json={"absence": {"id": "a1"}})

        transport = _transport_with(handler)
        transport('absence/create', {"foo": "bar"}, 'idem-1', 1.0)
        assert seen['token'] == 'super-secret-token'

    def test_read_sends_service_token_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['token'] = request.headers.get('X-Core-Service-Token')
            return httpx.Response(200, json={"assignments": []})

        transport = _transport_with(handler)
        transport.get('/assignments', {"worker_id": "w1"}, 1.0)
        assert seen['token'] == 'super-secret-token'

    def test_credential_never_appears_in_raised_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"code": "internal", "message": "boom"})

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport('absence/create', {}, 'idem-1', 1.0)
        assert 'super-secret-token' not in str(exc_info.value)
        assert 'super-secret-token' not in exc_info.value.message

    def test_credential_never_appears_in_connection_error_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport('absence/create', {}, 'idem-1', 1.0)
        assert 'super-secret-token' not in str(exc_info.value)


class TestErrorClassification:
    def test_4xx_is_rejected(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"code": "validation_error", "message": "bad payload"})

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport('absence/create', {}, 'idem-1', 1.0)
        assert exc_info.value.kind == CoreErrorKind.REJECTED
        assert exc_info.value.status_code == 422

    def test_404_is_rejected_with_status_code(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"code": "not_found", "message": "no mapping"})

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport.get('/worker-identity/resolve', {}, 1.0)
        assert exc_info.value.kind == CoreErrorKind.REJECTED
        assert exc_info.value.status_code == 404

    def test_5xx_is_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"code": "unavailable", "message": "try later"})

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport('absence/create', {}, 'idem-1', 1.0)
        assert exc_info.value.kind == CoreErrorKind.UNAVAILABLE

    def test_network_error_is_unavailable(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = _transport_with(handler)
        with pytest.raises(CoreIntegrationError) as exc_info:
            transport('absence/create', {}, 'idem-1', 1.0)
        assert exc_info.value.kind == CoreErrorKind.UNAVAILABLE

    def test_timeout_raises_timeout_error_for_command_wrapping(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        transport = _transport_with(handler)
        with pytest.raises(TimeoutError):
            transport('absence/create', {}, 'idem-1', 1.0)

    def test_client_command_wraps_transport_timeout_as_timeout_kind(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.TimeoutException("timed out")

        config = _config()
        mock = httpx.MockTransport(handler)
        http_client = httpx.Client(base_url=config.base_url, transport=mock)
        transport = CoreHttpTransport(config, client=http_client)
        client = GrandmontCoreClient(config=config, transport=transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('absence/create', {}, idempotency_key='idem-1')
        assert exc_info.value.kind == CoreErrorKind.TIMEOUT

    def test_client_read_wraps_5xx_as_unavailable_kind(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"message": "boom"})

        config = _config()
        mock = httpx.MockTransport(handler)
        http_client = httpx.Client(base_url=config.base_url, transport=mock)
        transport = CoreHttpTransport(config, client=http_client)
        client = GrandmontCoreClient(config=config, transport=transport, read_transport=transport.get)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.list_assignments(worker_id='w1')
        assert exc_info.value.kind == CoreErrorKind.UNAVAILABLE


class TestSuccessPaths:
    def test_command_success_returns_body(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"absence": {"id": "a1", "status": "pending"}})

        transport = _transport_with(handler)
        raw = transport('absence/create', {"worker_id": "w1"}, 'idem-1', 1.0)
        assert raw == {"absence": {"id": "a1", "status": "pending"}}

    def test_command_envelope_shape(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json
            seen['body'] = json.loads(request.content)
            seen['path'] = request.url.path
            return httpx.Response(200, json={"absence": {}})

        transport = _transport_with(handler)
        transport('absence/create', {"worker_id": "w1"}, 'stable-key-1', 1.0)
        assert seen['path'] == '/commands/absence/create'
        assert seen['body']['idempotency_key'] == 'stable-key-1'
        assert seen['body']['actor_id'] == 'service:miniapp'
        assert seen['body']['source'] == 'miniapp'
        assert seen['body']['payload'] == {"worker_id": "w1"}

    def test_read_query_params_sent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen['params'] = dict(request.url.params)
            return httpx.Response(200, json={"assignments": []})

        transport = _transport_with(handler)
        transport.get('/assignments', {"worker_id": "w1", "status": "active"}, 1.0)
        assert seen['params'] == {"worker_id": "w1", "status": "active"}


class TestDisabledModeNeverConstructsHttp:
    def test_get_core_client_disabled_has_no_transport(self, monkeypatch):
        monkeypatch.delenv('CORE_INTEGRATION_ENABLED', raising=False)
        client = get_core_client()
        assert client.transport is None
        assert client.read_transport is None
