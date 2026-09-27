"""Static contract for the 2026-09-27 fix to frontend/js/critical-alerts.js:
owner reported the critical popup appearing 3x again. Two concrete gaps
found by reading the poll/queue/show code:

1. `_pollCriticalAlerts()` built `existingIds` ONCE before its push loop, so
   if a single /api/critical-alerts/pending response ever contained the same
   alert id twice, both copies passed the "not already queued" check and got
   queued -- the set was never updated as items were pushed.
2. `_showNextCriticalAlert()` didn't itself enforce "never show while a
   modal is already open" -- it relied entirely on callers checking first.

This does not touch the backend's existing persisted (kind, target_user_id,
ref_id) dedup, per instruction not to add another blind title/kind dedup.
"""
import os

JS_PATH = os.path.join(os.path.dirname(__file__), '..', 'frontend', 'js', 'critical-alerts.js')


def _read():
    with open(JS_PATH, encoding='utf-8') as f:
        return f.read()


class TestPollLoopDedupsWithinOneResponse:
    def test_existing_ids_set_is_updated_inside_the_push_loop(self):
        src = _read()
        # The bug: existingIds computed once, then only read (never .add())
        # inside the for-loop. The fix must both read AND write it per
        # iteration so a same-response duplicate id is caught immediately.
        loop_start = src.index('for (const alert of fresh)')
        loop_body = src[loop_start:loop_start + 300]
        assert 'existingIds.add(alert.id)' in loop_body, (
            "existingIds must be updated inside the loop, not just checked, "
            "or a duplicate id within one poll response is queued twice"
        )

    def test_push_only_happens_after_the_dedup_check(self):
        src = _read()
        loop_start = src.index('for (const alert of fresh)')
        loop_body = src[loop_start:loop_start + 300]
        add_idx = loop_body.index('existingIds.add(alert.id)')
        push_idx = loop_body.index('_criticalAlertQueue.push(alert)')
        assert add_idx < push_idx


class TestShowNextEnforcesInvariantItself:
    def test_show_next_checks_modal_open_before_finding_next_alert(self):
        src = _read()
        fn_start = src.index('function _showNextCriticalAlert()')
        fn_end = src.index('\n}', fn_start)
        fn_body = src[fn_start:fn_end]
        guard_idx = fn_body.index('if (_criticalAlertModalOpen) return;')
        find_idx = fn_body.index('_criticalAlertQueue.find(')
        assert guard_idx < find_idx, (
            "_showNextCriticalAlert must refuse to show anything while a "
            "modal is already open, not just rely on callers checking first"
        )

    def test_show_next_also_excludes_locally_acked_ids(self):
        src = _read()
        fn_start = src.index('function _showNextCriticalAlert()')
        fn_end = src.index('\n}', fn_start)
        fn_body = src[fn_start:fn_end]
        assert '_criticalAlertAckedIds.has(a.id)' in fn_body


class TestBackendDedupUntouched:
    def test_no_new_title_or_kind_based_dedup_introduced(self):
        """Instruction: do not add another blind dedup keyed only on title
        or kind -- confirms this fix stayed id-based, matching the existing
        backend (kind, target_user_id, ref_id) semantic dedup."""
        src = _read()
        assert 'a.title ===' not in src
        assert 'alert.title ===' not in src


if __name__ == '__main__':
    import pytest
    raise SystemExit(pytest.main([__file__, '-q']))
