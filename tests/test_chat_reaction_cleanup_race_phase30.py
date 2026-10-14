import copy
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from services.chat_storage import ChatStorageDeps, ChatStorageService


def test_message_delete_cleanup_preserves_concurrent_reaction_to_other_message():
    with tempfile.TemporaryDirectory() as tmp:
        chat_file = os.path.join(tmp, 'chat.json')
        archive_file = os.path.join(tmp, 'archive.json')
        reactions_file = os.path.join(tmp, 'reactions.json')
        attach_dir = os.path.join(tmp, 'attachments')
        os.makedirs(attach_dir, exist_ok=True)

        stores = {
            chat_file: [
                {'id': 'msg-a', 'ts': int(time.time())},
                {'id': 'msg-b', 'ts': int(time.time())},
            ],
            archive_file: [],
            reactions_file: [
                {'message_id': 'msg-a', 'user_id': '1', 'reaction': '👍'},
            ],
        }
        delete_loaded_reactions = threading.Event()

        def safe_load_json(path, default):
            value = copy.deepcopy(stores.get(path, default))
            if path == reactions_file and threading.current_thread().name == 'deleter':
                delete_loaded_reactions.set()
                # Open the stale-snapshot window.  Under the fixed code the
                # competing mutation cannot enter because delete still owns the
                # shared chat lock.  Before Phase 30 it could enter here and its
                # write would then be overwritten by delete cleanup.
                time.sleep(0.08)
            return value

        def atomic_write_json(path, value):
            stores[path] = copy.deepcopy(value)

        service = ChatStorageService(ChatStorageDeps(
            safe_load_json=safe_load_json,
            atomic_write_json=atomic_write_json,
            chat_file=lambda: chat_file,
            chat_archive_file=lambda: archive_file,
            chat_reactions_file=lambda: reactions_file,
            chat_attach_dir=lambda: attach_dir,
            chat_max=100,
            chat_retention_seconds=86400,
        ))

        delete_result = {}

        def delete_message():
            delete_result['count'] = service.delete_messages_by_ids({'msg-a'})

        def add_other_reaction():
            assert delete_loaded_reactions.wait(timeout=1)
            with service.lock:
                reactions = service.load_reactions()
                reactions.append({
                    'message_id': 'msg-b',
                    'user_id': '2',
                    'reaction': '❤️',
                })
                service.save_reactions(reactions)

        deleter = threading.Thread(target=delete_message, name='deleter')
        reactor = threading.Thread(target=add_other_reaction, name='reactor')
        deleter.start()
        reactor.start()
        deleter.join(timeout=2)
        reactor.join(timeout=2)

        assert not deleter.is_alive()
        assert not reactor.is_alive()
        assert delete_result['count'] == 1
        assert stores[chat_file] == [{'id': 'msg-b', 'ts': stores[chat_file][0]['ts']}]
        assert stores[archive_file][0]['id'] == 'msg-a'
        assert stores[reactions_file] == [
            {'message_id': 'msg-b', 'user_id': '2', 'reaction': '❤️'},
        ]


def test_reaction_cleanup_remains_best_effort_after_durable_message_delete():
    with tempfile.TemporaryDirectory() as tmp:
        chat_file = os.path.join(tmp, 'chat.json')
        archive_file = os.path.join(tmp, 'archive.json')
        reactions_file = os.path.join(tmp, 'reactions.json')
        attach_dir = os.path.join(tmp, 'attachments')
        os.makedirs(attach_dir, exist_ok=True)

        stores = {
            chat_file: [{'id': 'msg-a', 'ts': 1}],
            archive_file: [],
            reactions_file: [{'message_id': 'msg-a', 'user_id': '1', 'reaction': '👍'}],
        }

        def safe_load_json(path, default):
            return copy.deepcopy(stores.get(path, default))

        def atomic_write_json(path, value):
            if path == reactions_file:
                raise OSError('reaction store unavailable')
            stores[path] = copy.deepcopy(value)

        service = ChatStorageService(ChatStorageDeps(
            safe_load_json=safe_load_json,
            atomic_write_json=atomic_write_json,
            chat_file=lambda: chat_file,
            chat_archive_file=lambda: archive_file,
            chat_reactions_file=lambda: reactions_file,
            chat_attach_dir=lambda: attach_dir,
            chat_max=100,
            chat_retention_seconds=86400,
        ))

        assert service.delete_messages_by_ids({'msg-a'}) == 1
        assert stores[chat_file] == []
        assert stores[archive_file][0]['id'] == 'msg-a'
        # Cleanup failed, but the durable delete must still report success.
        assert stores[reactions_file][0]['message_id'] == 'msg-a'
