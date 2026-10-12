"""22.09/23.09 — real bug found in the owner's live iPhone Telegram regression
pass: the critical-alert popup reappeared after tapping "Принял". Root cause:
ack_critical_alert() and resolve_critical_alert() both used the forbidden
read-modify-write pattern storage.py's own _atomic_write_json docstring
explicitly warns about --
    items = _load_critical_alerts()   # read OUTSIDE any lock
    ... mutate items in place ...
    _save_critical_alerts(items)      # lock only covers this write

storage.py already names the fix for this exact pattern: update_json_transaction()
does the read, the mutation, and the write under ONE lock acquisition. Every
other critical_alerts.json writer (_create_critical_alert) already used it --
ack/resolve did not, and could silently lose a concurrent write (or have
their own write lost) if another mutation of the same file happened between
their read and their write.

This file proves the fix holds under actual concurrent execution, not just
by reading the source -- it runs ack_critical_alert() and a competing
_create_critical_alert() append from two real threads hammering the same
file, and asserts neither write is ever lost. It also covers the 23.09
owner-review addition: acknowledging a legacy bare-uid birthday alert must
transactionally supersede every other unresolved legacy sibling sharing the
same (kind, target_user_id, ref_id) key, scoped ONLY to that legacy shape --
never new idem-keyed birthday alerts or any other kind.
"""
import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
import core.permissions as permissions  # noqa: E402
from fastapi import HTTPException  # noqa: E402

OWNER_ID = '1'
WORKER_ID = '555'


class _Body:
    def __init__(self, comment=''):
        self.comment = comment


class CriticalAlertAckRaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='critical-alert-ack-race-')
        self._orig_file = backend.CRITICAL_ALERTS_FILE
        backend.CRITICAL_ALERTS_FILE = os.path.join(self.tmp, 'critical_alerts.json')
        self._patchers = [
            patch.object(backend, '_load_roles', return_value={OWNER_ID: 'owner', WORKER_ID: 'worker'}),
            patch.object(backend, 'send_telegram_message'),
            patch.object(backend, '_ensure_critical_alert_chat'),
            patch.object(backend, '_chat_thread_id', return_value='group'),
            patch.object(backend, '_load_chat', return_value=[]),
            patch.object(backend, '_save_chat'),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        backend.CRITICAL_ALERTS_FILE = self._orig_file
        for p in self._patchers:
            p.stop()

    def test_ack_uses_update_json_transaction_not_a_bare_load_then_save(self):
        # Source-level guard: the old pattern must not come back even if a
        # future edit "simplifies" this endpoint again. Checks for an actual
        # call, not just the function name appearing anywhere (e.g. in a
        # comment referencing the old pattern by name).
        import inspect
        src = inspect.getsource(backend.ack_critical_alert)
        assert 'deps.update_json_transaction(' in src
        assert 'deps.critical_alerts_file()' in src
        assert '_save_critical_alerts(items)' not in src

    def test_resolve_uses_update_json_transaction_not_a_bare_load_then_save(self):
        import inspect
        src = inspect.getsource(backend.resolve_critical_alert)
        assert 'deps.update_json_transaction(' in src
        assert 'deps.critical_alerts_file()' in src
        assert '_save_critical_alerts(items)' not in src

    def test_ack_survives_a_concurrent_create_without_losing_either_write(self):
        alert = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',
        )
        user = {'id': int(OWNER_ID)}

        barrier = threading.Barrier(2)
        errors = []

        def do_ack():
            try:
                barrier.wait(timeout=5)
                backend.ack_critical_alert(alert['id'], _Body(comment=''), user=user)
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        def do_create_other():
            try:
                barrier.wait(timeout=5)
                backend._create_critical_alert(
                    target_user_id=OWNER_ID, kind='plan_publish_reminder',
                    title='unrelated concurrent alert', ref_id='2026-09-23',
                )
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=do_ack), threading.Thread(target=do_create_other)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(errors, [])

        stored = backend._load_critical_alerts()
        self.assertEqual(len(stored), 2, "both the ack'd alert and the concurrently-created one must survive")

        acked = next(a for a in stored if a['id'] == alert['id'])
        self.assertIsNotNone(acked['acknowledged_at'], "the ack must not be silently lost by a concurrent write")

        other = next(a for a in stored if a['id'] != alert['id'])
        self.assertEqual(other['kind'], 'plan_publish_reminder')

    def test_ack_of_nonexistent_alert_raises_without_writing_a_corrupt_file(self):
        with self.assertRaises(HTTPException):
            backend.ack_critical_alert('does-not-exist', _Body(comment=''), user={'id': int(OWNER_ID)})
        # The transaction must not have written anything on the exception path.
        self.assertEqual(backend._load_critical_alerts(), [])


class LegacyBirthdaySiblingSupersedeTests(unittest.TestCase):
    """23.09 owner review: ACKing a legacy bare-uid birthday alert must
    transactionally supersede unresolved siblings sharing its semantic key."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='critical-alert-legacy-supersede-')
        self._orig_file = backend.CRITICAL_ALERTS_FILE
        backend.CRITICAL_ALERTS_FILE = os.path.join(self.tmp, 'critical_alerts.json')
        self._patchers = [
            patch.object(backend, '_load_roles', return_value={OWNER_ID: 'owner', WORKER_ID: 'worker'}),
            patch.object(backend, '_chat_thread_id', return_value='group'),
            patch.object(backend, '_load_chat', return_value=[]),
            patch.object(backend, '_save_chat'),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        backend.CRITICAL_ALERTS_FILE = self._orig_file
        for p in self._patchers:
            p.stop()

    def _seed(self, alerts):
        backend._save_critical_alerts(alerts)

    def _legacy_birthday(self, id_, ref_id=WORKER_ID, created_at=100, acknowledged_at=None):
        return {
            'id': id_, 'target_user_id': OWNER_ID, 'kind': 'birthday', 'title': 't',
            'subtitle': '', 'ref_id': ref_id, 'created_at': created_at,
            'deadline_at': None, 'acknowledged_at': acknowledged_at, 'comment': None,
            'resolution': None, 'resolution_note': None, 'resolution_photos': [],
        }

    def test_acking_a_legacy_sibling_supersedes_the_other_unresolved_legacy_siblings(self):
        self._seed([
            self._legacy_birthday('a1', created_at=100),
            self._legacy_birthday('a2', created_at=200),
            self._legacy_birthday('a3', created_at=300),
        ])
        backend.ack_critical_alert('a2', _Body(comment=''), user={'id': int(OWNER_ID)})

        stored = {a['id']: a for a in backend._load_critical_alerts()}
        self.assertIsNotNone(stored['a2']['acknowledged_at'])
        self.assertNotIn('superseded_by', stored['a2'])

        self.assertIsNotNone(stored['a1']['acknowledged_at'])
        self.assertEqual(stored['a1']['superseded_by'], 'a2')
        self.assertIsNotNone(stored['a3']['acknowledged_at'])
        self.assertEqual(stored['a3']['superseded_by'], 'a2')

    def test_new_idem_shaped_birthday_alerts_are_never_superseded_by_a_legacy_ack(self):
        idem_ref = f'birthday:{WORKER_ID}:2027:today'
        self._seed([
            self._legacy_birthday('legacy1', ref_id=WORKER_ID, created_at=100),
            {**self._legacy_birthday('new1', ref_id=idem_ref, created_at=500)},
        ])
        backend.ack_critical_alert('legacy1', _Body(comment=''), user={'id': int(OWNER_ID)})

        stored = {a['id']: a for a in backend._load_critical_alerts()}
        self.assertIsNotNone(stored['legacy1']['acknowledged_at'])
        # The new-shaped alert must be completely untouched.
        self.assertIsNone(stored['new1']['acknowledged_at'])
        self.assertNotIn('superseded_by', stored['new1'])

    def test_acking_a_non_birthday_alert_never_triggers_supersede_logic(self):
        self._seed([
            {**self._legacy_birthday('a1', created_at=100), 'kind': 'plan_overdue'},
            {**self._legacy_birthday('a2', created_at=200), 'kind': 'plan_overdue'},
        ])
        backend.ack_critical_alert('a1', _Body(comment=''), user={'id': int(OWNER_ID)})
        stored = {a['id']: a for a in backend._load_critical_alerts()}
        self.assertIsNotNone(stored['a1']['acknowledged_at'])
        # a2 shares kind/target/ref_id but is NOT a legacy birthday shape --
        # must not be touched by supersede logic (only real dedup at create-time covers this kind).
        self.assertIsNone(stored['a2']['acknowledged_at'])

    def test_already_acknowledged_siblings_are_never_re_touched(self):
        self._seed([
            self._legacy_birthday('a1', created_at=100, acknowledged_at=999),
            self._legacy_birthday('a2', created_at=200),
        ])
        original_a1 = dict(backend._load_critical_alerts()[0])
        backend.ack_critical_alert('a2', _Body(comment=''), user={'id': int(OWNER_ID)})
        stored = {a['id']: a for a in backend._load_critical_alerts()}
        self.assertEqual(stored['a1'], original_a1)


if __name__ == '__main__':
    unittest.main()
