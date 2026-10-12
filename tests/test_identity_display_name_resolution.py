"""22.09 iPhone screenshot audit, P0/P1: raw Telegram user_id displayed instead
of a name in Chat, Feed, and Calendar/Abwesenheit -- confirmed live on a real
device (Chat showed '5298622655', Feed showed '872079437' for the owner even
though Profile for that same user correctly showed their configured name).

Root cause: post_chat_message()/create_abwesenheit()/_upsert_checkin_feed_post()
all stamp a `name` field at WRITE time from
`user.get('first_name', str(user['id']))` (or similar) -- if Telegram's
first_name was empty/missing at that moment, the record permanently stores the
raw numeric user_id as its name. Every later read of that record then displays
the raw ID forever, even after the profile is later given a real name.

Fix: _resolve_current_display_name(user_id, stored_name) is a canonical
READ-SIDE resolver, applied wherever these records are returned to the
frontend (GET /api/chat/messages, GET /api/feed/photos, GET /api/abwesenheit,
GET /api/abwesenheit/all) -- it always prefers the CURRENT worker_profiles
name over whatever was frozen in the record, and only falls back to the
stored value when the profile has no meaningful name either.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402

WORKER_ID = '872079437'
OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_WITH_LEGACY_NAME = {'id': int(WORKER_ID), 'first_name': ''}


class ResolveCurrentDisplayNameTests(unittest.TestCase):
    def test_legacy_record_with_name_equal_to_user_id_resolves_to_current_profile_name(self):
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}):
            result = backend._resolve_current_display_name(WORKER_ID, stored_name=WORKER_ID)
        self.assertEqual(result, 'Лох')

    def test_no_profile_and_no_meaningful_stored_name_falls_back_to_user_id(self):
        # Genuinely nothing to resolve to -- must not crash, returns the id
        # itself as a last resort (matches _sanitize_display_name's own
        # fallback contract elsewhere in this codebase).
        with patch.object(backend, '_load_worker_profiles', return_value={}):
            result = backend._resolve_current_display_name(WORKER_ID, stored_name=WORKER_ID)
        self.assertEqual(result, WORKER_ID)

    def test_current_profile_name_wins_even_when_stored_name_was_meaningful_at_write_time(self):
        # A worker renamed their profile after a record was created -- the
        # record must show the CURRENT name, not the one frozen at write time.
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'New Name'}}):
            result = backend._resolve_current_display_name(WORKER_ID, stored_name='Old Name')
        self.assertEqual(result, 'New Name')

    def test_stored_name_used_only_when_profile_has_no_meaningful_name(self):
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': ''}}):
            result = backend._resolve_current_display_name(WORKER_ID, stored_name='Fallback Name')
        self.assertEqual(result, 'Fallback Name')


class ChatMessageNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='chat-identity-')
        self._orig_chat_file = backend.CHAT_FILE
        backend.CHAT_FILE = os.path.join(self.tmp, 'chat.json')
        self._patchers = [
            patch.object(backend, '_load_chat_reactions', return_value={}),
            patch.object(backend, '_load_reads', return_value={}),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        backend.CHAT_FILE = self._orig_chat_file
        for p in self._patchers:
            p.stop()

    def _seed_message(self, user_id, name, thread_key=None, to_user_id=None):
        import time
        backend._save_chat([{
            'id': 'm1', 'ts': int(time.time()), 'user_id': user_id, 'name': name,
            'text': 'hi', 'to_user_id': to_user_id, 'thread_key': thread_key,
        }])

    def test_legacy_chat_message_with_id_as_name_resolves_on_read(self):
        self._seed_message(WORKER_ID, WORKER_ID, thread_key='obj:OBJ-1')  # legacy: name == user_id
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}), \
             patch.object(backend, '_check_thread_access'):
            result = backend.get_chat_messages(thread_key='obj:OBJ-1', user=OWNER, role='owner')
        self.assertEqual(result['messages'][0]['name'], 'Лох')

    def test_system_sender_name_is_not_touched_by_resolver(self):
        self._seed_message('system', 'Система')
        with patch.object(backend, '_load_worker_profiles', return_value={}):
            result = backend.get_chat_messages(user=OWNER, role='owner')
        self.assertEqual(result['messages'][0]['name'], 'Система')


class AbwesenheitNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='abw-identity-')
        self._orig_file = backend.ABWESENHEIT_FILE
        backend.ABWESENHEIT_FILE = os.path.join(self.tmp, 'abwesenheit.json')

    def tearDown(self):
        backend.ABWESENHEIT_FILE = self._orig_file

    def _seed(self, user_id, name):
        backend._save_abwesenheit([{
            'id': 'abw-1', 'user_id': user_id, 'name': name,
            'date_from': '2099-09-22', 'date_to': '2099-09-23',
            'open_ended': False, 'status': 'approved',
        }])

    def test_list_my_abwesenheit_resolves_legacy_id_as_name(self):
        self._seed(WORKER_ID, WORKER_ID)
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}):
            result = backend.list_my_abwesenheit(user={'id': int(WORKER_ID)})
        self.assertEqual(result['entries'][0]['name'], 'Лох')

    def test_list_all_abwesenheit_resolves_legacy_id_as_name_for_owner(self):
        self._seed(WORKER_ID, WORKER_ID)
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}):
            result = backend.list_all_abwesenheit(user=OWNER, role='owner')
        self.assertEqual(result['entries'][0]['name'], 'Лох')

    def test_list_all_abwesenheit_resolved_name_survives_public_field_filter_for_other_workers(self):
        # A different worker (not owner, not the entry's own author) only sees
        # ABWESENHEIT_PUBLIC_FIELDS -- 'name' must still be the resolved one,
        # not the raw legacy value, since the resolve happens before filtering.
        self._seed(WORKER_ID, WORKER_ID)
        other_worker = {'id': 999}
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}):
            result = backend.list_all_abwesenheit(user=other_worker, role='worker')
        self.assertEqual(result['entries'][0]['name'], 'Лох')


class FeedPhotoNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='feed-identity-')
        self._orig_photo_dir = backend.PHOTO_DIR
        backend.PHOTO_DIR = self.tmp
        self._patchers = [
            patch.object(backend, '_load_photo_reactions', return_value={}),
            patch.object(backend, '_load_feed_saved', return_value={}),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        backend.PHOTO_DIR = self._orig_photo_dir
        for p in self._patchers:
            p.stop()

    def test_legacy_feed_post_with_id_as_name_resolves_on_read(self):
        fname = 'test.jpg'
        with open(os.path.join(self.tmp, fname), 'wb') as f:
            f.write(b'fake')
        with patch.object(backend, '_load_photo_meta', return_value=[{
            'id': 'p1', 'user_id': WORKER_ID, 'name': WORKER_ID,
            'files': [fname], 'ts': 1000, 'object_id': 'OBJ-1', 'caption': '',
        }]), patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Лох'}}):
            result = backend.list_feed_photos(user=OWNER)
        self.assertEqual(result['photos'][0]['name'], 'Лох')


class FeedCommentNameResolutionTests(unittest.TestCase):
    """22.09 (Item I): comments stored `name` at write time was never
    re-resolved on read -- a worker's later profile-name correction never
    reached their PAST comments on news posts or feed photos."""

    def test_news_comment_with_legacy_id_as_name_resolves_on_read(self):
        with patch.object(backend, '_load_news_comments', return_value={
            'post-1': [{'id': 'c1', 'user_id': WORKER_ID, 'name': WORKER_ID, 'text': 'hi', 'ts': 1000}],
        }), patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
            result = backend.get_news_comments('post-1', user=OWNER)
        self.assertEqual(result['comments'][0]['name'], 'Марат')

    def test_feed_photo_comment_with_legacy_id_as_name_resolves_on_read(self):
        with patch.object(backend, '_load_photo_meta', return_value=[{
            'id': 'p1', 'user_id': WORKER_ID,
            'comments': [{'id': 'c1', 'user_id': WORKER_ID, 'name': WORKER_ID, 'text': 'hi', 'at': '2026-01-01T00:00:00'}],
        }]), patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
            result = backend.get_feed_photo_comments('p1', user=OWNER)
        self.assertEqual(result['comments'][0]['name'], 'Марат')


if __name__ == '__main__':
    unittest.main()
