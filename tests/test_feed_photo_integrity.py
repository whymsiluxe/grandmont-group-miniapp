import os
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
from photo_feed_testkit import PhotoFeedHarness  # noqa: E402
from routes.photo_feed import PhotoReactionBody  # noqa: E402


class FeedPhotoIntegrityTests(unittest.TestCase):
    def test_list_feed_photos_hides_posts_without_existing_files(self):
        harness = PhotoFeedHarness()
        try:
            harness.create_photo_file('keep.jpg')
            harness.metadata = [
                {'id': 'missing', 'files': ['missing.jpg'], 'comments': [{'id': 'c1'}]},
                {'id': 'partial', 'files': ['keep.jpg', 'gone.jpg'], 'comments': []},
            ]
            harness._write_json(harness.reactions_path, {'partial': {'1': True, '2': True}})
            harness.saved = {'1': {'photo': {'partial': 123}}}

            result = harness.handlers.list_feed_photos(user={'id': 1})

            self.assertEqual([p['id'] for p in result['photos']], ['partial'])
            self.assertEqual(result['photos'][0]['files'], ['keep.jpg'])
            self.assertEqual(result['photos'][0]['comment_count'], 0)
            self.assertEqual(result['photos'][0]['likes'], 2)
            self.assertTrue(result['photos'][0]['liked_by_me'])
            self.assertTrue(result['photos'][0]['saved_by_me'])
        finally:
            harness.close()

    def test_react_feed_photo_persists_like_state(self):
        harness = PhotoFeedHarness()
        try:
            harness.metadata = [{'id': 'PH1', 'files': ['keep.jpg']}]
            liked = harness.handlers.react_feed_photo(
                'PH1', PhotoReactionBody(liked=True), user={'id': 7},
            )
            self.assertEqual(harness.reactions, {'PH1': {'7': True}})
            unliked = harness.handlers.react_feed_photo(
                'PH1', PhotoReactionBody(liked=False), user={'id': 7},
            )

            self.assertEqual(liked, {'likes': 1, 'liked_by_me': True})
            self.assertEqual(unliked, {'likes': 0, 'liked_by_me': False})
            self.assertEqual(harness.reactions, {'PH1': {}})
        finally:
            harness.close()

    def test_react_feed_photo_404_for_missing_post(self):
        harness = PhotoFeedHarness()
        try:
            harness.metadata = []
            with self.assertRaises(backend.HTTPException) as ctx:
                harness.handlers.react_feed_photo(
                    'missing', PhotoReactionBody(liked=True), user={'id': 7},
                )
            self.assertEqual(ctx.exception.status_code, 404)
        finally:
            harness.close()

    def test_save_feed_photo_persists_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_path = os.path.join(tmp, 'feed_saved.json')
            meta = [{'id': 'PH1', 'files': ['keep.jpg']}]

            with (
                patch.object(backend, 'FEED_SAVED_FILE', saved_path),
                patch.object(backend, '_load_photo_meta', return_value=meta),
            ):
                saved = backend.set_feed_saved(
                    backend.FeedSavedBody(item_type='photo', item_id='PH1', saved=True),
                    user={'id': 7},
                )
                unsaved = backend.set_feed_saved(
                    backend.FeedSavedBody(item_type='photo', item_id='PH1', saved=False),
                    user={'id': 7},
                )

            self.assertTrue(saved['saved_by_me'])
            self.assertFalse(unsaved['saved_by_me'])

    def test_save_feed_photo_accepts_legacy_plural_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_path = os.path.join(tmp, 'feed_saved.json')
            meta = [{'id': 'PH1', 'files': ['keep.jpg']}]

            with (
                patch.object(backend, 'FEED_SAVED_FILE', saved_path),
                patch.object(backend, '_load_photo_meta', return_value=meta),
            ):
                result = backend.set_feed_saved(
                    backend.FeedSavedBody(item_type='photos', item_id='PH1', saved=True),
                    user={'id': 7},
                )

            self.assertEqual(result['item_type'], 'photo')
            self.assertTrue(result['saved_by_me'])

    def test_save_weather_accepts_info_alias_and_marks_feed(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved_path = os.path.join(tmp, 'feed_saved.json')
            weather_path = os.path.join(tmp, 'weather.json')
            backend._atomic_write_json(weather_path, [{'object': 'Дом', 'created': '2026-09-13T08:00:00'}])

            with (
                patch.object(backend, 'WEATHER_FEED_FILE', weather_path),
                patch.object(backend, 'FEED_SAVED_FILE', saved_path),
            ):
                result = backend.set_feed_saved(
                    backend.FeedSavedBody(item_type='info', item_id='Дом::2026-09-13T08:00:00', saved=True),
                    user={'id': 7},
                )
                feed = backend.get_weather_feed(user={'id': 7})

            self.assertEqual(result['item_type'], 'weather')
            self.assertTrue(result['saved_by_me'])
            self.assertTrue(feed['feed'][0]['saved_by_me'])

    def test_photo_comment_reply_to_is_stored(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, 'feed_photos.json')
            backend._atomic_write_json(meta_path, [{
                'id': 'PH1',
                'files': ['keep.jpg'],
                'user_id': 20,
                'comments': [{
                    'id': 'C1',
                    'user_id': '9',
                    'name': 'Ann',
                    'text': 'hello',
                    'at': '2026-09-13T08:00:00',
                }],
            }])

            with patch.object(backend, 'PHOTO_META_FILE', meta_path):
                result = backend.add_feed_photo_comment(
                    'PH1',
                    backend.PhotoCommentBody(text='reply', reply_to='C1'),
                    user={'id': 10, 'first_name': 'Bob'},
                )

            self.assertEqual(result['comments'][1]['reply_to'], 'C1')
            self.assertEqual(result['comments'][1]['text'], 'reply')

    def test_worker_cannot_delete_another_photo_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, 'feed_photos.json')
            backend._atomic_write_json(meta_path, [{
                'id': 'PH1', 'files': ['keep.jpg'],
                'comments': [{'id': 'C1', 'user_id': '9', 'text': 'hello'}],
            }])

            with patch.object(backend, 'PHOTO_META_FILE', meta_path):
                with self.assertRaises(backend.HTTPException) as ctx:
                    backend.delete_feed_photo_comment('PH1', 'C1', user={'id': 10}, role='worker')

            self.assertEqual(ctx.exception.status_code, 403)

    def test_worker_deletes_own_photo_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            meta_path = os.path.join(tmp, 'feed_photos.json')
            backend._atomic_write_json(meta_path, [{
                'id': 'PH1', 'files': ['keep.jpg'],
                'comments': [{'id': 'C1', 'user_id': '9', 'text': 'hello'}],
            }])

            with patch.object(backend, 'PHOTO_META_FILE', meta_path):
                result = backend.delete_feed_photo_comment('PH1', 'C1', user={'id': 9}, role='worker')

            self.assertEqual(result['comments'], [])


if __name__ == '__main__':
    unittest.main()
