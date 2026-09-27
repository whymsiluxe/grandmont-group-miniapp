"""Regression tests for the Finish-outbox periodic retry liveness fix
(Issue 2, fix/miniapp-profile-rmw-outbox-retry).

Before this fix, `_retry_pending_outbox_events()` (backend/main.py) was only
ever called once, at FastAPI startup (`_on_startup`). A transient failure
(e.g. a momentary lock contention or a passing I/O error) occurring AFTER
startup left the event stuck in state "retrying" until the next process
restart -- on a stable long-running service that could be days, even though
the durable state machine (pending -> retrying -> applied / dead_letter) was
already designed to support automatic retry.

The fix adds a small periodic asyncio background sweep
(`_outbox_retry_sweep_loop` / `_run_outbox_retry_sweep_once`), started from
`_on_startup` and cancelled from `_on_shutdown`, that calls the EXACT SAME
`_retry_pending_outbox_events()` used at startup -- no reimplementation of
the state machine, no new queue framework.

Uses the same plain-function-call + isolated-tmp-file pattern as
tests/test_json_transaction.py and tests/test_ui_fix_round_tofu_names.py:
reassign backend.FINISH_OUTBOX_FILE to a per-test tmp file (save/restore in
setUp/tearDown so it never leaks into other tests in the same pytest
process), call backend's plain functions directly (no HTTP test client
needed), and use backend.dpl (already configured with an isolated DATA_ROOT
by tests/conftest.py) to create a real plan/acceptance so
apply_daily_execution's validation has real data to check against.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402


def _make_item(idx=1) -> dict:
    return {
        "id": f"item-{idx}",
        "sequence": idx,
        "title": f"Шпаклевание зона {idx}",
        "objective": "Нанести шпаклёвку",
        "planned_quantity": 10.0,
        "unit": "м²",
        "time_estimate_hours": 2.0,
        "work_type_id": "filling_q1_q4",
        "required_tools": [],
        "required_materials": [],
    }


class OutboxRetryLivenessTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix='grandmont-group-test-outbox-liveness-')
        self._saved_attrs = {
            name: getattr(backend, name) for name in ('FINISH_OUTBOX_FILE',)
        }
        backend.FINISH_OUTBOX_FILE = os.path.join(self._tmp, 'finish_outbox.json')
        # Reset the module-level sweep-in-progress flag so a prior test's state
        # (or lack of _on_startup having run in this process) never leaks in.
        backend._outbox_sweep_in_progress = False

    def tearDown(self):
        for name, value in self._saved_attrs.items():
            setattr(backend, name, value)
        backend._outbox_sweep_in_progress = False

    def _make_accepted_plan(self, worker_id='42', items=None):
        """Real plan + acceptance via backend.dpl (already configured with an
        isolated DATA_ROOT by conftest.py) so apply_daily_execution's
        validate_execution_against_acceptance() has real data to check
        against -- same pattern as test_p0_integrity_fixes.py."""
        dpl = backend.dpl
        plan = dpl.create_plan(
            object_id='OBJ-1', stage_key='OBJ-1-S1', date_str='2026-09-10',
            assigned_worker_ids=[worker_id], items=items or [_make_item()],
            created_by='owner',
        )
        dpl.publish_plan(plan['id'], 'owner')
        acceptance = dpl.accept_plan(plan['id'], 1, worker_id)
        return plan, acceptance

    def _write_pending(self, session_id, plan, acceptance, worker_id='42', item_results=None):
        backend._outbox_write_pending(
            session_id=session_id,
            plan_id=plan['id'],
            plan_version=acceptance['plan_version'],
            worker_id=worker_id,
            date_str=plan['date'],
            object_id=plan['object_id'],
            item_results=item_results if item_results is not None else [
                {"item_id": "item-1", "status": "done", "actual_quantity": 10.0}
            ],
            acceptance_id=acceptance['id'],
        )

    def _read_outbox(self):
        with open(backend.FINISH_OUTBOX_FILE, encoding='utf-8') as f:
            return json.load(f)

    # ---------- A: pending/retrying event becomes applied without a restart ----------

    def test_pending_event_becomes_applied_via_sweep_without_restart(self):
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-a-pending-to-applied'
        self._write_pending(session_id, plan, acceptance)

        self.assertEqual(self._read_outbox()[session_id]['state'], 'pending')

        # This is the runtime sweep, NOT _on_startup -- proves the event does not
        # need a process restart to get applied.
        retried = asyncio.run(backend._run_outbox_retry_sweep_once())

        self.assertEqual(retried, 1)
        outbox = self._read_outbox()
        self.assertEqual(outbox[session_id]['state'], 'applied')
        execution = backend.dpl._load_store()['executions'].get(session_id)
        self.assertIsNotNone(execution, 'apply_daily_execution must have actually run')

    def test_retrying_event_also_picked_up_by_sweep(self):
        """The state machine has THREE retryable-adjacent states in play here:
        pending and retrying are both retried; dead_letter is not (see test C).
        This proves 'retrying' (not just fresh 'pending') is picked up too."""
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-a2-retrying-to-applied'
        self._write_pending(session_id, plan, acceptance)
        backend._outbox_mark_failed(session_id, 'simulated transient error')
        self.assertEqual(self._read_outbox()[session_id]['state'], 'retrying')

        retried = asyncio.run(backend._run_outbox_retry_sweep_once())

        self.assertEqual(retried, 1)
        self.assertEqual(self._read_outbox()[session_id]['state'], 'applied')

    # ---------- B: transient failure remains retryable ----------

    def test_transient_failure_stays_retrying_not_dead_letter(self):
        """A transient failure (apply_daily_execution raising a plain Exception,
        not ExecutionValidationError) must leave the event in 'retrying' with
        the attempt recorded -- eligible for the NEXT sweep tick, not terminal."""
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-b-transient'
        self._write_pending(session_id, plan, acceptance)

        original_apply = backend.dpl.apply_daily_execution
        def _boom(*a, **kw):
            raise RuntimeError('simulated transient I/O failure')
        backend.dpl.apply_daily_execution = _boom
        try:
            retried = asyncio.run(backend._run_outbox_retry_sweep_once())
        finally:
            backend.dpl.apply_daily_execution = original_apply

        self.assertEqual(retried, 0)
        evt = self._read_outbox()[session_id]
        self.assertEqual(evt['state'], 'retrying')
        self.assertEqual(evt['attempt_count'], 1)
        self.assertIn('simulated transient I/O failure', evt['error'])

        # A SECOND sweep (simulating the next periodic tick) with the real
        # function restored must now succeed -- proves the event truly stayed
        # retryable, not silently wedged.
        retried_2 = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(retried_2, 1)
        self.assertEqual(self._read_outbox()[session_id]['state'], 'applied')

    # ---------- C: validation rejection -> dead_letter, excluded from periodic retry ----------

    def test_validation_rejection_becomes_dead_letter_and_is_not_retried(self):
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-c-invalid'
        # item_id 'item-does-not-exist' is not in the accepted version's items ->
        # validate_execution_against_acceptance raises ExecutionValidationError.
        self._write_pending(session_id, plan, acceptance, item_results=[
            {"item_id": "item-does-not-exist", "status": "done", "actual_quantity": 1.0}
        ])

        retried = asyncio.run(backend._run_outbox_retry_sweep_once())

        self.assertEqual(retried, 0)
        evt = self._read_outbox()[session_id]
        self.assertEqual(evt['state'], 'dead_letter')
        self.assertIn('validation rejected', evt['error'])

        # A further sweep must NOT touch it again (attempt_count must not grow,
        # apply_daily_execution must not be invoked for it) -- dead_letter is
        # excluded from _retry_pending_outbox_events' own state filter.
        attempt_count_before = evt['attempt_count']
        retried_again = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(retried_again, 0)
        evt_after = self._read_outbox()[session_id]
        self.assertEqual(evt_after['state'], 'dead_letter')
        self.assertEqual(evt_after['attempt_count'], attempt_count_before,
            'dead_letter events must be excluded from automatic periodic retry')

    # ---------- D: retry does not duplicate / OUTBOX_MAX_ATTEMPTS respected ----------

    def test_applied_event_not_reapplied_on_later_sweep(self):
        """apply_daily_execution is idempotent by session_id, and the outbox
        state machine must not even attempt an already-applied event again --
        proves no duplicate execution record and no wasted retry attempt."""
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-d-no-duplicate'
        self._write_pending(session_id, plan, acceptance)

        first = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(first, 1)
        execution_after_first = backend.dpl._load_store()['executions'][session_id]

        second = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(second, 0, 'an already-applied event must not be retried again')
        execution_after_second = backend.dpl._load_store()['executions'][session_id]
        self.assertEqual(execution_after_first, execution_after_second,
            'the execution record must not change/duplicate on a later sweep')

    def test_outbox_max_attempts_still_respected_by_periodic_sweep(self):
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-d2-max-attempts'
        self._write_pending(session_id, plan, acceptance)

        original_apply = backend.dpl.apply_daily_execution
        def _always_fails(*a, **kw):
            raise RuntimeError('persistent transient failure')
        backend.dpl.apply_daily_execution = _always_fails
        try:
            for _ in range(backend.OUTBOX_MAX_ATTEMPTS):
                asyncio.run(backend._run_outbox_retry_sweep_once())
        finally:
            backend.dpl.apply_daily_execution = original_apply

        evt = self._read_outbox()[session_id]
        self.assertEqual(evt['attempt_count'], backend.OUTBOX_MAX_ATTEMPTS)
        self.assertEqual(evt['state'], 'dead_letter',
            'OUTBOX_MAX_ATTEMPTS exhausted must terminate into dead_letter, '
            'never retry forever')

        # Further sweeps (now with a working apply_daily_execution) must not
        # touch it -- it is terminal.
        retried_after_exhaustion = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(retried_after_exhaustion, 0)
        self.assertEqual(self._read_outbox()[session_id]['attempt_count'],
                          backend.OUTBOX_MAX_ATTEMPTS)

    # ---------- E: overlapping sweeps cannot run concurrently ----------

    def test_overlapping_sweep_ticks_do_not_run_concurrently(self):
        """Simulates two scheduler ticks landing at (almost) the same time --
        the in-progress flag must make the second one skip (return None)
        rather than run a second _retry_pending_outbox_events() concurrently
        with the first."""
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-e-overlap'
        self._write_pending(session_id, plan, acceptance)

        call_count = {'n': 0}
        original_apply = backend.dpl.apply_daily_execution

        # apply_daily_execution runs inside asyncio.to_thread (a separate OS
        # thread), so signalling back to the test driver needs a plain
        # threading.Event, not an asyncio.Event (not thread-safe to .set()
        # from a worker thread).
        import threading
        started_flag = threading.Event()
        release_flag = threading.Event()

        def _blocking_apply(*a, **kw):
            call_count['n'] += 1
            started_flag.set()
            release_flag.wait(timeout=5)
            return original_apply(*a, **kw)

        backend.dpl.apply_daily_execution = _blocking_apply
        try:
            async def _drive():
                first_task = asyncio.create_task(backend._run_outbox_retry_sweep_once())
                # Wait until the first sweep's apply_daily_execution has
                # actually started (i.e. it holds the in-progress flag) before
                # firing the second, overlapping tick.
                await asyncio.to_thread(started_flag.wait, 5)
                second_result = await backend._run_outbox_retry_sweep_once()
                release_flag.set()
                first_result = await first_task
                return first_result, second_result

            first_result, second_result = asyncio.run(_drive())
        finally:
            backend.dpl.apply_daily_execution = original_apply
            release_flag.set()

        self.assertEqual(second_result, None,
            'an overlapping tick must skip (None), not run a second sweep')
        self.assertEqual(first_result, 1)
        self.assertEqual(call_count['n'], 1,
            'apply_daily_execution must be invoked exactly once across both '
            'overlapping ticks -- no concurrent double-processing')
        self.assertFalse(backend._outbox_sweep_in_progress,
            'flag must be reset after the sweep finishes (success path)')

    def test_in_progress_flag_reset_after_sweep_failure(self):
        """The in-progress flag must be released even if the sweep itself
        raises (e.g. a bug, or the outbox file briefly unreadable) -- a single
        failed tick must not permanently wedge every future tick into 'skip'."""
        plan, acceptance = self._make_accepted_plan()
        session_id = 'sess-e2-flag-reset-on-error'
        self._write_pending(session_id, plan, acceptance)

        original_load = backend._outbox_load
        def _boom():
            raise RuntimeError('simulated unexpected sweep failure')
        backend._outbox_load = _boom
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(backend._run_outbox_retry_sweep_once())
        finally:
            backend._outbox_load = original_load

        self.assertFalse(backend._outbox_sweep_in_progress,
            'flag must be reset in a finally block even when the sweep raises')

        # A subsequent sweep must be able to run normally.
        retried = asyncio.run(backend._run_outbox_retry_sweep_once())
        self.assertEqual(retried, 1)


if __name__ == '__main__':
    unittest.main()
