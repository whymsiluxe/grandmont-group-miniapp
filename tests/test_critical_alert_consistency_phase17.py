"""Phase 17 regression tests for critical-alert durable side effects."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from fastapi import HTTPException, UploadFile  # noqa: E402

OWNER_ID = '1'


class _AckBody:
    def __init__(self, comment=''):
        self.comment = comment


def _jpeg_bytes():
    return b'\xff\xd8\xff' + b'x' * 16


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


class CriticalAlertConsistencyPhase17Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='critical-alert-phase17-')
        self._orig_file = backend.CRITICAL_ALERTS_FILE
        self._orig_photo_dir = backend.CRITICAL_ALERT_PHOTO_DIR
        backend.CRITICAL_ALERTS_FILE = os.path.join(self.tmp, 'critical_alerts.json')
        backend.CRITICAL_ALERT_PHOTO_DIR = os.path.join(self.tmp, 'photos')
        os.makedirs(backend.CRITICAL_ALERT_PHOTO_DIR, exist_ok=True)
        self.user = {'id': int(OWNER_ID), 'first_name': 'Owner'}
        self._patchers = [
            patch.object(backend, '_load_roles', return_value={OWNER_ID: 'owner'}),
            patch.object(backend, '_chat_thread_id', return_value='group'),
            patch.object(backend, 'send_telegram_message'),
        ]
        for patcher in self._patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self._patchers:
            patcher.stop()
        backend.CRITICAL_ALERTS_FILE = self._orig_file
        backend.CRITICAL_ALERT_PHOTO_DIR = self._orig_photo_dir

    def _seed_alert(self, **extra):
        alert = {
            'id': 'alert-1',
            'target_user_id': OWNER_ID,
            'kind': 'manual',
            'title': 'Test',
            'subtitle': '',
            'ref_id': 'ref-1',
            'created_at': 1,
            'deadline_at': None,
            'acknowledged_at': None,
            'comment': None,
            'resolution': None,
            'resolution_note': None,
            'resolution_photos': [],
        }
        alert.update(extra)
        backend._save_critical_alerts([alert])
        return alert

    def test_create_chat_failure_does_not_false_500_and_retry_repairs_without_duplicate(self):
        chat = []

        def load_chat():
            return [dict(item) for item in chat]

        calls = {'save': 0}

        def flaky_save(messages):
            calls['save'] += 1
            if calls['save'] == 1:
                raise OSError('chat disk failure')
            chat[:] = [dict(item) for item in messages]

        with patch.object(backend, '_load_chat', side_effect=load_chat), \
             patch.object(backend, '_save_chat', side_effect=flaky_save):
            first = backend._create_critical_alert(
                target_user_id=OWNER_ID,
                kind='plan_overdue',
                title='t',
                ref_id='2026-10-13',
            )
            second = backend._create_critical_alert(
                target_user_id=OWNER_ID,
                kind='plan_overdue',
                title='t',
                ref_id='2026-10-13',
            )

        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(backend._load_critical_alerts()), 1)
        self.assertEqual(len(chat), 1)
        self.assertEqual(chat[0]['critical_alert_id'], first['id'])
        self.assertNotIn('_chat_create_pending', backend._load_critical_alerts()[0])

    def test_ack_chat_failure_does_not_false_500_after_ack_commit(self):
        self._seed_alert()
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_save_chat', side_effect=OSError('chat failed')):
            result = backend.ack_critical_alert(
                'alert-1', _AckBody('получил'), user=self.user
            )

        self.assertIsNotNone(result['acknowledged_at'])
        stored = backend._load_critical_alerts()[0]
        self.assertIsNotNone(stored['acknowledged_at'])
        self.assertEqual(stored['comment'], 'получил')

    def test_resolve_chat_failure_does_not_false_500_after_resolution_commit(self):
        self._seed_alert()
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_save_chat', side_effect=OSError('chat failed')):
            result = backend.resolve_critical_alert(
                'alert-1', resolution='no', note='не решено', files=[], user=self.user
            )

        self.assertEqual(result['resolution'], 'no')
        stored = backend._load_critical_alerts()[0]
        self.assertEqual(stored['resolution'], 'no')
        self.assertEqual(stored['resolution_note'], 'не решено')

    def test_invalid_second_photo_leaves_no_first_photo_or_metadata_change(self):
        self._seed_alert()
        valid = _upload('one.jpg', _jpeg_bytes())
        invalid = _upload('two.txt', b'not-an-image')

        with self.assertRaises(HTTPException):
            backend.resolve_critical_alert(
                'alert-1',
                resolution='yes',
                note='',
                files=[valid, invalid],
                user=self.user,
            )

        alert_dir = os.path.join(backend.CRITICAL_ALERT_PHOTO_DIR, 'alert-1')
        self.assertFalse(os.path.exists(alert_dir) and os.listdir(alert_dir))
        self.assertEqual(backend._load_critical_alerts()[0]['resolution_photos'], [])

    def test_metadata_failure_rolls_back_new_photo_and_preserves_old_live_photo(self):
        self._seed_alert(resolution='yes', resolution_photos=['old.jpg'])
        alert_dir = os.path.join(backend.CRITICAL_ALERT_PHOTO_DIR, 'alert-1')
        os.makedirs(alert_dir, exist_ok=True)
        old_path = os.path.join(alert_dir, 'old.jpg')
        with open(old_path, 'wb') as handle:
            handle.write(_jpeg_bytes())

        original_tx = backend.update_json_transaction

        def fail_critical_alert_transaction(path, default, mutator):
            if path == backend.CRITICAL_ALERTS_FILE:
                raise OSError('metadata commit failed')
            return original_tx(path, default, mutator)

        with patch.object(
            backend,
            'update_json_transaction',
            side_effect=fail_critical_alert_transaction,
        ):
            with self.assertRaises(OSError):
                backend.resolve_critical_alert(
                    'alert-1',
                    resolution='yes',
                    note='new',
                    files=[_upload('new.jpg', _jpeg_bytes())],
                    user=self.user,
                )

        self.assertTrue(os.path.exists(old_path))
        self.assertEqual(os.listdir(alert_dir), ['old.jpg'])
        stored = backend._load_critical_alerts()[0]
        self.assertEqual(stored['resolution_photos'], ['old.jpg'])
        self.assertEqual(stored['resolution'], 'yes')


if __name__ == '__main__':
    unittest.main()
