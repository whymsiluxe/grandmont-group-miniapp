"""Tests for the Grandmont Core integration seam (backend/core/grandmont_core_client.py).

This seam is not wired into any route yet -- these tests exercise the
adapter module directly. The single hard invariant under test throughout:
disabled mode (the default) must never attempt anything network-shaped, and
must fail fast/cheap before touching config validation or a transport.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.grandmont_core_client import (  # noqa: E402
    CoreErrorKind,
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


class TestDisabledByDefault:
    def test_default_config_is_disabled(self):
        assert CoreIntegrationConfig().enabled is False

    def test_from_env_defaults_disabled_when_no_env_set(self, monkeypatch):
        monkeypatch.delenv('CORE_INTEGRATION_ENABLED', raising=False)
        assert CoreIntegrationConfig.from_env().enabled is False

    def test_disabled_client_never_calls_transport(self):
        calls = []

        def fake_transport(name, payload, key, timeout):
            calls.append((name, payload, key, timeout))
            return {"should": "never happen"}

        client = GrandmontCoreClient(config=CoreIntegrationConfig(enabled=False), transport=fake_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {"worker_id": "123"})

        assert exc_info.value.kind == CoreErrorKind.DISABLED
        assert calls == [], "transport must not be invoked when integration is disabled"

    def test_disabled_client_does_not_validate_config_first(self):
        """Even a config that would otherwise fail validation (missing
        base_url) must short-circuit on the disabled check first -- disabled
        mode should never even reach config validation, let alone a network
        call."""
        client = GrandmontCoreClient(config=CoreIntegrationConfig(enabled=False, base_url=''))
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('anything', {})
        assert exc_info.value.kind == CoreErrorKind.DISABLED

    def test_get_core_client_singleton_defaults_disabled(self, monkeypatch):
        monkeypatch.delenv('CORE_INTEGRATION_ENABLED', raising=False)
        client = get_core_client()
        assert client.config.enabled is False


class TestConfigValidation:
    def test_enabled_without_base_url_is_invalid(self):
        config = CoreIntegrationConfig(enabled=True, base_url='')
        with pytest.raises(CoreIntegrationError) as exc_info:
            config.validate()
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID

    def test_enabled_without_service_credential_is_invalid(self):
        config = CoreIntegrationConfig(
            enabled=True, base_url='https://core.example.internal', service_credential='',
        )
        with pytest.raises(CoreIntegrationError) as exc_info:
            config.validate()
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID

    def test_enabled_missing_credential_rejected_before_network(self):
        """CONFIG_INVALID must be raised by command() itself (via
        _ensure_enabled) before any transport is ever touched."""
        calls = []

        def fake_transport(name, payload, key, timeout):
            calls.append((name, payload, key, timeout))
            return {}

        config = CoreIntegrationConfig(
            enabled=True, base_url='https://core.example.internal', service_credential='',
        )
        client = GrandmontCoreClient(config=config, transport=fake_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='req-1')
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID
        assert calls == []

    def test_enabled_with_base_url_and_no_transport_is_config_invalid(self):
        config = CoreIntegrationConfig(enabled=True, base_url='https://core.example.internal')
        client = GrandmontCoreClient(config=config, transport=None)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='req-no-transport')
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID

    def test_disabled_config_skips_validation_even_if_otherwise_invalid(self):
        config = CoreIntegrationConfig(enabled=False, base_url='', timeout_seconds=-1)
        config.validate()  # must not raise

    def test_zero_or_negative_timeout_is_invalid_when_enabled(self):
        config = CoreIntegrationConfig(enabled=True, base_url='https://core.example.internal', timeout_seconds=0)
        with pytest.raises(CoreIntegrationError) as exc_info:
            config.validate()
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID

    def test_from_env_reads_base_url_and_credential_and_timeout(self, monkeypatch):
        monkeypatch.setenv('CORE_INTEGRATION_ENABLED', 'true')
        monkeypatch.setenv('CORE_BASE_URL', 'https://core.internal')
        monkeypatch.setenv('CORE_SERVICE_CREDENTIAL', 'placeholder-token')
        monkeypatch.setenv('CORE_TIMEOUT_SECONDS', '2.5')
        config = CoreIntegrationConfig.from_env()
        assert config.enabled is True
        assert config.base_url == 'https://core.internal'
        assert config.service_credential == 'placeholder-token'
        assert config.timeout_seconds == 2.5

    @pytest.mark.parametrize('raw,expected', [
        ('1', True), ('true', True), ('True', True), ('yes', True), ('on', True),
        ('0', False), ('false', False), ('', False), ('nonsense', False),
    ])
    def test_env_bool_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv('CORE_INTEGRATION_ENABLED', raw)
        assert CoreIntegrationConfig.from_env().enabled is expected


class TestEnabledModeWithFakeTransport:
    def _enabled_client(self, transport):
        config = CoreIntegrationConfig(
            enabled=True, base_url='https://core.example.internal',
            service_credential='test-credential', timeout_seconds=1.0,
        )
        return GrandmontCoreClient(config=config, transport=transport)

    def test_successful_command_returns_ok_result(self):
        def fake_transport(name, payload, key, timeout):
            return {"echo": name, "payload": payload}

        client = self._enabled_client(fake_transport)
        result = client.command('assign_worker', {"worker_id": "42"}, idempotency_key='req-1')
        assert result.ok is True
        assert result.idempotency_key == 'req-1'
        assert result.data == {"echo": "assign_worker", "payload": {"worker_id": "42"}}

    def test_idempotency_key_propagates_to_transport_unchanged(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['key'] = key
            return {}

        client = self._enabled_client(fake_transport)
        client.command('assign_worker', {}, idempotency_key='caller-supplied-key-123')
        assert seen['key'] == 'caller-supplied-key-123'

    def test_missing_idempotency_key_is_rejected_not_generated(self):
        """A silently client-generated key would defeat idempotency itself:
        a caller retry (timeout, crash, double-submit) must reuse the SAME
        key so Core recognizes the replay. Missing key is a caller bug --
        fail loudly, never mint a fresh one."""
        calls = []

        def fake_transport(name, payload, key, timeout):
            calls.append(key)
            return {}

        client = self._enabled_client(fake_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {})
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID
        assert calls == [], "transport must not be invoked when idempotency_key is missing"

    def test_blank_idempotency_key_is_rejected(self):
        client = self._enabled_client(lambda name, payload, key, timeout: {})
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='   ')
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID

    def test_explicit_key_passed_unchanged(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['key'] = key
            return {}

        client = self._enabled_client(fake_transport)
        result = client.command('assign_worker', {}, idempotency_key='caller-key-abc')
        assert seen['key'] == 'caller-key-abc'
        assert result.idempotency_key == 'caller-key-abc'

    def test_timeout_seconds_propagates_to_transport(self):
        seen = {}

        def fake_transport(name, payload, key, timeout):
            seen['timeout'] = timeout
            return {}

        config = CoreIntegrationConfig(
            enabled=True, base_url='https://core.example.internal',
            service_credential='test-credential', timeout_seconds=3.25,
        )
        client = GrandmontCoreClient(config=config, transport=fake_transport)
        client.command('assign_worker', {}, idempotency_key='req-timeout-test')
        assert seen['timeout'] == 3.25

    def test_transport_timeout_error_is_wrapped(self):
        def failing_transport(name, payload, key, timeout):
            raise TimeoutError("core did not respond in time")

        client = self._enabled_client(failing_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='req-timeout')
        assert exc_info.value.kind == CoreErrorKind.TIMEOUT

    def test_transport_generic_exception_is_wrapped_as_unavailable(self):
        def failing_transport(name, payload, key, timeout):
            raise ConnectionError("connection refused")

        client = self._enabled_client(failing_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='req-unavailable')
        assert exc_info.value.kind == CoreErrorKind.UNAVAILABLE

    def test_transport_raised_core_integration_error_passes_through_unchanged(self):
        def failing_transport(name, payload, key, timeout):
            raise CoreIntegrationError(CoreErrorKind.REJECTED, "Core rejected the command: duplicate assignment")

        client = self._enabled_client(failing_transport)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.command('assign_worker', {}, idempotency_key='req-rejected')
        assert exc_info.value.kind == CoreErrorKind.REJECTED


class TestLegacyPathUnaffected:
    """The seam module must be a no-op for current production behavior: it
    is not imported by main.py or any route module, so simply importing it
    (as this test file does at module level) must not change any existing
    legacy behavior. This test documents that expectation explicitly rather
    than relying on it being implicitly true."""

    def test_daily_plan_lib_does_not_import_core_client(self):
        import ast
        import inspect

        import daily_plan_lib as dpl

        source = inspect.getsource(dpl)
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
        assert not any('grandmont_core_client' in name for name in imported_names)

    def test_assignment_matching_does_not_import_core_client(self):
        import ast
        import inspect

        import assignment_matching as am

        source = inspect.getsource(am)
        tree = ast.parse(source)
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported_names.add(alias.name)
        assert not any('grandmont_core_client' in name for name in imported_names)
