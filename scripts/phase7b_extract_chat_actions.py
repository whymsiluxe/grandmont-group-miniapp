#!/usr/bin/env python3
from pathlib import Path


def replace_between(source: str, start_marker: str, end_marker: str, replacement: str, label: str) -> str:
    start = source.find(start_marker)
    end = source.find(end_marker, start)
    if start < 0 or end < 0:
        raise SystemExit(f'{label}: markers not found')
    return source[:start] + replacement + source[end:]


# 1) Extract the remaining Chat write/action implementation from main.py.
path = Path('backend/main.py')
text = path.read_text(encoding='utf-8')
start_marker = '@app.get("/api/chat/attachments/{fname}")\n'
end_marker = '# ---------- AI Chat (GLM / Sonnet / Opus, переключаемо) ----------\n'
start = text.find(start_marker)
end = text.find(end_marker, start)
if start < 0 or end < 0:
    raise SystemExit('chat actions extraction markers not found')
if text.find(start_marker, start + 1) >= 0:
    raise SystemExit('duplicate chat attachment route marker')

replacement = '''# ---------- Chat write/actions domain ----------\n# Message CRUD, reactions, thread controls, attachment serving and manager\n# broadcast live in routes/chat_actions.py and reuse the extracted Chat core.\nif __package__:\n    from .routes.chat_actions import (\n        BroadcastBody,\n        ChatActionsRouteDeps,\n        ChatMessageBody,\n        ChatReactionBody,\n        ChatThreadCloseBody,\n        ChatThreadPrefsBody,\n        create_chat_actions_router,\n    )\nelse:\n    from routes.chat_actions import (  # noqa: E402\n        BroadcastBody,\n        ChatActionsRouteDeps,\n        ChatMessageBody,\n        ChatReactionBody,\n        ChatThreadCloseBody,\n        ChatThreadPrefsBody,\n        create_chat_actions_router,\n    )\n\n_chat_actions_router, _chat_actions_handlers = create_chat_actions_router(\n    ChatActionsRouteDeps(\n        get_current_user=get_current_user,\n        get_role=get_role,\n        require_owner=require_owner,\n        core=_chat_handlers,\n        safe_load_json=lambda path, default: _safe_load_json(path, default),\n        chat_archive_file=lambda: CHAT_ARCHIVE_FILE,\n        chat_attach_dir=lambda: CHAT_ATTACH_DIR,\n        chat_reaction_options=CHAT_REACTION_OPTIONS,\n        load_roles=lambda: _load_roles(),\n        send_telegram_message=lambda chat_id, text: send_telegram_message(chat_id, text),\n        find_object_row_by_id=lambda object_id: _find_object_row_by_id(object_id),\n        append_object_history_best_effort=lambda *args, **kwargs: _append_object_history_best_effort(*args, **kwargs),\n    )\n)\napp.include_router(_chat_actions_router)\n\n# Temporary compatibility aliases; implementations live in chat_actions.py.\n_normalize_chat_location = _chat_actions_handlers.normalize_chat_location\n_reply_snapshot = _chat_actions_handlers.reply_snapshot\n_chat_thread_participants = _chat_actions_handlers.chat_thread_participants\n_check_message_access = _chat_actions_handlers.check_message_access\n_thread_user_prefs = _chat_handlers.thread_user_prefs\nget_chat_attachment = _chat_actions_handlers.get_chat_attachment\nsend_manager_broadcast = _chat_actions_handlers.send_manager_broadcast\npost_chat_message = _chat_actions_handlers.post_chat_message\nforward_chat_message = _chat_actions_handlers.forward_chat_message\ndelete_chat_message = _chat_actions_handlers.delete_chat_message\ndelete_chat_thread = _chat_actions_handlers.delete_chat_thread\ntoggle_chat_reaction = _chat_actions_handlers.toggle_chat_reaction\nset_chat_thread_prefs = _chat_actions_handlers.set_chat_thread_prefs\nclose_chat_thread = _chat_actions_handlers.close_chat_thread\nreopen_chat_thread = _chat_actions_handlers.reopen_chat_thread\nget_chat_thread_status = _chat_actions_handlers.get_chat_thread_status\n\n\n'''
new_text = text[:start] + replacement + text[end:]
if len(new_text) >= len(text):
    raise SystemExit('expected main.py to shrink')
path.write_text(new_text, encoding='utf-8')
print(f'main.py: {text.count(chr(10)) + 1} -> {new_text.count(chr(10)) + 1} lines')

# 2) Migrate two security tests in the large access-control suite from
# monkey-patching main.py internals to the extracted domain harness.
access_path = Path('tests/test_access_control.py')
access = access_path.read_text(encoding='utf-8')
if "sys.path.insert(0, os.path.dirname(__file__))" not in access:
    access = access.replace(
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))\n",
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n",
        1,
    )
if 'from chat_domain_testkit import ChatDomainHarness' not in access:
    access = access.replace(
        'import main as backend  # noqa: E402\n',
        'import main as backend  # noqa: E402\nfrom chat_domain_testkit import ChatDomainHarness  # noqa: E402\n',
        1,
    )
access = replace_between(
    access,
    '    def test_worker_cannot_delete_another_workers_chat_message(self):\n',
    '    def test_worker_cannot_ack_another_workers_critical_alert(self):\n',
    '''    def test_worker_cannot_delete_another_workers_chat_message(self):\n        h = ChatDomainHarness(roles={'111': 'worker', '222': 'worker'})\n        try:\n            h.messages = [{'id': 'm1', 'user_id': 222, 'text': 'hi', 'to_user_id': None}]\n            with self.assertRaises(HTTPException) as ctx:\n                h.actions.delete_chat_message(msg_id='m1', user={'id': 111}, role='worker')\n            self.assertEqual(ctx.exception.status_code, 403)\n        finally:\n            h.close()\n\n''',
    'cross-worker chat delete test',
)
access = replace_between(
    access,
    '    def test_worker_cannot_fetch_other_threads_chat_attachment(self):\n',
    '    def test_worker_cannot_fetch_another_users_transcribe_audio_by_guessing_id(self):\n',
    '''    def test_worker_cannot_fetch_other_threads_chat_attachment(self):\n        h = ChatDomainHarness(roles={'111': 'worker', '222': 'worker', '333': 'worker'})\n        try:\n            h.create_attachment('secret.jpg')\n            h.messages = [{\n                'id': 'm1', 'user_id': 222, 'to_user_id': '333',\n                'attachment': {'file': 'secret.jpg'},\n            }]\n            with self.assertRaises(HTTPException) as ctx:\n                h.actions.get_chat_attachment(fname='secret.jpg', user={'id': 111}, role='worker')\n            self.assertEqual(ctx.exception.status_code, 403)\n        finally:\n            h.close()\n\n''',
    'direct chat attachment ACL test',
)
access_path.write_text(access, encoding='utf-8')

# 3) Preserve archived-attachment IDOR coverage at the new domain boundary.
release_path = Path('tests/test_release_audit2.py')
release = release_path.read_text(encoding='utf-8')
if "sys.path.insert(0, os.path.dirname(__file__))" not in release:
    release = release.replace(
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))\n",
        "sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n",
        1,
    )
if 'from chat_domain_testkit import ChatDomainHarness' not in release:
    release = release.replace(
        'import main as backend  # noqa: E402\n',
        'import main as backend  # noqa: E402\nfrom chat_domain_testkit import ChatDomainHarness  # noqa: E402\n',
        1,
    )
release = replace_between(
    release,
    'class ArchivedAttachmentAccessTests(unittest.TestCase):\n',
    "\n\nif __name__ == '__main__':\n",
    '''class ArchivedAttachmentAccessTests(unittest.TestCase):\n    def test_participant_opens_attachment_from_archived_message(self):\n        h = ChatDomainHarness()\n        try:\n            h.grant_object('OBJ-1', '10')\n            h.create_attachment('archived1.jpg')\n            h.archive = [{\n                'id': 'old1', 'ts': 1000, 'user_id': 10, 'name': 'Ivan', 'text': '',\n                'to_user_id': None, 'thread_key': 'obj:OBJ-1',\n                'attachment': {'file': 'archived1.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'},\n            }]\n            response = h.actions.get_chat_attachment('archived1.jpg', user=WORKER_A, role='worker')\n            self.assertTrue(response.path.endswith('archived1.jpg'))\n        finally:\n            h.close()\n\n    def test_outsider_denied_for_archived_attachment(self):\n        h = ChatDomainHarness()\n        try:\n            h.grant_object('OBJ-1', '10')\n            h.create_attachment('archived2.jpg')\n            h.archive = [{\n                'id': 'old2', 'ts': 1000, 'user_id': 10, 'name': 'Ivan', 'text': '',\n                'to_user_id': None, 'thread_key': 'obj:OBJ-1',\n                'attachment': {'file': 'archived2.jpg', 'name': 'photo.jpg', 'content_type': 'image/jpeg'},\n            }]\n            with self.assertRaises(HTTPException) as ctx:\n                h.actions.get_chat_attachment('archived2.jpg', user=WORKER_B, role='worker')\n            self.assertEqual(ctx.exception.status_code, 403)\n        finally:\n            h.close()\n''',
    'archived attachment tests',
)
release_path.write_text(release, encoding='utf-8')
