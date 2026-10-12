"""Тесты для reply/forward/delete-доступа (действия с сообщениями чата).

Тот же стиль, что tests/test_chat_backend.py -- plain unittest, функции
эндпоинтов вызываются напрямую (без TestClient/HTTP-слоя), _load_chat/_save_chat
мокаются через patch.object.

Run:
    /home/promonta/agent/miniapp/.venv/bin/python3 -m unittest tests.test_chat_actions -v
"""
import json
import os
import sys
import unittest
import unittest.mock
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
import core.permissions as permissions  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_A = {'id': 10, 'first_name': 'Ivan'}
WORKER_B = {'id': 20, 'first_name': 'Petr'}


def _msg(id_, user_id, text='hi', thread_key=None, to_user_id=None, attachment=None, location=None):
    return {
        'id': id_, 'ts': 1000, 'user_id': user_id, 'name': f'user{user_id}',
        'text': text, 'to_user_id': to_user_id, 'thread_key': thread_key,
        'attachment': attachment, 'location': location,
    }


class ReplyTests(unittest.TestCase):
    def test_reply_in_same_thread_ok(self):
        source = _msg('m1', 10, text='original', thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='re', thread_key='obj:OBJ-1', reply_to_id='m1')
            result = backend.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['reply_to']['id'], 'm1')
        self.assertEqual(result['message']['reply_to']['preview'], 'original')

    def test_reply_to_nonexistent_message_404(self):
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='re', thread_key='obj:OBJ-1', reply_to_id='missing')
            with self.assertRaises(HTTPException) as ctx:
                backend.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_quote_message_from_other_dm(self):
        source = _msg('m1', 10, text='private', to_user_id='1')  # DM 10<->1
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_load_roles', return_value={'1': 'owner', '20': 'worker', '30': 'worker'}):
            body = backend.ChatMessageBody(text='re', to_user_id='30', reply_to_id='m1')  # 20 -> 30
            with self.assertRaises(HTTPException) as ctx:
                backend.post_chat_message(body, user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_old_messages_without_reply_field_still_work(self):
        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='plain', thread_key='obj:OBJ-1')
            result = backend.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertIsNone(result['message']['reply_to'])


class ForwardTests(unittest.TestCase):
    def test_forward_to_allowed_thread(self):
        source = _msg('m1', 10, text='fwd me', thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_check_message_access'), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='', thread_key='obj:OBJ-2')
            result = backend.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['text'], 'fwd me')
        self.assertEqual(result['message']['forwarded_from'], 'user10')

    def test_forward_denied_to_inaccessible_thread(self):
        source = _msg('m1', 10, text='fwd me', thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_check_message_access'), \
             patch.object(backend, '_check_thread_access', side_effect=HTTPException(403, 'no access')):
            body = backend.ChatMessageBody(text='', thread_key='obj:OBJ-2')
            with self.assertRaises(HTTPException) as ctx:
                backend.forward_chat_message('m1', body, user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_forward_attachment_preserves_file_reference_not_broken_link(self):
        att = {'file': 'abc123.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        source = _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=att)
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_check_message_access'), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='', thread_key='obj:OBJ-2')
            result = backend.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['attachment']['file'], 'abc123.jpg')

    def test_forward_location_preserves_coordinates(self):
        loc = {'lat': 52.52, 'lon': 13.405, 'accuracy': 12.4}
        source = _msg('m1', 10, text='', thread_key='obj:OBJ-1', location=loc)
        with patch.object(backend, '_load_chat', return_value=[source]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_check_message_access'), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='', thread_key='obj:OBJ-2')
            result = backend.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['location'], loc)


class LocationMessageTests(unittest.TestCase):
    def test_location_message_stored_without_text(self):
        saved = {}

        def fake_save_chat(items):
            saved['messages'] = items

        with patch.object(backend, '_load_chat', return_value=[]), \
             patch.object(backend, '_save_chat', side_effect=fake_save_chat), \
             patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(
                text='', thread_key='obj:OBJ-1',
                location={'lat': 52.520008, 'lon': 13.404954, 'accuracy': 8.6},
            )
            result = backend.post_chat_message(body, user=WORKER_A, role='worker')

        self.assertEqual(result['message']['text'], '')
        self.assertEqual(result['message']['location']['lat'], 52.520008)
        self.assertEqual(result['message']['location']['lon'], 13.404954)
        self.assertEqual(result['message']['location']['accuracy'], 8.6)
        self.assertEqual(saved['messages'][0]['location'], result['message']['location'])
        self.assertEqual(backend._message_preview(result['message']), '📍 Геолокация')

    def test_invalid_location_rejected(self):
        with patch.object(backend, '_check_thread_access'):
            body = backend.ChatMessageBody(text='', thread_key='obj:OBJ-1', location={'lat': 123, 'lon': 13.4})
            with self.assertRaises(HTTPException) as ctx:
                backend.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 400)


class DeleteAccessTests(unittest.TestCase):
    def test_worker_deletes_own_message(self):
        msg = _msg('m1', 10, thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[msg]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_archive_chat_messages'), \
             patch.object(backend, '_load_chat_reactions', return_value=[]):
            result = backend.delete_chat_message('m1', user=WORKER_A, role='worker')
        self.assertEqual(result['status'], 'ok')

    def test_worker_cannot_delete_others_message(self):
        msg = _msg('m1', 10, thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[msg]):
            with self.assertRaises(HTTPException) as ctx:
                backend.delete_chat_message('m1', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_deletes_any_message(self):
        msg = _msg('m1', 10, thread_key='obj:OBJ-1')
        with patch.object(backend, '_load_chat', return_value=[msg]), \
             patch.object(backend, '_save_chat'), \
             patch.object(backend, '_archive_chat_messages'), \
             patch.object(backend, '_load_chat_reactions', return_value=[]):
            result = backend.delete_chat_message('m1', user=OWNER, role='owner')
        self.assertEqual(result['status'], 'ok')


class ThreadDeleteTests(unittest.TestCase):
    def test_owner_delete_dm_removes_only_exact_thread_pair(self):
        messages = [
            _msg('owner-a-1', 1, to_user_id='10'),
            _msg('a-owner-1', 10, to_user_id='1'),
            _msg('a-b-1', 10, to_user_id='20'),
            _msg('b-a-1', 20, to_user_id='10'),
            _msg('b-c-1', 20, to_user_id='30'),
            _msg('group-a', 10, to_user_id=None),
        ]
        reactions = [
            {'message_id': 'owner-a-1', 'user_id': '10', 'reaction': '👍'},
            {'message_id': 'a-b-1', 'user_id': '20', 'reaction': '👍'},
            {'message_id': 'b-c-1', 'user_id': '30', 'reaction': '👍'},
        ]
        saved = {}

        def fake_save_chat(items):
            saved['messages'] = items

        def fake_save_reactions(items):
            saved['reactions'] = items

        with patch.object(backend, '_load_chat', return_value=messages), \
             patch.object(backend, '_save_chat', side_effect=fake_save_chat), \
             patch.object(backend, '_archive_chat_messages') as archive_mock, \
             patch.object(backend, '_load_chat_reactions', return_value=reactions), \
             patch.object(backend, '_save_chat_reactions', side_effect=fake_save_reactions), \
             patch.object(backend, '_load_roles', return_value={'1': 'owner', '10': 'worker', '20': 'worker', '30': 'worker'}):
            result = backend.delete_chat_thread(thread_key='', with_='10', user=OWNER, _=None)

        self.assertEqual(result['deleted_count'], 2)
        self.assertEqual({m['id'] for m in saved['messages']}, {'a-b-1', 'b-a-1', 'b-c-1', 'group-a'})
        archive_mock.assert_called_once()
        self.assertEqual({m['id'] for m in archive_mock.call_args[0][0]}, {'owner-a-1', 'a-owner-1'})
        self.assertEqual({r['message_id'] for r in saved['reactions']}, {'a-b-1', 'b-c-1'})

    def test_new_dm_to_revoked_user_is_rejected(self):
        with patch.object(backend, '_load_roles', return_value={'1': 'owner', '10': 'worker'}):
            body = backend.ChatMessageBody(text='hello', to_user_id='999')
            with self.assertRaises(HTTPException) as ctx:
                backend.post_chat_message(body, user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 404)


class AttachmentAccessTests(unittest.TestCase):
    """31.07: get_chat_attachment раньше брал первое сообщение с этим файлом и
    проверял доступ только через to_user_id -- ломалось для obj:/mangel:/task:
    тредов и для файлов, пересланных в другой чат. Теперь доступ разрешён, если
    юзер имеет доступ хотя бы к одному сообщению с этим файлом."""

    def test_outsider_worker_cannot_open_object_chat_attachment(self):
        att = {'file': 'photo1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        msg = _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=att)
        with patch.object(backend, '_load_chat', return_value=[msg]), \
             patch.object(backend, '_safe_load_json', return_value=[]), \
             patch.object(backend, 'os') as mock_os:
            mock_os.path.basename.return_value = 'photo1.jpg'
            mock_os.path.exists.return_value = True
            mock_os.path.join.side_effect = lambda *a: '/'.join(a)
            with patch.object(backend, '_check_thread_access', side_effect=HTTPException(403, 'no access')):
                with self.assertRaises(HTTPException) as ctx:
                    backend.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_object_participant_can_open_attachment(self):
        att = {'file': 'photo1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        msg = _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=att)
        with patch.object(backend, '_load_chat', return_value=[msg]), \
             patch.object(backend, '_safe_load_json', return_value=[]), \
             patch.object(backend, 'os') as mock_os:
            mock_os.path.basename.return_value = 'photo1.jpg'
            mock_os.path.exists.return_value = True
            mock_os.path.join.side_effect = lambda *a: '/'.join(a)
            with patch.object(backend, '_check_thread_access'), \
                 patch.object(backend, 'FileResponse', return_value='OK') as mock_fr:
                result = backend.get_chat_attachment('photo1.jpg', user=WORKER_A, role='worker')
        self.assertEqual(result, 'OK')
        mock_fr.assert_called_once()

    def test_new_recipient_can_open_forwarded_attachment(self):
        att = {'file': 'photo1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        original = _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=att)
        forwarded = _msg('m2', 10, text='', thread_key='obj:OBJ-2', attachment=att)
        with patch.object(backend, '_load_chat', return_value=[original, forwarded]), \
             patch.object(backend, '_safe_load_json', return_value=[]), \
             patch.object(backend, 'os') as mock_os:
            mock_os.path.basename.return_value = 'photo1.jpg'
            mock_os.path.exists.return_value = True
            mock_os.path.join.side_effect = lambda *a: '/'.join(a)

            def access(thread_key, uid, role):
                if thread_key == 'obj:OBJ-1':
                    raise HTTPException(403, 'no access')
                # OBJ-2: доступ есть

            with patch.object(backend, '_check_thread_access', side_effect=access), \
                 patch.object(backend, 'FileResponse', return_value='OK') as mock_fr:
                result = backend.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertEqual(result, 'OK')
        mock_fr.assert_called_once()

    def test_no_access_to_either_thread_403(self):
        att = {'file': 'photo1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        original = _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=att)
        forwarded = _msg('m2', 10, text='', thread_key='obj:OBJ-2', attachment=att)
        with patch.object(backend, '_load_chat', return_value=[original, forwarded]), \
             patch.object(backend, '_safe_load_json', return_value=[]), \
             patch.object(backend, 'os') as mock_os:
            mock_os.path.basename.return_value = 'photo1.jpg'
            mock_os.path.exists.return_value = True
            mock_os.path.join.side_effect = lambda *a: '/'.join(a)
            with patch.object(backend, '_check_thread_access', side_effect=HTTPException(403, 'no access')):
                with self.assertRaises(HTTPException) as ctx:
                    backend.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)


class AttachmentThreadKeyTests(unittest.TestCase):
    """31.07: post_chat_attachment принимал thread_key в форме, но не сохранял его
    в сообщении -- вложение, отправленное в obj:-тред, попадало в общий чат."""

    def test_attachment_sent_to_object_thread_gets_thread_key(self):
        # Exercise the extracted handler against the real pytest-isolated stores.
        # This deliberately avoids monkey-patching legacy main.py helper aliases.
        for critical_path in (backend.ROLES_FILE, backend.OBJECT_ASSIGNMENTS_FILE, backend.CHAT_FILE):
            marker = backend._corrupt_lock_path(critical_path)
            if os.path.exists(marker):
                os.remove(marker)
        backend._atomic_write_json(backend.ROLES_FILE, {'10': 'worker'})
        backend._atomic_write_json(backend.OBJECT_ASSIGNMENTS_FILE, {
            'OBJ-1': [{'id': 'a1', 'user_id': '10', 'status': 'accepted'}],
        })
        backend._atomic_write_json(backend.CHAT_FILE, [])

        fake_file = unittest.mock.MagicMock()
        # Minimal JPEG magic is enough for sniff_chat_attachment().
        fake_file.file.read.return_value = b'\xff\xd8\xff\xe0' + b'chat-test'
        fake_file.filename = 'photo.jpg'
        fake_file.content_type = 'image/jpeg'
        result = backend.post_chat_attachment(
            thread_key='obj:OBJ-1', to_user_id='', file=fake_file,
            user=WORKER_A, role='worker',
        )
        persisted = backend._safe_load_json(backend.CHAT_FILE, [])
        self.assertEqual(result['message']['thread_key'], 'obj:OBJ-1')
        self.assertIsNone(result['message']['to_user_id'])
        self.assertEqual(persisted[-1]['thread_key'], 'obj:OBJ-1')


if __name__ == '__main__':
    unittest.main()
