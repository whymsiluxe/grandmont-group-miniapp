"""Behavior/security tests for extracted chat write/action routes."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import HTTPException  # noqa: E402
from routes.chat_actions import ChatMessageBody  # noqa: E402
from chat_domain_testkit import ChatDomainHarness  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
WORKER_A = {'id': 10, 'first_name': 'Ivan'}
WORKER_B = {'id': 20, 'first_name': 'Petr'}


def _msg(id_, user_id, text='hi', thread_key=None, to_user_id=None, attachment=None, location=None):
    return {
        'id': id_, 'ts': 1000, 'user_id': user_id, 'name': f'user{user_id}',
        'text': text, 'to_user_id': to_user_id, 'thread_key': thread_key,
        'attachment': attachment, 'location': location,
    }


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.h = ChatDomainHarness()

    def tearDown(self):
        self.h.close()


class ReplyTests(HarnessCase):
    def test_reply_in_same_thread_ok(self):
        self.h.grant_object('OBJ-1', '10')
        self.h.messages = [_msg('m1', 10, text='original', thread_key='obj:OBJ-1')]
        body = ChatMessageBody(text='re', thread_key='obj:OBJ-1', reply_to_id='m1')
        result = self.h.actions.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['reply_to']['id'], 'm1')
        self.assertEqual(result['message']['reply_to']['preview'], 'original')

    def test_reply_to_nonexistent_message_404(self):
        self.h.grant_object('OBJ-1', '10')
        body = ChatMessageBody(text='re', thread_key='obj:OBJ-1', reply_to_id='missing')
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_cannot_quote_message_from_other_dm(self):
        self.h.messages = [_msg('m1', 10, text='private', to_user_id='1')]
        body = ChatMessageBody(text='re', to_user_id='30', reply_to_id='m1')
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.post_chat_message(body, user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_old_messages_without_reply_field_still_work(self):
        self.h.grant_object('OBJ-1', '10')
        body = ChatMessageBody(text='plain', thread_key='obj:OBJ-1')
        result = self.h.actions.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertIsNone(result['message']['reply_to'])


class ForwardTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.h.grant_object('OBJ-1', '10', '20')
        self.h.grant_object('OBJ-2', '10')

    def test_forward_to_allowed_thread(self):
        self.h.messages = [_msg('m1', 10, text='fwd me', thread_key='obj:OBJ-1')]
        body = ChatMessageBody(text='', thread_key='obj:OBJ-2')
        result = self.h.actions.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['text'], 'fwd me')
        self.assertEqual(result['message']['forwarded_from'], 'user10')

    def test_forward_denied_to_inaccessible_thread(self):
        self.h.messages = [_msg('m1', 10, text='fwd me', thread_key='obj:OBJ-1')]
        body = ChatMessageBody(text='', thread_key='obj:OBJ-2')
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.forward_chat_message('m1', body, user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_forward_attachment_preserves_file_reference_not_broken_link(self):
        attachment = {'file': 'abc123.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}
        self.h.messages = [_msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=attachment)]
        body = ChatMessageBody(text='', thread_key='obj:OBJ-2')
        result = self.h.actions.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['attachment']['file'], 'abc123.jpg')

    def test_forward_location_preserves_coordinates(self):
        location = {'lat': 52.52, 'lon': 13.405, 'accuracy': 12.4}
        self.h.messages = [_msg('m1', 10, text='', thread_key='obj:OBJ-1', location=location)]
        body = ChatMessageBody(text='', thread_key='obj:OBJ-2')
        result = self.h.actions.forward_chat_message('m1', body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['location'], location)


class LocationMessageTests(HarnessCase):
    def test_location_message_stored_without_text(self):
        self.h.grant_object('OBJ-1', '10')
        body = ChatMessageBody(
            text='', thread_key='obj:OBJ-1',
            location={'lat': 52.520008, 'lon': 13.404954, 'accuracy': 8.6},
        )
        result = self.h.actions.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(result['message']['text'], '')
        self.assertEqual(result['message']['location']['lat'], 52.520008)
        self.assertEqual(result['message']['location']['lon'], 13.404954)
        self.assertEqual(result['message']['location']['accuracy'], 8.6)
        self.assertEqual(self.h.messages[0]['location'], result['message']['location'])
        self.assertEqual(self.h.core.message_preview(result['message']), '📍 Геолокация')

    def test_invalid_location_rejected(self):
        self.h.grant_object('OBJ-1', '10')
        body = ChatMessageBody(text='', thread_key='obj:OBJ-1', location={'lat': 123, 'lon': 13.4})
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.post_chat_message(body, user=WORKER_A, role='worker')
        self.assertEqual(ctx.exception.status_code, 400)


class DeleteAccessTests(HarnessCase):
    def test_worker_deletes_own_message(self):
        self.h.messages = [_msg('m1', 10, thread_key='obj:OBJ-1')]
        result = self.h.actions.delete_chat_message('m1', user=WORKER_A, role='worker')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(self.h.messages, [])
        self.assertEqual([m['id'] for m in self.h.archive], ['m1'])

    def test_worker_cannot_delete_others_message(self):
        self.h.messages = [_msg('m1', 10, thread_key='obj:OBJ-1')]
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.delete_chat_message('m1', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_deletes_any_message(self):
        self.h.messages = [_msg('m1', 10, thread_key='obj:OBJ-1')]
        result = self.h.actions.delete_chat_message('m1', user=OWNER, role='owner')
        self.assertEqual(result['status'], 'ok')


class ThreadDeleteTests(HarnessCase):
    def test_owner_delete_dm_removes_only_exact_thread_pair(self):
        self.h.messages = [
            _msg('owner-a-1', 1, to_user_id='10'),
            _msg('a-owner-1', 10, to_user_id='1'),
            _msg('a-b-1', 10, to_user_id='20'),
            _msg('b-a-1', 20, to_user_id='10'),
            _msg('b-c-1', 20, to_user_id='30'),
            _msg('group-a', 10, to_user_id=None),
        ]
        self.h.reactions = [
            {'message_id': 'owner-a-1', 'user_id': '10', 'reaction': '👍'},
            {'message_id': 'a-b-1', 'user_id': '20', 'reaction': '👍'},
            {'message_id': 'b-c-1', 'user_id': '30', 'reaction': '👍'},
        ]
        result = self.h.actions.delete_chat_thread(thread_key='', with_='10', user=OWNER, _=None)
        self.assertEqual(result['deleted_count'], 2)
        self.assertEqual({m['id'] for m in self.h.messages}, {'a-b-1', 'b-a-1', 'b-c-1', 'group-a'})
        self.assertEqual({m['id'] for m in self.h.archive}, {'owner-a-1', 'a-owner-1'})
        self.assertEqual({r['message_id'] for r in self.h.reactions}, {'a-b-1', 'b-c-1'})

    def test_new_dm_to_revoked_user_is_rejected(self):
        self.h.roles = {'1': 'owner', '10': 'worker'}
        body = ChatMessageBody(text='hello', to_user_id='999')
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.post_chat_message(body, user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 404)


class AttachmentAccessTests(HarnessCase):
    def setUp(self):
        super().setUp()
        self.h.create_attachment('photo1.jpg')
        self.att = {'file': 'photo1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'}

    def test_outsider_worker_cannot_open_object_chat_attachment(self):
        self.h.grant_object('OBJ-1', '10')
        self.h.messages = [_msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=self.att)]
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_object_participant_can_open_attachment(self):
        self.h.grant_object('OBJ-1', '10')
        self.h.messages = [_msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=self.att)]
        response = self.h.actions.get_chat_attachment('photo1.jpg', user=WORKER_A, role='worker')
        self.assertTrue(response.path.endswith('photo1.jpg'))
        self.assertEqual(response.headers.get('x-content-type-options'), 'nosniff')

    def test_new_recipient_can_open_forwarded_attachment(self):
        self.h.grant_object('OBJ-1', '10')
        self.h.grant_object('OBJ-2', '20')
        self.h.messages = [
            _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=self.att),
            _msg('m2', 10, text='', thread_key='obj:OBJ-2', attachment=self.att),
        ]
        response = self.h.actions.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertTrue(response.path.endswith('photo1.jpg'))

    def test_no_access_to_either_thread_403(self):
        self.h.grant_object('OBJ-1', '10')
        self.h.grant_object('OBJ-2', '10')
        self.h.messages = [
            _msg('m1', 10, text='', thread_key='obj:OBJ-1', attachment=self.att),
            _msg('m2', 10, text='', thread_key='obj:OBJ-2', attachment=self.att),
        ]
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.get_chat_attachment('photo1.jpg', user=WORKER_B, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)


class AttachmentThreadKeyTests(HarnessCase):
    def test_attachment_sent_to_object_thread_gets_thread_key(self):
        self.h.grant_object('OBJ-1', '10')
        fake_file = unittest.mock.MagicMock()
        fake_file.file.read.return_value = b'\xff\xd8\xff\xe0chat-test'
        fake_file.filename = 'photo.jpg'
        fake_file.content_type = 'image/jpeg'
        result = self.h.core.post_chat_attachment(
            thread_key='obj:OBJ-1', to_user_id='', file=fake_file,
            user=WORKER_A, role='worker',
        )
        self.assertEqual(result['message']['thread_key'], 'obj:OBJ-1')
        self.assertIsNone(result['message']['to_user_id'])
        self.assertEqual(self.h.messages[-1]['thread_key'], 'obj:OBJ-1')


if __name__ == '__main__':
    unittest.main()
