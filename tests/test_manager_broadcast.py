import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
sys.path.insert(0, os.path.dirname(__file__))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
from conftest import iter_app_routes  # noqa: E402
from routes.chat_actions import BroadcastBody  # noqa: E402
from chat_domain_testkit import ChatDomainHarness  # noqa: E402


OWNER = {'id': '1', 'first_name': 'Борис'}


class ManagerBroadcastTests(unittest.TestCase):
    def setUp(self):
        self.h = ChatDomainHarness()

    def tearDown(self):
        self.h.close()

    def test_company_broadcast_appends_group_chat_message(self):
        result = self.h.actions.send_manager_broadcast(
            BroadcastBody(scope='company', text='Завтра общий сбор в 8:00'),
            user=OWNER, _=None,
        )
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['thread_key'], 'group')
        self.assertEqual(result['audience_count'], 4)
        self.assertEqual(len(self.h.messages), 1)
        msg = self.h.messages[0]
        self.assertIsNone(msg['thread_key'])
        self.assertIsNone(msg['to_user_id'])
        self.assertEqual(msg['text'], 'Завтра общий сбор в 8:00')
        self.assertEqual(msg['broadcast']['scope'], 'company')

    def test_object_broadcast_appends_object_chat_and_history(self):
        self.h.objects['OBJ-1'] = {'ID объекта': 'OBJ-1', 'Объект': 'Мюллер'}
        self.h.grant_object('OBJ-1', '10', '20')
        result = self.h.actions.send_manager_broadcast(
            BroadcastBody(scope='object', object_id='OBJ-1', text='Берём лазер и защиту пола'),
            user=OWNER, _=None,
        )
        self.assertEqual(result['thread_key'], 'obj:OBJ-1')
        self.assertEqual(result['audience_count'], 3)
        msg = self.h.messages[0]
        self.assertEqual(msg['thread_key'], 'obj:OBJ-1')
        self.assertEqual(msg['broadcast']['object_name'], 'Мюллер')
        self.assertEqual(len(self.h.history), 1)
        args, kwargs = self.h.history[0]
        self.assertEqual(args[:3], ('OBJ-1', 'broadcast_sent', 'Объявление отправлено'))
        self.assertIn('Берём лазер', kwargs['subtitle'])
        self.assertEqual(kwargs['meta']['message_id'], msg['id'])

    def test_object_broadcast_requires_existing_object(self):
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.send_manager_broadcast(
                BroadcastBody(scope='object', object_id='MISSING', text='Текст'),
                user=OWNER, _=None,
            )
        self.assertEqual(ctx.exception.status_code, 404)

    def test_broadcast_validates_scope_and_text(self):
        with self.assertRaises(HTTPException) as ctx:
            self.h.actions.send_manager_broadcast(
                BroadcastBody(scope='team', text='Привет'), user=OWNER, _=None,
            )
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException) as empty_ctx:
            self.h.actions.send_manager_broadcast(
                BroadcastBody(scope='company', text='   '), user=OWNER, _=None,
            )
        self.assertEqual(empty_ctx.exception.status_code, 400)

    def test_broadcast_route_registered_and_owner_only(self):
        route = next(route for route in iter_app_routes(backend.app) if route.path == '/api/manager/broadcast')
        dep_callables = {dep.call for dep in route.dependant.dependencies}
        self.assertIn(backend.require_owner, dep_callables)


if __name__ == '__main__':
    unittest.main()
