"""Regression coverage for current-profile display-name resolution."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault('BOT_TOKEN', 'ci-dummy')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import main as backend  # noqa: E402


WORKER_ID = '10'
OWNER = {'id': 1, 'first_name': 'Boss'}


class ResolveCurrentDisplayNameTests(unittest.TestCase):
    def test_current_profile_name_wins_over_legacy_stored_id(self):
        with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
            self.assertEqual(backend._resolve_current_display_name(WORKER_ID, WORKER_ID), 'Марат')

    def test_stored_name_fallback_when_profile_missing(self):
        with patch.object(backend, '_load_worker_profiles', return_value={}):
            self.assertEqual(backend._resolve_current_display_name(WORKER_ID, 'Иван'), 'Иван')


class ChatDisplayNameResolutionTests(unittest.TestCase):
    def test_legacy_chat_message_resolves_current_name(self):
        with patch.object(backend, '_load_chat', return_value=[{
            'id': 'm1', 'ts': 1000, 'user_id': int(WORKER_ID), 'name': WORKER_ID,
            'text': 'hi', 'to_user_id': None, 'thread_key': None,
        }]), patch.object(backend, '_save_chat'), patch.object(backend, '_load_chat_reactions', return_value=[]), \
             patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
            result = backend.get_chat_messages(user=OWNER, role='owner')
        self.assertEqual(result['messages'][0]['name'], 'Марат')


class FeedPostNameResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = self.tmpdir.name
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
        self.tmpdir.cleanup()

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
    """Past comments must resolve against the current profile name on read."""

    def test_news_comment_with_legacy_id_as_name_resolves_on_read(self):
        # Seed the real isolated store so this test survives implementation moves.
        original = backend._safe_load_json(backend.NEWS_COMMENTS_FILE, {})
        try:
            backend._atomic_write_json(backend.NEWS_COMMENTS_FILE, {
                'post-1': [{
                    'id': 'c1', 'user_id': WORKER_ID, 'name': WORKER_ID,
                    'text': 'hi', 'ts': 1000,
                }],
            })
            with patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
                result = backend.get_news_comments('post-1', user=OWNER)
            self.assertEqual(result['comments'][0]['name'], 'Марат')
        finally:
            backend._atomic_write_json(backend.NEWS_COMMENTS_FILE, original)

    def test_feed_photo_comment_with_legacy_id_as_name_resolves_on_read(self):
        with patch.object(backend, '_load_photo_meta', return_value=[{
            'id': 'p1', 'user_id': WORKER_ID,
            'comments': [{'id': 'c1', 'user_id': WORKER_ID, 'name': WORKER_ID, 'text': 'hi', 'at': '2026-01-01T00:00:00'}],
        }]), patch.object(backend, '_load_worker_profiles', return_value={WORKER_ID: {'name': 'Марат'}}):
            result = backend.get_feed_photo_comments('p1', user=OWNER)
        self.assertEqual(result['comments'][0]['name'], 'Марат')


if __name__ == '__main__':
    unittest.main()
