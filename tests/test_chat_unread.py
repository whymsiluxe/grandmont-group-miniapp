"""Behavior tests for chat unread counters after routes/chat.py extraction."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.chat import ChatRouteDeps, create_chat_router  # noqa: E402


class UnreadCountThreadKeyTests(unittest.TestCase):
    """Unread behavior is tested at the extracted domain boundary, not via main aliases."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.messages = []
        self.reads = {}
        self.meta = {}
        self.object_participants = {'OBJ-1': {'222'}}
        self.mangel_participants = {'T-1': {'222'}}

        def safe_load(path, default):
            if path.endswith('chat.json'):
                return [dict(m) for m in self.messages]
            if path.endswith('reads.json'):
                return {k: dict(v) if isinstance(v, dict) else v for k, v in self.reads.items()}
            if path.endswith('meta.json'):
                return self.meta
            if path.endswith('reactions.json'):
                return []
            if path.endswith('archive.json'):
                return []
            return default

        deps = ChatRouteDeps(
            get_current_user=lambda: {},
            get_role=lambda: 'worker',
            safe_load_json=safe_load,
            atomic_write_json=lambda path, value: None,
            chat_file=lambda: os.path.join(self.tmp.name, 'chat.json'),
            chat_archive_file=lambda: os.path.join(self.tmp.name, 'archive.json'),
            chat_reads_file=lambda: os.path.join(self.tmp.name, 'reads.json'),
            chat_thread_meta_file=lambda: os.path.join(self.tmp.name, 'meta.json'),
            chat_reactions_file=lambda: os.path.join(self.tmp.name, 'reactions.json'),
            chat_attach_dir=lambda: self.tmp.name,
            chat_max=200,
            chat_retention_seconds=7 * 24 * 3600,
            chat_reaction_options=('👍', '👀'),
            thread_type_by_prefix={'obj:': 'OBJECT', 'mangel:': 'DEFECT', 'task:': 'TASK'},
            default_thread_prefs={'muted': False, 'pinned': False, 'archived': False},
            online_threshold_seconds=120,
            load_roles=lambda: {'111': 'worker', '222': 'worker', '333': 'worker'},
            load_assignments=lambda: {
                oid: [{'user_id': uid, 'status': 'accepted'} for uid in users]
                for oid, users in self.object_participants.items()
            },
            today_berlin_str=lambda: '2026-10-12',
            has_active_object_access=lambda uid, oid, today: uid in self.object_participants.get(oid, set()),
            load_mangel_lib=lambda: type('M', (), {'get_ticket': staticmethod(lambda tid: {})})(),
            load_tasks=lambda: [],
            cached_get_used_range=lambda tab: [],
            load_worker_profiles=lambda: {},
            last_seen_for=lambda uid: None,
            sanitize_display_name=lambda raw, fallback: raw or fallback,
            resolve_current_display_name=lambda uid, fallback: fallback or str(uid),
            sniff_chat_attachment=lambda data: ('image/jpeg', 'jpg'),
        )
        _, self.handlers = create_chat_router(deps)

    def tearDown(self):
        self.tmp.cleanup()

    def test_own_messages_never_count_as_unread(self):
        self.messages = [{'user_id': 111, 'ts': 1000, 'text': 'привет'}]
        self.assertEqual(self.handlers.get_unread_count(user={'id': 111}, role='worker')['unread'], 0)

    def test_group_message_counts_when_not_read(self):
        self.messages = [{'user_id': 222, 'ts': 1000, 'text': 'всем привет'}]
        self.assertEqual(self.handlers.get_unread_count(user={'id': 111}, role='worker')['unread'], 1)

    def test_group_message_does_not_count_after_read(self):
        self.messages = [{'user_id': 222, 'ts': 1000, 'text': 'всем привет'}]
        self.reads = {'111': {'group': 2000}}
        self.assertEqual(self.handlers.get_unread_count(user={'id': 111}, role='worker')['unread'], 0)

    def test_object_thread_message_counts_only_for_participant(self):
        self.messages = [{'user_id': 111, 'ts': 1000, 'thread_key': 'obj:OBJ-1', 'text': 'нужен материал'}]
        self.assertEqual(self.handlers.get_unread_count(user={'id': 222}, role='worker')['unread'], 1)
        self.assertEqual(self.handlers.get_unread_count(user={'id': 333}, role='worker')['unread'], 0)

    def test_object_thread_does_not_leak_into_group_count(self):
        self.messages = [{'user_id': 111, 'ts': 1000, 'thread_key': 'obj:OBJ-1', 'text': 'X'}]
        self.reads = {'222': {'group': 999999}}
        self.assertEqual(self.handlers.get_unread_count(user={'id': 222}, role='worker')['unread'], 1)

    def test_muted_thread_excluded_from_count(self):
        self.messages = [{'user_id': 111, 'ts': 1000, 'thread_key': 'mangel:T-1', 'text': 'статус изменён'}]
        self.meta = {'mangel:T-1': {'user_prefs': {'222': {'muted': True}}}}
        # The product rule grants active workers access to defect chat.
        self.assertEqual(self.handlers.get_unread_count(user={'id': 222}, role='worker')['unread'], 0)

    def test_dm_message_only_counts_for_recipient(self):
        self.messages = [{'user_id': 111, 'to_user_id': '222', 'ts': 1000, 'text': 'личное'}]
        self.assertEqual(self.handlers.get_unread_count(user={'id': 222}, role='worker')['unread'], 1)
        self.assertEqual(self.handlers.get_unread_count(user={'id': 333}, role='worker')['unread'], 0)

    def test_matches_unread_by_thread_counts_for_same_data(self):
        self.messages = [
            {'user_id': 111, 'ts': 1000, 'text': 'group msg'},
            {'user_id': 111, 'ts': 1001, 'thread_key': 'obj:OBJ-1', 'text': 'obj msg'},
            {'user_id': 111, 'to_user_id': '222', 'ts': 1002, 'text': 'dm msg'},
        ]
        total = self.handlers.get_unread_count(user={'id': 222}, role='worker')
        by_thread = self.handlers.get_unread_by_thread(user={'id': 222}, role='worker')
        self.assertEqual(total['unread'], sum(by_thread['unread_by_thread'].values()))


if __name__ == '__main__':
    unittest.main()
