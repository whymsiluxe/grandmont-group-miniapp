import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


def test_lifespan_preserves_startup_and_shutdown_semantics(monkeypatch):
    calls = []

    monkeypatch.setattr(
        backend,
        '_reconcile_missing_outbox_events',
        lambda: calls.append('reconcile') or 0,
    )
    monkeypatch.setattr(
        backend,
        '_retry_pending_outbox_events',
        lambda: calls.append('retry') or 0,
    )
    monkeypatch.setattr(
        backend,
        '_migrate_abwesenheit_legacy_ids',
        lambda: calls.append('migrate') or 0,
    )

    async def fake_sweep_loop():
        calls.append('sweep-start')
        try:
            await asyncio.Event().wait()
        finally:
            calls.append('sweep-stop')

    monkeypatch.setattr(backend, '_outbox_retry_sweep_loop', fake_sweep_loop)
    backend._outbox_retry_task = None
    backend._outbox_sweep_in_progress = True

    async def scenario():
        async with backend._lifespan(backend.app):
            await asyncio.sleep(0)
            assert calls[:3] == ['reconcile', 'retry', 'migrate']
            assert 'sweep-start' in calls
            assert backend._outbox_sweep_in_progress is False
            assert backend._outbox_retry_task is not None
            assert not backend._outbox_retry_task.done()

        assert backend._outbox_retry_task is None

    asyncio.run(scenario())
    assert calls[-1] == 'sweep-stop'


def test_fastapi_uses_lifespan_without_deprecated_on_event_decorators():
    source = (Path(__file__).resolve().parents[1] / 'backend/main.py').read_text(encoding='utf-8')
    assert '@app.on_event(' not in source
    assert 'lifespan=_lifespan' in source
