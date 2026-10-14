import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402


WORKER = {'id': 10, 'first_name': 'Worker'}
OTHER_WORKER = {'id': 20, 'first_name': 'Other'}
OWNER = {'id': 1, 'first_name': 'Owner'}


def _manual_body(**overrides):
    data = {
        'object_id': 'OBJ-1',
        'art': 'Arbeitszeit',
        'date': '2026-09-22',
        'start_time': '08:00',
        'end_time': '16:00',
        'pause_minutes': 30,
        'description': 'Коррекция времени',
    }
    data.update(overrides)
    return backend.ZeiterfassungBody(**data)


class CheckinManualAndIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='checkin-manual-idem-')
        # 25.09 (test-pollution fix): these _FILE reassignments used to leak
        # into every later test in the same pytest process -- tearDown below
        # deleted the tmpdir itself but never restored these module attrs,
        # so any later test reading backend.OBJECT_ASSIGNMENTS_FILE (etc.)
        # without patching it itself would silently point at a deleted
        # directory. Save so tearDown can restore them.
        self._saved_attrs = {
            name: getattr(backend, name)
            for name in ('CHECKIN_META_FILE', 'CHECKIN_IDEMPOTENCY_FILE', 'ROLES_FILE', 'OBJECT_ASSIGNMENTS_FILE')
        }
        backend.CHECKIN_META_FILE = os.path.join(self.tmpdir, 'checkin_meta.json')
        backend.CHECKIN_IDEMPOTENCY_FILE = os.path.join(self.tmpdir, 'checkin_idempotency.json')
        backend.ROLES_FILE = os.path.join(self.tmpdir, 'roles.json')
        backend.OBJECT_ASSIGNMENTS_FILE = os.path.join(self.tmpdir, 'object_assignments.json')
        backend._idempotency_cache.clear()
        backend._save_roles({'1': 'owner', '10': 'worker', '20': 'worker'})
        backend._atomic_write_json(backend.OBJECT_ASSIGNMENTS_FILE, {
            'OBJ-1': [
                {'id': 'assign-10', 'user_id': '10', 'status': 'accepted', 'date_from': '2026-09-01', 'date_to': '2026-09-30'},
                {'id': 'assign-20', 'user_id': '20', 'status': 'accepted', 'date_from': '2026-09-01', 'date_to': '2026-09-30'},
            ],
        })

    def tearDown(self):
        backend._idempotency_cache.clear()
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        for name, value in self._saved_attrs.items():
            setattr(backend, name, value)

    def _object_rows(self):
        return [['ID объекта', 'Объект'], ['OBJ-1', 'Дом Мюллер']]

    def test_start_uses_client_occurred_at_not_delivery_time(self):
        tz = backend.business_now().tzinfo
        event_ts = int(datetime(2026, 9, 22, 8, 15, tzinfo=tz).timestamp())
        delivery_ts = event_ts + 3 * 3600
        save_photos = AsyncMock(return_value=['OBJ-1/2026-09-22/start.jpg'])

        with patch.object(backend.time, 'time', return_value=delivery_ts), \
             patch.object(backend, '_save_checkin_photos', new=save_photos), \
             patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_upsert_checkin_feed_post'):
            started = asyncio.run(backend.checkin_start(
                object_id='OBJ-1', lat='52.5', lon='13.4', accuracy='', geo_timestamp='',
                occurred_at=str(event_ts * 1000), stage_name='', files=[MagicMock()],
                daily_plan_id='', daily_plan_version='', daily_plan_acceptance_id='',
                user=WORKER, role='worker', idempotency_key='start-offline-time',
            ))

        self.assertEqual(started['start_at'], event_ts)
        self.assertEqual(started['start_received_at'], delivery_ts)
        self.assertEqual(started['date'], '2026-09-22')
        self.assertEqual(started['start_occurred_at_source'], 'client')

    def test_manual_time_does_not_block_next_photo_shift_start(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row'):
            manual = backend.checkin_manual(_manual_body(), user=WORKER, role='worker', idempotency_key='manual-1')

        event_ts = int(datetime(2026, 9, 22, 8, 0, tzinfo=backend.business_now().tzinfo).timestamp())
        save_photos = AsyncMock(return_value=['OBJ-1/2026-09-22/start.jpg'])
        with patch.object(backend.time, 'time', return_value=event_ts + 60), \
             patch.object(backend, '_save_checkin_photos', new=save_photos), \
             patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_upsert_checkin_feed_post'):
            started = asyncio.run(backend.checkin_start(
                object_id='OBJ-1', lat='52.5', lon='13.4', accuracy='', geo_timestamp='',
                occurred_at=str(event_ts * 1000), stage_name='', files=[MagicMock()],
                daily_plan_id='', daily_plan_version='', daily_plan_acceptance_id='',
                user=WORKER, role='worker', idempotency_key='start-1',
            ))
            backend._idempotency_cache.clear()
            replayed = asyncio.run(backend.checkin_start(
                object_id='OBJ-1', lat='52.5', lon='13.4', accuracy='', geo_timestamp='',
                occurred_at=str(event_ts * 1000), stage_name='', files=[MagicMock()],
                daily_plan_id='', daily_plan_version='', daily_plan_acceptance_id='',
                user=WORKER, role='worker', idempotency_key='start-1',
            ))

        items = backend._load_checkin_meta()
        self.assertTrue(manual['manual_entry'])
        self.assertEqual(started['object_id'], 'OBJ-1')
        self.assertEqual(started['id'], replayed['id'])
        self.assertEqual(save_photos.await_count, 1)
        self.assertEqual(len(items), 2)
        self.assertEqual(len([s for s in items if backend._is_active_photo_checkin_session(s)]), 1)

    def test_manual_time_rejects_invalid_date_time_pause_and_missing_assignment(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()):
            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(pause_minutes=-1), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(date='22.09.2026'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(start_time='16:00', end_time='08:00'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 400)

            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(date='2026-08-31'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 403)

    def test_manual_time_replays_same_key_without_duplicate_entry(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row') as write_row:
            first = backend.checkin_manual(_manual_body(), user=WORKER, role='worker', idempotency_key='manual-same')
            backend._idempotency_cache.clear()
            second = backend.checkin_manual(_manual_body(), user=WORKER, role='worker', idempotency_key='manual-same')

        self.assertEqual(first['id'], second['id'])
        self.assertEqual(len(backend._load_checkin_meta()), 1)
        write_row.assert_called_once()

    def test_manual_time_survives_crash_between_business_write_and_idempotency_save(self):
        # Phase 22 strengthens the old F03 crash-window contract: once the manual
        # time entry is durable in checkin_meta.json, failure of the secondary
        # idempotency response store must NOT turn the request into a false 500.
        # A retry with the same key must still resolve the same deterministic entry.
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row'), \
             patch.object(backend, '_idempotency_save', side_effect=RuntimeError('simulated crash')):
            first = backend.checkin_manual(
                _manual_body(), user=WORKER, role='worker', idempotency_key='manual-crash'
            )

        # "Restart": in-memory cache lost, durable idempotency entry still 'pending'.
        backend._idempotency_cache.clear()
        items_after_crash = backend._load_checkin_meta()
        self.assertEqual(len(items_after_crash), 1, "business fact must have survived the simulated crash")
        crashed_entry_id = items_after_crash[0]['id']
        self.assertEqual(first['id'], crashed_entry_id)

        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row') as write_row:
            replayed = backend.checkin_manual(_manual_body(), user=WORKER, role='worker', idempotency_key='manual-crash')

        # Same derived entity id -- the retry lands on the SAME business fact,
        # not a second one, and the durable idempotency store is now healed to 'done'.
        self.assertEqual(replayed['id'], crashed_entry_id)
        self.assertEqual(len(backend._load_checkin_meta()), 1, "crash-recovered retry must not duplicate the manual entry")
        write_row.assert_not_called()

    def test_manual_time_art_whitelist_rejects_unknown_type(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()):
            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(art='Urlaub'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 400)

    def test_manual_time_art_whitelist_accepts_fahrzeit(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row'):
            entry = backend.checkin_manual(_manual_body(art='Fahrzeit'), user=WORKER, role='worker', idempotency_key='')
        self.assertEqual(entry['art'], 'Fahrzeit')

    def test_manual_time_rejects_overlap_with_existing_manual_entry(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row'):
            backend.checkin_manual(_manual_body(start_time='08:00', end_time='16:00'), user=WORKER, role='worker', idempotency_key='')
            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(start_time='15:00', end_time='18:00'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 409)

    def test_manual_time_allows_adjacent_non_overlapping_entry(self):
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_write_zeiterfassung_row'):
            backend.checkin_manual(_manual_body(start_time='08:00', end_time='12:00'), user=WORKER, role='worker', idempotency_key='')
            second = backend.checkin_manual(_manual_body(start_time='12:00', end_time='16:00'), user=WORKER, role='worker', idempotency_key='')
        self.assertEqual(second['start_time'], '12:00')

    def test_start_survives_crash_between_business_write_and_idempotency_save(self):
        # Phase 21 strengthens the old F03 crash-window contract: once the open
        # session is durable in checkin_meta.json, failure of the secondary
        # idempotency response store must NOT turn Start into a false 500. A retry
        # with the same key must still land on the SAME deterministic session.
        event_ts = int(datetime(2026, 9, 22, 8, 0, tzinfo=backend.business_now().tzinfo).timestamp())
        first_received_at = event_ts + 60
        save_photos = AsyncMock(return_value=['OBJ-1/2026-09-22/start.jpg'])
        with patch.object(backend.time, 'time', return_value=first_received_at), \
             patch.object(backend, '_save_checkin_photos', new=save_photos), \
             patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_upsert_checkin_feed_post'), \
             patch.object(backend, '_idempotency_save', side_effect=RuntimeError('simulated crash')):
            first = asyncio.run(backend.checkin_start(
                object_id='OBJ-1', lat='52.5', lon='13.4', accuracy='', geo_timestamp='',
                occurred_at=str(event_ts * 1000), stage_name='', files=[MagicMock()],
                daily_plan_id='', daily_plan_version='', daily_plan_acceptance_id='',
                user=WORKER, role='worker', idempotency_key='start-crash',
            ))

        backend._idempotency_cache.clear()
        items_after_crash = backend._load_checkin_meta()
        self.assertEqual(len(items_after_crash), 1, "open session must have survived the simulated crash")
        crashed_session_id = items_after_crash[0]['id']
        self.assertEqual(first['id'], crashed_session_id)

        # A retry (even immediately after) must not be blocked by the crashed
        # claim's still-'pending' state -- _idempotency_claim no longer 409s on
        # pending (that was the F03 bug: it blocked every retry until the full
        # 10-minute TTL). The retry proceeds to the same deterministic entity id
        # and finds the crashed attempt's business fact already there.
        save_photos_retry = AsyncMock(return_value=['OBJ-1/2026-09-22/start-retry.jpg'])
        with patch.object(backend.time, 'time', return_value=first_received_at), \
             patch.object(backend, '_save_checkin_photos', new=save_photos_retry), \
             patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()), \
             patch.object(backend, '_upsert_checkin_feed_post'), \
             patch.object(backend, '_cleanup_checkin_photo_files') as mock_cleanup, \
             patch.object(backend, '_write_zeiterfassung_row'):
            replayed = asyncio.run(backend.checkin_start(
                object_id='OBJ-1', lat='52.5', lon='13.4', accuracy='', geo_timestamp='',
                occurred_at=str(event_ts * 1000), stage_name='', files=[MagicMock()],
                daily_plan_id='', daily_plan_version='', daily_plan_acceptance_id='',
                user=WORKER, role='worker', idempotency_key='start-crash',
            ))

        self.assertEqual(replayed['id'], crashed_session_id)
        self.assertEqual(len(backend._load_checkin_meta()), 1, "crash-recovered retry must not open a second session")
        # Retry's own newly-saved photos are discarded (cleaned up) -- the reused
        # session keeps its original start_photos from the crashed first attempt.
        mock_cleanup.assert_called_once_with(['OBJ-1/2026-09-22/start-retry.jpg'])

    def test_manual_time_rejects_overlap_with_active_photo_checkin(self):
        photo_session = {
            'id': 'photo-1', 'object_id': 'OBJ-1', 'date': '2026-09-22', 'user_id': '10',
            'start_at': int(__import__('datetime').datetime(2026, 9, 22, 8, 0, tzinfo=backend.business_now().tzinfo).timestamp()),
            'finish_at': int(__import__('datetime').datetime(2026, 9, 22, 16, 0, tzinfo=backend.business_now().tzinfo).timestamp()),
        }
        backend._save_checkin_meta([photo_session])
        with patch.object(backend, '_cached_get_used_range', return_value=self._object_rows()):
            with self.assertRaises(HTTPException) as ctx:
                backend.checkin_manual(_manual_body(start_time='15:00', end_time='18:00'), user=WORKER, role='worker', idempotency_key='')
            self.assertEqual(ctx.exception.status_code, 409)

    def test_idempotency_is_scoped_by_actor_endpoint_entity_and_payload(self):
        scope_worker = backend._idempotency_scope('checkin_start', WORKER['id'], 'OBJ-1', {'object_id': 'OBJ-1'})
        scope_other = backend._idempotency_scope('checkin_start', OTHER_WORKER['id'], 'OBJ-1', {'object_id': 'OBJ-1'})
        scope_other_payload = backend._idempotency_scope('checkin_start', WORKER['id'], 'OBJ-2', {'object_id': 'OBJ-2'})

        backend._idempotency_save('same-raw-key', {'ok': True, 'user_id': '10'}, scope_worker)
        backend._idempotency_cache.clear()

        self.assertIsNone(backend._idempotency_get('same-raw-key', scope_other))
        self.assertEqual(backend._idempotency_get('same-raw-key', scope_worker), {'ok': True, 'user_id': '10'})
        with self.assertRaises(HTTPException) as ctx:
            backend._idempotency_get('same-raw-key', scope_other_payload)
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == '__main__':
    unittest.main()
