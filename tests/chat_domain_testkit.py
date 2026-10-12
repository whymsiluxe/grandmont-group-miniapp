"""Reusable isolated harness for extracted chat-domain behavior tests."""
from __future__ import annotations

import copy
import os
import tempfile
from dataclasses import dataclass

from routes.chat import ChatRouteDeps, create_chat_router
from routes.chat_actions import ChatActionsRouteDeps, create_chat_actions_router


class _MangelLib:
    def __init__(self, tickets):
        self.tickets = tickets

    def get_ticket(self, ticket_id):
        if ticket_id not in self.tickets:
            raise KeyError(ticket_id)
        return copy.deepcopy(self.tickets[ticket_id])


@dataclass
class ChatDomainHarness:
    roles: dict | None = None
    assignments: dict | None = None
    tasks: list | None = None
    mangel_tickets: dict | None = None
    objects: dict | None = None
    worker_profiles: dict | None = None

    def __post_init__(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chat-domain-')
        root = self.tmp.name
        self.paths = {
            'chat': os.path.join(root, 'chat.json'),
            'archive': os.path.join(root, 'archive.json'),
            'reads': os.path.join(root, 'reads.json'),
            'meta': os.path.join(root, 'meta.json'),
            'reactions': os.path.join(root, 'reactions.json'),
        }
        self.attach_dir = os.path.join(root, 'attachments')
        os.makedirs(self.attach_dir, exist_ok=True)
        self.store = {
            self.paths['chat']: [],
            self.paths['archive']: [],
            self.paths['reads']: {},
            self.paths['meta']: {},
            self.paths['reactions']: [],
        }
        self.roles = copy.deepcopy(self.roles or {
            '1': 'owner', '10': 'worker', '20': 'worker', '30': 'worker',
        })
        self.assignments = copy.deepcopy(self.assignments or {})
        self.tasks = copy.deepcopy(self.tasks or [])
        self.mangel_tickets = copy.deepcopy(self.mangel_tickets or {})
        self.objects = copy.deepcopy(self.objects or {})
        self.worker_profiles = copy.deepcopy(self.worker_profiles or {})
        self.history = []
        self.telegram = []

        def safe_load(path, default):
            return copy.deepcopy(self.store.get(path, default))

        def atomic_write(path, value):
            self.store[path] = copy.deepcopy(value)

        def has_access(uid, object_id, today):
            for assignment in self.assignments.get(str(object_id), []):
                if str(assignment.get('user_id')) != str(uid):
                    continue
                if (assignment.get('status') or 'accepted') != 'accepted':
                    continue
                date_from = assignment.get('date_from', '')
                date_to = assignment.get('date_to', '')
                if not (date_from and date_to) or date_from <= today <= date_to:
                    return True
            return False

        def resolve_current_display_name(uid, fallback):
            profile = self.worker_profiles.get(str(uid), {})
            current_name = profile.get('name')
            if current_name:
                return str(current_name)
            return str(fallback or uid)

        _, self.core = create_chat_router(ChatRouteDeps(
            get_current_user=lambda: {},
            get_role=lambda: 'worker',
            safe_load_json=safe_load,
            atomic_write_json=atomic_write,
            chat_file=lambda: self.paths['chat'],
            chat_archive_file=lambda: self.paths['archive'],
            chat_reads_file=lambda: self.paths['reads'],
            chat_thread_meta_file=lambda: self.paths['meta'],
            chat_reactions_file=lambda: self.paths['reactions'],
            chat_attach_dir=lambda: self.attach_dir,
            chat_max=200,
            chat_retention_seconds=7 * 24 * 3600,
            chat_reaction_options=('👍', '👀', '✅', '❤️'),
            thread_type_by_prefix={'obj:': 'OBJECT', 'mangel:': 'DEFECT', 'task:': 'TASK'},
            default_thread_prefs={'muted': False, 'pinned': False, 'archived': False},
            online_threshold_seconds=120,
            load_roles=lambda: copy.deepcopy(self.roles),
            load_assignments=lambda: copy.deepcopy(self.assignments),
            today_berlin_str=lambda: '2026-10-12',
            has_active_object_access=has_access,
            load_mangel_lib=lambda: _MangelLib(self.mangel_tickets),
            load_tasks=lambda: copy.deepcopy(self.tasks),
            cached_get_used_range=lambda tab: [],
            load_worker_profiles=lambda: copy.deepcopy(self.worker_profiles),
            last_seen_for=lambda uid: None,
            sanitize_display_name=lambda raw, fallback: raw or fallback,
            resolve_current_display_name=resolve_current_display_name,
            sniff_chat_attachment=lambda data: ('image/jpeg', 'jpg') if data.startswith(b'\xff\xd8\xff') else None,
        ))

        _, self.actions = create_chat_actions_router(ChatActionsRouteDeps(
            get_current_user=lambda: {},
            get_role=lambda: 'worker',
            require_owner=lambda: None,
            core=self.core,
            safe_load_json=safe_load,
            chat_archive_file=lambda: self.paths['archive'],
            chat_attach_dir=lambda: self.attach_dir,
            chat_reaction_options=('👍', '👀', '✅', '❤️'),
            load_roles=lambda: copy.deepcopy(self.roles),
            send_telegram_message=lambda chat_id, text: self.telegram.append((chat_id, text)),
            find_object_row_by_id=lambda object_id: copy.deepcopy(self.objects.get(str(object_id))),
            append_object_history_best_effort=lambda *args, **kwargs: self.history.append((args, kwargs)),
        ))

    def close(self):
        self.tmp.cleanup()

    @property
    def messages(self):
        return self.store[self.paths['chat']]

    @messages.setter
    def messages(self, value):
        self.store[self.paths['chat']] = copy.deepcopy(value)

    @property
    def archive(self):
        return self.store[self.paths['archive']]

    @archive.setter
    def archive(self, value):
        self.store[self.paths['archive']] = copy.deepcopy(value)

    @property
    def reactions(self):
        return self.store[self.paths['reactions']]

    @reactions.setter
    def reactions(self, value):
        self.store[self.paths['reactions']] = copy.deepcopy(value)

    def grant_object(self, object_id, *user_ids):
        self.assignments[str(object_id)] = [
            {'id': f'{object_id}-{uid}', 'user_id': str(uid), 'status': 'accepted'}
            for uid in user_ids
        ]

    def create_attachment(self, filename='photo1.jpg', data=b'fake-image'):
        path = os.path.join(self.attach_dir, filename)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path
