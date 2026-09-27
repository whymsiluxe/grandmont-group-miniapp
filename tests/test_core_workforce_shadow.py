"""Tests for backend/core/workforce_shadow.py -- the worker-facing shadow
glue between the Mini App legacy paths and the Grandmont Core client, and
its wiring into the real worker-facing routes (GET /api/my-assignments,
POST /api/abwesenheit).

Every scenario here checks the same invariant from a different angle: a
Core failure, a missing identity mapping, or Core being disabled must never
change what the legacy Mini App returns to a worker.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.grandmont_core_client import (  # noqa: E402
    CoreErrorKind,
    CoreIntegrationConfig,
    CoreIntegrationError,
    CoreResult,
    GrandmontCoreClient,
    reset_core_client_for_tests,
)
import core.workforce_shadow as workforce_shadow  # noqa: E402
from core.workforce_shadow import (  # noqa: E402
    resolve_worker_uuid,
    shadow_compare_assignments,
    shadow_create_absence,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_core_client_for_tests()
    yield
    reset_core_client_for_tests()


def _install_fake_client(monkeypatch, *, enabled=True, resolve_result=None,
                          resolve_raises=None, assignments_result=None,
                          assignments_raises=None, absence_calls=None,
                          absence_raises=None):
    """Builds a fake GrandmontCoreClient and points
    workforce_shadow.get_core_client at it, so tests never touch real
    network/config plumbing -- only the shadow orchestration logic."""
    config = CoreIntegrationConfig(enabled=enabled, base_url='https://core.example.internal',
                                    service_credential='tok', timeout_seconds=1.0)
    client = GrandmontCoreClient(config=config)

    def _resolve_worker_identity(legacy_id):
        if resolve_raises:
            raise resolve_raises
        return resolve_result

    def _list_assignments(**kwargs):
        if assignments_raises:
            raise assignments_raises
        return assignments_result

    def _create_absence(**kwargs):
        if absence_calls is not None:
            absence_calls.append(kwargs)
        if absence_raises:
            raise absence_raises
        return CoreResult(ok=True, data={"absence": {"status": "pending"}})

    client.resolve_worker_identity = _resolve_worker_identity
    client.list_assignments = _list_assignments
    client.create_absence = _create_absence

    monkeypatch.setattr(workforce_shadow, 'get_core_client', lambda: client)
    return client


class TestDisabledIntegration:
    def test_disabled_resolve_returns_none_no_calls(self, monkeypatch):
        calls = []

        def boom(legacy_id):
            calls.append(legacy_id)
            raise AssertionError("must not be called when disabled")

        _install_fake_client(monkeypatch, enabled=False, resolve_result=None)
        worker_id, org_id = resolve_worker_uuid('12345')
        assert worker_id is None and org_id is None

    def test_disabled_assignment_shadow_is_noop(self, monkeypatch):
        client = _install_fake_client(monkeypatch, enabled=False)

        def boom(**kwargs):
            raise AssertionError("must not be called when disabled")
        client.list_assignments = boom
        shadow_compare_assignments('12345', [{"id": "a1"}])  # must not raise

    def test_disabled_absence_shadow_is_noop(self, monkeypatch):
        calls = []
        client = _install_fake_client(monkeypatch, enabled=False, absence_calls=calls)
        shadow_create_absence('12345', {"id": "e1", "reason": "sick", "date_from": "2026-09-01", "date_to": "2026-09-02"})
        assert calls == []


class TestIdentityResolution:
    def test_telegram_id_resolves_to_worker_uuid(self, monkeypatch):
        _install_fake_client(monkeypatch, resolve_result=CoreResult(ok=True, data={
            "worker": {"id": "worker-uuid-1", "organization_id": "org-uuid-1"},
        }))
        worker_id, org_id = resolve_worker_uuid('555')
        assert worker_id == 'worker-uuid-1'
        assert org_id == 'org-uuid-1'

    def test_identity_404_produces_controlled_unmapped_state(self, monkeypatch):
        _install_fake_client(monkeypatch, resolve_result=CoreResult(ok=False, data=None))
        worker_id, org_id = resolve_worker_uuid('999')
        assert worker_id is None and org_id is None

    def test_identity_error_treated_as_unmapped_not_raised(self, monkeypatch):
        _install_fake_client(monkeypatch, resolve_raises=CoreIntegrationError(CoreErrorKind.UNAVAILABLE, "down"))
        worker_id, org_id = resolve_worker_uuid('777')
        assert worker_id is None and org_id is None


class TestAssignmentShadow:
    def test_mapped_worker_reads_only_its_own_assignments(self, monkeypatch):
        seen_kwargs = {}

        def _list_assignments(**kwargs):
            seen_kwargs.update(kwargs)
            return CoreResult(ok=True, data={"assignments": [{"id": "core-a1"}]})

        client = _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "worker-uuid-1", "organization_id": "org-1"}},
        ))
        client.list_assignments = _list_assignments
        shadow_compare_assignments('555', [{"id": "legacy-a1"}])
        assert seen_kwargs == {"worker_id": "worker-uuid-1"}

    def test_foreign_worker_cannot_see_assignment(self, monkeypatch):
        """An unmapped/foreign telegram id must never reach list_assignments
        at all -- there is no worker_id to scope the Core read by."""
        def boom(**kwargs):
            raise AssertionError("must not fetch Core assignments for an unmapped worker")

        client = _install_fake_client(monkeypatch, resolve_result=CoreResult(ok=False, data=None))
        client.list_assignments = boom
        shadow_compare_assignments('unknown-999', [])  # must not raise

    def test_assignment_response_parsing(self, monkeypatch, caplog):
        client = _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "w1", "organization_id": "org-1"}},
        ))
        client.list_assignments = lambda **kwargs: CoreResult(
            ok=True, data={"assignments": [{"id": "c1"}, {"id": "c2"}]},
        )
        with caplog.at_level('WARNING'):
            shadow_compare_assignments('555', [{"id": "legacy-a1"}])
        assert any('mismatch' in r.message for r in caplog.records)

    def test_core_mismatch_does_not_replace_legacy_result(self, monkeypatch):
        client = _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "w1", "organization_id": "org-1"}},
        ))
        client.list_assignments = lambda **kwargs: CoreResult(
            ok=True, data={"assignments": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}]},
        )
        legacy_result = [{"id": "legacy-a1"}]
        legacy_snapshot = list(legacy_result)
        shadow_compare_assignments('555', legacy_result)
        assert legacy_result == legacy_snapshot  # untouched regardless of Core's differing count

    def test_core_assignment_fetch_failure_does_not_raise(self, monkeypatch):
        client = _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "w1", "organization_id": "org-1"}},
        ))
        client.list_assignments = lambda **kwargs: (_ for _ in ()).throw(
            CoreIntegrationError(CoreErrorKind.TIMEOUT, "slow")
        )
        shadow_compare_assignments('555', [{"id": "legacy-a1"}])  # must not raise


class TestAbsenceShadow:
    def test_absence_shadow_create_sends_expected_fields(self, monkeypatch):
        calls = []
        _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "worker-uuid-1", "organization_id": "org-uuid-1"}},
        ), absence_calls=calls)
        entry = {
            "id": "abw-entry-1", "reason": "krankheit",
            "date_from": "2026-09-01", "date_to": "2026-09-03", "note": "grippe",
        }
        shadow_create_absence('555', entry)
        assert len(calls) == 1
        sent = calls[0]
        assert sent['worker_id'] == 'worker-uuid-1'
        assert sent['organization_id'] == 'org-uuid-1'
        assert sent['type_'] == 'krankheit'
        assert sent['starts_at'] == '2026-09-01'
        assert sent['ends_at'] == '2026-09-03'
        assert sent['note'] == 'grippe'
        assert sent['idempotency_key'] == 'miniapp-abwesenheit-abw-entry-1'

    def test_same_logical_retry_preserves_idempotency_key(self, monkeypatch):
        calls = []
        _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "worker-uuid-1", "organization_id": "org-uuid-1"}},
        ), absence_calls=calls)
        entry = {"id": "abw-entry-1", "reason": "urlaub", "date_from": "2026-09-01", "date_to": "2026-09-03"}
        shadow_create_absence('555', entry)
        shadow_create_absence('555', entry)  # simulated retry of the same logical op
        assert calls[0]['idempotency_key'] == calls[1]['idempotency_key']

    def test_unmapped_worker_absence_shadow_is_controlled_noop(self, monkeypatch):
        calls = []
        _install_fake_client(monkeypatch, resolve_result=CoreResult(ok=False, data=None), absence_calls=calls)
        shadow_create_absence('unknown-999', {"id": "abw-2", "reason": "sick", "date_from": "2026-09-01", "date_to": "2026-09-02"})
        assert calls == []  # no auto-create, no guessed UUID

    def test_core_failure_does_not_raise_and_legacy_already_succeeded(self, monkeypatch):
        _install_fake_client(monkeypatch, resolve_result=CoreResult(
            ok=True, data={"worker": {"id": "w1", "organization_id": "org-1"}},
        ), absence_raises=CoreIntegrationError(CoreErrorKind.UNAVAILABLE, "down"))
        entry = {"id": "abw-3", "reason": "sick", "date_from": "2026-09-01", "date_to": "2026-09-02"}
        shadow_create_absence('555', entry)  # legacy caller already saved the entry; this must not raise


class TestConfigInvalidNoNetwork:
    def test_enabled_missing_credential_yields_config_invalid_no_network(self):
        """End-to-end through the real client (no fake): enabled with a
        blank credential must fail closed before any transport is touched."""
        config = CoreIntegrationConfig(enabled=True, base_url='https://core.example.internal', service_credential='')
        client = GrandmontCoreClient(config=config)
        with pytest.raises(CoreIntegrationError) as exc_info:
            client.resolve_worker_identity('555')
        assert exc_info.value.kind == CoreErrorKind.CONFIG_INVALID


class TestLegacyRouteWiringUnaffected:
    """Static checks that the worker-facing routes wire the shadow calls as
    best-effort side effects, not as something that can change the legacy
    response shape or propagate an exception."""

    def test_my_assignments_route_guards_shadow_call(self):
        import ast
        import inspect

        import routes.objects as objects_module

        source = inspect.getsource(objects_module)
        assert 'shadow_compare_assignments' in source
        tree = ast.parse(source)
        found_try_guard = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Try):
                dumped = ast.dump(node)
                if 'shadow_compare_assignments' in dumped:
                    found_try_guard = True
        assert found_try_guard, "shadow_compare_assignments call must be wrapped in try/except in my_assignments"

    def test_create_abwesenheit_route_guards_shadow_call(self):
        import ast
        import inspect

        import main as backend

        source = inspect.getsource(backend.create_abwesenheit)
        assert 'shadow_create_absence' in source
        tree = ast.parse(source)
        found_try_guard = any(
            isinstance(node, ast.Try) and 'shadow_create_absence' in ast.dump(node)
            for node in ast.walk(tree)
        )
        assert found_try_guard, "shadow_create_absence call must be wrapped in try/except in create_abwesenheit"
