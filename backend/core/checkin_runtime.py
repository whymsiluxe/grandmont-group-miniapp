"""Shared check-in runtime infrastructure.

Owns the durable idempotency state machine and Finish projector outbox that are
shared by routes/checkin.py and routes/execution.py.  The module deliberately
has no dependency on backend.main; file paths and legacy collaborators are
injected by the composition root so tests can still isolate stores dynamically.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import HTTPException


@dataclass(frozen=True)
class IdempotencyDeps:
    file_path: Callable[[], str]
    ttl_seconds: int
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    now: Callable[[], float]


class IdempotencyRuntime:
    """Durable, request-scoped check-in idempotency state machine."""

    def __init__(self, deps: IdempotencyDeps):
        self.deps = deps
        self.cache: dict[str, dict] = {}
        self.lock = threading.Lock()

    def payload_hash(self, payload: dict | None) -> str:
        raw = json.dumps(
            payload or {}, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str,
        )
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def scope(
        self,
        kind: str,
        actor_id: str | int,
        entity_id: str = '',
        payload: dict | None = None,
    ) -> dict:
        return {
            'kind': str(kind),
            'actor_id': str(actor_id),
            'entity_id': str(entity_id or ''),
            'payload_hash': self.payload_hash(payload),
        }

    def identity(self, key: str, scope: dict | None = None) -> str:
        scope = scope or {
            'kind': 'legacy', 'actor_id': '', 'entity_id': '', 'payload_hash': '',
        }
        raw = json.dumps({
            'key': str(key),
            'kind': str(scope.get('kind') or ''),
            'actor_id': str(scope.get('actor_id') or ''),
        }, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(raw.encode('utf-8')).hexdigest()

    def entity_id(self, key: str, scope: dict | None) -> str:
        """Stable entity id closes business-write -> idempotency-save crash window."""
        if not key:
            return uuid.uuid4().hex
        return self.identity(key, scope)[:32]

    @staticmethod
    def entry_matches(entry: dict, scope: dict | None) -> bool:
        if scope is None:
            return True
        return (
            str(entry.get('kind') or '') == str(scope.get('kind') or '')
            and str(entry.get('actor_id') or '') == str(scope.get('actor_id') or '')
            and str(entry.get('entity_id') or '') == str(scope.get('entity_id') or '')
            and str(entry.get('payload_hash') or '') == str(scope.get('payload_hash') or '')
        )

    def assert_same_request(self, entry: dict, scope: dict | None) -> None:
        if not self.entry_matches(entry, scope):
            raise HTTPException(409, 'Idempotency-Key уже использован для другого запроса')

    def prune(self, store: dict, now: float) -> None:
        stale = [
            key for key, value in store.items()
            if now - float(value.get('updated_at') or value.get('created_at') or 0)
            > self.deps.ttl_seconds
        ]
        for key in stale:
            store.pop(key, None)
            self.cache.pop(key, None)

    def get(self, key: str, scope: dict | None = None):
        if not key:
            return None
        identity = self.identity(key, scope)
        now = self.deps.now()
        cached = self.cache.get(identity)
        if (
            cached
            and now - float(cached.get('updated_at') or cached.get('created_at') or 0)
            <= self.deps.ttl_seconds
        ):
            self.assert_same_request(cached, scope)
            if cached.get('state') == 'done':
                return copy.deepcopy(cached.get('response'))
            raise HTTPException(409, 'Запрос уже выполняется, повторите чуть позже')

        result = {'found': False, 'response': None, 'pending': False}

        def mutator(store: dict):
            self.prune(store, now)
            entry = store.get(identity)
            if not entry:
                return
            self.assert_same_request(entry, scope)
            self.cache[identity] = copy.deepcopy(entry)
            result['found'] = True
            if entry.get('state') == 'done':
                result['response'] = copy.deepcopy(entry.get('response'))
            else:
                result['pending'] = True

        self.deps.update_json_transaction(self.deps.file_path(), {}, mutator)
        if result['pending']:
            raise HTTPException(409, 'Запрос уже выполняется, повторите чуть позже')
        return result['response'] if result['found'] else None

    def claim(self, key: str, scope: dict | None = None):
        if not key:
            return None
        identity = self.identity(key, scope)
        now = self.deps.now()
        result = {'response': None}

        def mutator(store: dict):
            self.prune(store, now)
            existing = store.get(identity)
            if existing:
                self.assert_same_request(existing, scope)
                if existing.get('state') == 'done':
                    self.cache[identity] = copy.deepcopy(existing)
                    result['response'] = copy.deepcopy(existing.get('response'))
                    return
                # A pending claim may be a live request or a crash residue.  The
                # deterministic business entity id + check-in lock resolves that
                # ambiguity without a timer heuristic: let the caller continue.
            entry = {
                'state': 'pending',
                'kind': str((scope or {}).get('kind') or 'legacy'),
                'actor_id': str((scope or {}).get('actor_id') or ''),
                'entity_id': str((scope or {}).get('entity_id') or ''),
                'payload_hash': str((scope or {}).get('payload_hash') or ''),
                'created_at': (existing or {}).get('created_at', now),
                'updated_at': now,
            }
            store[identity] = entry
            self.cache[identity] = copy.deepcopy(entry)

        with self.lock:
            self.deps.update_json_transaction(self.deps.file_path(), {}, mutator)
        return result['response']

    def save(self, key: str, response: dict, scope: dict | None = None) -> None:
        if not key:
            return
        identity = self.identity(key, scope)
        now = self.deps.now()
        entry = {
            'state': 'done',
            'kind': str((scope or {}).get('kind') or 'legacy'),
            'actor_id': str((scope or {}).get('actor_id') or ''),
            'entity_id': str((scope or {}).get('entity_id') or ''),
            'payload_hash': str((scope or {}).get('payload_hash') or ''),
            'response': copy.deepcopy(response),
            'created_at': now,
            'updated_at': now,
        }

        def mutator(store: dict):
            self.prune(store, now)
            existing = store.get(identity)
            if existing:
                self.assert_same_request(existing, scope)
                entry['created_at'] = existing.get('created_at') or now
            store[identity] = entry

        with self.lock:
            self.deps.update_json_transaction(self.deps.file_path(), {}, mutator)
        self.cache[identity] = copy.deepcopy(entry)

    def release(self, key: str, scope: dict | None = None) -> None:
        if not key:
            return
        identity = self.identity(key, scope)

        def mutator(store: dict):
            entry = store.get(identity)
            if entry and self.entry_matches(entry, scope) and entry.get('state') != 'done':
                store.pop(identity, None)

        with self.lock:
            self.deps.update_json_transaction(self.deps.file_path(), {}, mutator)
        self.cache.pop(identity, None)


@dataclass(frozen=True)
class FinishOutboxDeps:
    file_path: Callable[[], str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    checkin_lock: Any
    load_checkin_meta: Callable[[], list]
    save_checkin_meta: Callable[[list], None]
    apply_daily_execution: Callable[..., Any]
    execution_validation_error: type[Exception]
    now: Callable[[], float]
    # These indirections preserve the legacy patch seam while main.py is still
    # the composition root. They resolve to this runtime's aliases normally.
    outbox_load: Callable[[], dict]
    outbox_write_pending: Callable[..., None]
    outbox_mark_applied: Callable[[str], None]
    outbox_mark_failed: Callable[[str, str], None]
    outbox_mark_dead_letter: Callable[[str, str], None]
    clear_pending_execution_report: Callable[[str], None]
    retry_pending_events: Callable[[], int]


class FinishOutboxRuntime:
    """Durable session->DailyExecution projector with retry and reconciliation."""

    MAX_ATTEMPTS = 10
    RETRY_SWEEP_INTERVAL_SECONDS = 60

    def __init__(self, deps: FinishOutboxDeps):
        self.deps = deps
        self.lock = threading.Lock()
        self.sweep_in_progress = False

    def load(self) -> dict:
        return self.deps.safe_load_json(self.deps.file_path(), {})

    def save(self, outbox: dict) -> None:
        self.deps.atomic_write_json(self.deps.file_path(), outbox)

    def write_pending(
        self,
        session_id: str,
        plan_id: str,
        plan_version: int,
        worker_id: str,
        date_str: str,
        object_id: str,
        item_results: list,
        acceptance_id: str | None = None,
    ) -> None:
        with self.lock:
            outbox = self.load()
            outbox[session_id] = {
                'state': 'pending',
                'plan_id': plan_id,
                'plan_version': plan_version,
                'worker_id': str(worker_id),
                'date': date_str,
                'object_id': object_id,
                'item_results': item_results,
                'acceptance_id': acceptance_id,
                'created_at': self.deps.now(),
                'last_attempt_at': None,
                'attempt_count': 0,
                'error': None,
            }
            self.save(outbox)

    def mark_applied(self, session_id: str) -> None:
        with self.lock:
            outbox = self.load()
            if session_id in outbox:
                outbox[session_id]['state'] = 'applied'
                outbox[session_id]['applied_at'] = self.deps.now()
                self.save(outbox)

    def mark_failed(self, session_id: str, error: str) -> None:
        with self.lock:
            outbox = self.load()
            if session_id in outbox:
                event = outbox[session_id]
                event['attempt_count'] = event.get('attempt_count', 0) + 1
                event['error'] = error[:500]
                event['last_attempt_at'] = self.deps.now()
                event['state'] = (
                    'dead_letter'
                    if event['attempt_count'] >= self.MAX_ATTEMPTS
                    else 'retrying'
                )
                self.save(outbox)

    def mark_dead_letter(self, session_id: str, error: str) -> None:
        with self.lock:
            outbox = self.load()
            if session_id in outbox:
                event = outbox[session_id]
                event['attempt_count'] = event.get('attempt_count', 0) + 1
                event['error'] = error[:500]
                event['last_attempt_at'] = self.deps.now()
                event['state'] = 'dead_letter'
                self.save(outbox)

    def retry_pending(self) -> int:
        with self.lock:
            outbox = self.deps.outbox_load()
        retried = 0
        for session_id, event in list(outbox.items()):
            if event.get('state') not in ('pending', 'retrying'):
                continue
            try:
                self.deps.apply_daily_execution(
                    session_id=session_id,
                    daily_plan_id=event['plan_id'],
                    plan_version=int(event.get('plan_version') or 0),
                    worker_id=str(event['worker_id']),
                    date_str=event['date'],
                    object_id=event['object_id'],
                    item_results=event.get('item_results') or [],
                    acceptance_id=event.get('acceptance_id'),
                )
                self.deps.outbox_mark_applied(session_id)
                self.deps.clear_pending_execution_report(session_id)
                retried += 1
            except self.deps.execution_validation_error as exc:
                self.deps.outbox_mark_dead_letter(
                    session_id, f'validation rejected: {exc}'
                )
            except Exception as exc:
                self.deps.outbox_mark_failed(session_id, str(exc))
        return retried

    async def run_retry_sweep_once(self) -> int | None:
        if self.sweep_in_progress:
            return None
        self.sweep_in_progress = True
        try:
            return await asyncio.to_thread(self.deps.retry_pending_events)
        finally:
            self.sweep_in_progress = False

    async def retry_sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.RETRY_SWEEP_INTERVAL_SECONDS)
                retried = await self.run_retry_sweep_once()
                if retried:
                    print(f'[outbox-sweep] Retried {retried} pending finish-outbox event(s)')
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f'[outbox-sweep] tick failed (will retry next interval): {exc}')

    def dead_letter_count(self) -> int:
        outbox = self.deps.outbox_load()
        return sum(
            1 for event in outbox.values()
            if event.get('state') == 'dead_letter'
        )

    def clear_pending_execution_report(self, session_id: str) -> None:
        with self.deps.checkin_lock:
            items = self.deps.load_checkin_meta()
            session = next((i for i in items if i.get('id') == session_id), None)
            if session and session.get('pending_execution_report'):
                session['pending_execution_report'] = None
                self.deps.save_checkin_meta(items)

    def reject_pending_execution_report(self, session_id: str, reason: str) -> None:
        with self.deps.checkin_lock:
            items = self.deps.load_checkin_meta()
            session = next((i for i in items if i.get('id') == session_id), None)
            if session and session.get('pending_execution_report'):
                session['rejected_execution_report'] = {
                    'raw': session['pending_execution_report'],
                    'reason': reason[:500],
                    'rejected_at': self.deps.now(),
                }
                session['pending_execution_report'] = None
                self.deps.save_checkin_meta(items)

    def reconcile_missing_events(self) -> int:
        items = self.deps.load_checkin_meta()
        outbox = self.deps.outbox_load()
        reconciled = 0
        for session in items:
            if session.get('finish_at') is None:
                continue
            report_raw = session.get('pending_execution_report')
            if not report_raw:
                continue
            session_id = session.get('id')
            if session_id in outbox:
                continue
            try:
                report = json.loads(report_raw)
            except Exception as exc:
                print(
                    f'WARNING: reconcile — session {session_id} has unparseable '
                    f'pending_execution_report: {exc}'
                )
                continue
            plan_id = report.get('plan_id', '') or session.get('daily_plan_id', '')
            plan_version = int(
                report.get('plan_version', 0)
                or session.get('daily_plan_version', 0)
                or 0
            )
            item_results = report.get('item_results') or []
            if not (plan_id and isinstance(item_results, list) and item_results):
                continue
            acceptance_id = session.get('daily_plan_acceptance_id') or None
            self.deps.outbox_write_pending(
                session_id=session_id,
                plan_id=plan_id,
                plan_version=plan_version,
                worker_id=str(session['user_id']),
                date_str=session['date'],
                object_id=session['object_id'],
                item_results=item_results,
                acceptance_id=acceptance_id,
            )
            reconciled += 1
            print(
                f'[startup] Reconciled missing outbox event for session '
                f'{session_id} (crash-window recovery)'
            )
        return reconciled
