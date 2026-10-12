"""22.09 iPhone screenshot audit: _create_critical_alert() used to unconditionally
append a new alert with a fresh UUID on EVERY call, regardless of whether an
identical unresolved alert already existed for the same user/kind/ref_id. A real
device confirmed this produces stacked, near-identical critical-alert popups (the
red-circle "Принял" modal, critical-alerts.js) showing back-to-back after a single
underlying event.

Dedup key: (kind, target_user_id, ref_id). An unacknowledged alert matching that
triple is returned as-is instead of creating a duplicate. Acknowledging one
allows a new alert with the same key to be created afterward (a genuinely new
occurrence of the same situation, e.g. tomorrow's plan-overdue reminder).
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
import core.permissions as permissions  # noqa: E402

OWNER_ID = '1'
WORKER_ID = '555'


class CriticalAlertDedupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='critical-alert-dedup-')
        self._orig_file = backend.CRITICAL_ALERTS_FILE
        backend.CRITICAL_ALERTS_FILE = os.path.join(self.tmp, 'critical_alerts.json')
        self._patchers = [
            patch.object(backend, '_load_roles', return_value={OWNER_ID: 'owner', WORKER_ID: 'worker'}),
            patch.object(backend, 'send_telegram_message'),
            patch.object(backend, '_ensure_critical_alert_chat'),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        backend.CRITICAL_ALERTS_FILE = self._orig_file
        for p in self._patchers:
            p.stop()

    def _stored(self):
        return backend._load_critical_alerts()

    def test_same_kind_user_ref_id_called_three_times_creates_one_alert(self):
        for _ in range(3):
            backend._create_critical_alert(
                target_user_id=OWNER_ID, kind='plan_overdue',
                title='План на 2026-09-22 не опубликован для 1 работника',
                ref_id='2026-09-22',
            )
        alerts = self._stored()
        self.assertEqual(len(alerts), 1)

    def test_returns_the_existing_unresolved_alert_not_a_new_id(self):
        first = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',
        )
        second = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t (different title, same event)', ref_id='2026-09-22',
        )
        self.assertEqual(first['id'], second['id'])

    def test_different_ref_id_creates_a_separate_alert(self):
        # A genuinely different business date is a different alert, not a dup.
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t1', ref_id='2026-09-22',
        )
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t2', ref_id='2026-09-23',
        )
        alerts = self._stored()
        self.assertEqual(len(alerts), 2)

    def test_different_kind_same_ref_id_creates_a_separate_alert(self):
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t1', ref_id='X',
        )
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_publish_reminder', title='t2', ref_id='X',
        )
        alerts = self._stored()
        self.assertEqual(len(alerts), 2)

    def test_different_target_user_same_kind_ref_id_creates_a_separate_alert(self):
        backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='birthday', title='t1', ref_id=WORKER_ID,
        )
        backend._create_critical_alert(
            target_user_id=WORKER_ID, kind='birthday', title='t2', ref_id=WORKER_ID,
        )
        alerts = self._stored()
        self.assertEqual(len(alerts), 2)

    def test_blank_ref_id_never_dedupes_two_unrelated_manual_alerts(self):
        # 23.09 (owner review): ref_id='' is the default for any caller that
        # has no natural reference id (e.g. a manually-created owner alert).
        # Two genuinely unrelated alerts sharing kind/target_user_id but both
        # with blank ref_id must NOT collapse into one -- dedup only applies
        # when ref_id is non-empty.
        first = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='manual', title='Проверить объект А',
        )
        second = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='manual', title='Проверить объект Б',
        )
        self.assertNotEqual(first['id'], second['id'])
        alerts = self._stored()
        self.assertEqual(len(alerts), 2)

    def test_after_acknowledgement_a_new_alert_with_the_same_key_can_be_created(self):
        first = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t1', ref_id='2026-09-22',
        )
        items = self._stored()
        for a in items:
            if a['id'] == first['id']:
                a['acknowledged_at'] = 1234567890
        backend._save_critical_alerts(items)

        second = backend._create_critical_alert(
            target_user_id=OWNER_ID, kind='plan_overdue', title='t2 (new occurrence)', ref_id='2026-09-22',
        )
        self.assertNotEqual(first['id'], second['id'])
        alerts = self._stored()
        self.assertEqual(len(alerts), 2)

    def test_dedup_hit_does_not_resend_chat_thread_or_push(self):
        # The chat-thread/telegram-push side effects must only fire on a
        # genuine first create, not on every dedup-returned call -- otherwise
        # the same "duplicate popup" problem just moves to duplicate chat
        # messages/push notifications instead.
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_save_chat') as mock_chat_save, \
             patch.object(backend, 'send_telegram_message') as mock_push:
            backend._create_critical_alert(
                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',
            )
            backend._create_critical_alert(
                target_user_id=OWNER_ID, kind='plan_overdue', title='t', ref_id='2026-09-22',
            )
        self.assertEqual(mock_chat_save.call_count, 1)
        self.assertEqual(mock_push.call_count, 1)


if __name__ == '__main__':
    unittest.main()
