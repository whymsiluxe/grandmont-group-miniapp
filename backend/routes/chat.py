"""Chat core: storage, thread access, read state, reactions and attachment upload.

The module owns the real implementation for the core chat endpoints extracted
from ``backend.main``.  Cross-domain collaborators are injected by the
composition root; this module never imports ``main``.
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

try:
    from ..services.chat_storage import ChatStorageDeps, ChatStorageService
except ImportError:
    from services.chat_storage import ChatStorageDeps, ChatStorageService  # noqa: E402


@dataclass(frozen=True)
class ChatRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    chat_file: Callable[[], str]
    chat_archive_file: Callable[[], str]
    chat_reads_file: Callable[[], str]
    chat_thread_meta_file: Callable[[], str]
    chat_reactions_file: Callable[[], str]
    chat_attach_dir: Callable[[], str]
    chat_max: int
    chat_retention_seconds: int
    chat_reaction_options: tuple[str, ...] | list[str]
    thread_type_by_prefix: Mapping[str, str]
    default_thread_prefs: Mapping[str, Any]
    online_threshold_seconds: int
    load_roles: Callable[[], dict]
    load_assignments: Callable[[], dict]
    today_berlin_str: Callable[[], str]
    has_active_object_access: Callable[[str, str, str], bool]
    load_mangel_lib: Callable[[], Any]
    load_tasks: Callable[[], list]
    cached_get_used_range: Callable[[str], Any]
    load_worker_profiles: Callable[[], dict]
    last_seen_for: Callable[[str], Any]
    sanitize_display_name: Callable[[Any, str], str]
    resolve_current_display_name: Callable[[Any, Any], str]
    sniff_chat_attachment: Callable[[bytes], Any]


@dataclass(frozen=True)
class ChatHandlers:
    chat_lock: threading.Lock
    archive_chat_messages: Callable[[list], None]
    load_chat: Callable[[], list]
    save_chat: Callable[[list], None]
    purge_old_chat: Callable[[list], list]
    load_reads: Callable[[], dict]
    save_reads: Callable[[dict], None]
    load_chat_thread_meta: Callable[[], dict]
    save_chat_thread_meta: Callable[[dict], None]
    load_chat_reactions: Callable[[], list]
    save_chat_reactions: Callable[[list], None]
    reactions_summary_for_message: Callable[[list, str, str], list]
    chat_thread_id: Callable[[str, str | None], str]
    reject_self_chat: Callable[[Any, str | None], None]
    validate_dm_recipient: Callable[[str | None], None]
    object_chat_participants: Callable[[str], list]
    mangel_chat_participants: Callable[[str], list]
    task_chat_participants: Callable[[str], list]
    check_thread_access: Callable[[str, str, str], None]
    thread_title: Callable[[str], str]
    message_preview: Callable[[dict | None], str]
    thread_last_read: Callable[[dict, str, str], int]
    thread_user_prefs: Callable[[dict, str, str], dict]
    get_my_chat_threads: Callable[..., dict]
    get_normalized_chat_threads: Callable[..., dict]
    get_chat_messages: Callable[..., dict]
    get_unread_count: Callable[..., dict]
    get_unread_by_thread: Callable[..., dict]
    mark_chat_read: Callable[..., dict]
    post_chat_attachment: Callable[..., dict]
    delete_messages_by_ids: Callable[[set[str]], int]


def create_chat_router(deps: ChatRouteDeps) -> tuple[APIRouter, ChatHandlers]:
    router = APIRouter()
    storage = ChatStorageService(
        ChatStorageDeps(
            safe_load_json=deps.safe_load_json,
            atomic_write_json=deps.atomic_write_json,
            chat_file=deps.chat_file,
            chat_archive_file=deps.chat_archive_file,
            chat_reactions_file=deps.chat_reactions_file,
            chat_attach_dir=deps.chat_attach_dir,
            chat_max=deps.chat_max,
            chat_retention_seconds=deps.chat_retention_seconds,
        )
    )
    chat_lock = storage.lock
    archive_chat_messages = storage.archive_messages
    load_chat = storage.load_chat
    save_chat = storage.save_chat
    purge_old_chat = storage.purge_old_chat

    def load_reads() -> dict:
        return deps.safe_load_json(deps.chat_reads_file(), {})

    def save_reads(reads: dict):
        deps.atomic_write_json(deps.chat_reads_file(), reads)

    def load_chat_thread_meta() -> dict:
        return deps.safe_load_json(deps.chat_thread_meta_file(), {})

    def save_chat_thread_meta(meta: dict):
        deps.atomic_write_json(deps.chat_thread_meta_file(), meta)

    def load_chat_reactions() -> list:
        return deps.safe_load_json(deps.chat_reactions_file(), [])

    def save_chat_reactions(reactions: list):
        deps.atomic_write_json(deps.chat_reactions_file(), reactions)

    def reactions_summary_for_message(reactions: list, message_id: str, my_id: str) -> list:
        by_type = {}
        for reaction in reactions:
            if reaction['message_id'] != message_id:
                continue
            entry = by_type.setdefault(
                reaction['reaction'],
                {'reaction': reaction['reaction'], 'count': 0, 'mine': False},
            )
            entry['count'] += 1
            if str(reaction['user_id']) == my_id:
                entry['mine'] = True
        return [by_type[name] for name in deps.chat_reaction_options if name in by_type]

    def chat_thread_id(user_id: str, to_user_id: str | None) -> str:
        if not to_user_id:
            return 'group'
        return '-'.join(sorted([str(user_id), str(to_user_id)]))

    def reject_self_chat(user_id, to_user_id: str | None):
        if to_user_id and str(to_user_id) == str(user_id):
            raise HTTPException(400, "Нельзя написать самому себе")

    def validate_dm_recipient(to_user_id: str | None):
        if not to_user_id:
            return
        roles = deps.load_roles()
        if str(to_user_id) not in roles:
            raise HTTPException(404, "Пользователь не найден или доступ отозван")

    def object_chat_participants(object_id: str) -> list:
        roles = deps.load_roles()
        owner_id = next((uid for uid, role in roles.items() if role == 'owner'), None)
        assignments = deps.load_assignments().get(object_id, [])
        candidate_ids = {str(a['user_id']) for a in assignments}
        today = deps.today_berlin_str()
        worker_ids = {
            uid for uid in candidate_ids
            if deps.has_active_object_access(uid, object_id, today)
        }
        if owner_id:
            worker_ids.add(str(owner_id))
        return list(worker_ids)

    def mangel_chat_participants(ticket_id: str) -> list:
        roles = deps.load_roles()
        participants = {str(uid) for uid, role in roles.items() if role in ('owner', 'worker')}
        try:
            ticket = deps.load_mangel_lib().get_ticket(ticket_id)
            if ticket.get('assigned_worker_id'):
                participants.add(str(ticket['assigned_worker_id']))
            if ticket.get('created_by'):
                participants.add(str(ticket['created_by']))
        except Exception:
            pass
        return list(participants)

    def task_chat_participants(task_id: str) -> list:
        roles = deps.load_roles()
        owner_id = next((uid for uid, role in roles.items() if role == 'owner'), None)
        participants = {str(owner_id)} if owner_id else set()
        task = next((item for item in deps.load_tasks() if item['id'] == task_id), None)
        if task and task.get('from_user_id'):
            participants.add(str(task['from_user_id']))
        object_id = task.get('object_id') if task else ''
        if object_id:
            today = deps.today_berlin_str()
            for assignment in deps.load_assignments().get(str(object_id), []):
                status = assignment.get('status') or 'accepted'
                if status != 'accepted':
                    continue
                date_from = assignment.get('date_from', '')
                date_to = assignment.get('date_to', '')
                if (not (date_from and date_to)) or (date_from <= today <= date_to):
                    participants.add(str(assignment.get('user_id')))
        return list(participants)

    def check_thread_access(thread_id: str, uid: str, role: str):
        if thread_id.startswith('obj:'):
            object_id = thread_id[len('obj:'):]
            if uid not in object_chat_participants(object_id) and role != 'owner':
                raise HTTPException(403, "Нет доступа к чату этого объекта")
        elif thread_id.startswith('mangel:'):
            ticket_id = thread_id[len('mangel:'):]
            if uid not in mangel_chat_participants(ticket_id) and role != 'owner':
                raise HTTPException(403, "Нет доступа к чату этого тикета")
        elif thread_id.startswith('task:'):
            task_id = thread_id[len('task:'):]
            if uid not in task_chat_participants(task_id) and role != 'owner':
                raise HTTPException(403, "Нет доступа к чату этой потребности")

    def thread_title(thread_key: str) -> str:
        if thread_key.startswith('obj:'):
            object_id = thread_key[len('obj:'):]
            try:
                rows = deps.cached_get_used_range('Объекты')
                if rows:
                    header, data = rows[0], rows[1:]
                    for row_values in data:
                        row = dict(zip(header, row_values))
                        if str(row.get('ID объекта', '')) == object_id:
                            return f"Объект: {row.get('Объект', object_id)}"
            except Exception:
                pass
            return f"Объект: {object_id}"
        if thread_key.startswith('mangel:'):
            ticket_id = thread_key[len('mangel:'):]
            try:
                ticket = deps.load_mangel_lib().get_ticket(ticket_id)
                return f"Тикет: {ticket.get('object_id', ticket_id)}"
            except Exception:
                return f"Тикет: {ticket_id}"
        if thread_key.startswith('task:'):
            task_id = thread_key[len('task:'):]
            task = next((item for item in deps.load_tasks() if item['id'] == task_id), None)
            return f"Потребность: {task['title']}" if task else f"Потребность: {task_id}"
        return thread_key

    def message_preview(msg: dict | None) -> str:
        if not msg:
            return ''
        if msg.get('text'):
            return msg['text']
        if msg.get('location'):
            return '📍 Геолокация'
        attachment = msg.get('attachment') or {}
        if (attachment.get('content_type') or '').startswith('audio'):
            return '🎤 Голосовое'
        if attachment:
            return '📎 Файл'
        return ''

    def thread_last_read(reads: dict, my_id: str, thread_key: str) -> int:
        user_reads = reads.get(my_id, {})
        if isinstance(user_reads, (int, float)):
            return int(user_reads)
        return int(user_reads.get(thread_key, 0))

    def thread_user_prefs(meta: dict, thread_id: str, uid: str) -> dict:
        prefs = meta.get(thread_id, {}).get('user_prefs', {}).get(uid)
        base = dict(deps.default_thread_prefs)
        if prefs:
            base.update(prefs)
        return base

    @router.get('/api/chat/my_threads')
    def get_my_chat_threads(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        uid = str(user['id'])
        messages = load_chat()
        keys = {m['thread_key'] for m in messages if m.get('thread_key')}
        result = []
        for key in keys:
            try:
                check_thread_access(key, uid, role)
            except HTTPException:
                continue
            thread_messages = [m for m in messages if m.get('thread_key') == key]
            last = max(thread_messages, key=lambda m: m['ts']) if thread_messages else None
            result.append({
                'thread_key': key,
                'title': thread_title(key),
                'last_ts': last['ts'] if last else 0,
                'last_preview': message_preview(last),
            })
        result.sort(key=lambda item: item['last_ts'], reverse=True)
        return {'threads': result}

    @router.get('/api/chat/threads')
    def get_normalized_chat_threads(
        type: str = '',
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        uid = str(user['id'])
        with chat_lock:
            messages = purge_old_chat(load_chat())
            save_chat(messages)
        reads = load_reads()
        meta = load_chat_thread_meta()
        profiles = deps.load_worker_profiles()
        roles = deps.load_roles()

        def last_message_field(last):
            return {
                'text': message_preview(last),
                'ts': last['ts'],
                'sender_id': str(last['user_id']),
            } if last else None

        threads = []
        if not type or type == 'GENERAL':
            group_messages = [m for m in messages if not m.get('to_user_id') and not m.get('thread_key')]
            last = max(group_messages, key=lambda m: m['ts']) if group_messages else None
            unread = sum(
                1 for m in group_messages
                if str(m.get('user_id')) != uid and m['ts'] > thread_last_read(reads, uid, 'group')
            )
            prefs = thread_user_prefs(meta, 'group', uid)
            threads.append({
                'id': 'group', 'type': 'GENERAL', 'title': 'Общий чат', 'avatar_url': None,
                'subtitle': message_preview(last), 'last_message': last_message_field(last),
                'unread_count': unread, **prefs, 'version': last['ts'] if last else 0,
            })

        if not type or type == 'DIRECT':
            for worker_id in (set(roles.keys()) | set(profiles.keys())):
                if worker_id == uid:
                    continue
                dm_messages = [m for m in messages if not m.get('thread_key') and (
                    (str(m.get('user_id')) == uid and str(m.get('to_user_id')) == worker_id)
                    or (str(m.get('user_id')) == worker_id and str(m.get('to_user_id')) == uid)
                )]
                last = max(dm_messages, key=lambda m: m['ts']) if dm_messages else None
                thread_id = chat_thread_id(uid, worker_id)
                unread = sum(
                    1 for m in dm_messages
                    if str(m.get('user_id')) != uid and m['ts'] > thread_last_read(reads, uid, worker_id)
                )
                prefs = thread_user_prefs(meta, thread_id, uid)
                profile = profiles.get(worker_id, {})
                last_seen = deps.last_seen_for(worker_id)
                threads.append({
                    'id': worker_id,
                    'type': 'DIRECT',
                    'title': deps.sanitize_display_name(profile.get('name'), worker_id),
                    'avatar_url': f'/api/profile/{worker_id}/avatar' if profile.get('avatar') else None,
                    'subtitle': message_preview(last) or ('Владелец' if roles.get(worker_id) == 'owner' else 'Работник'),
                    'online': bool(last_seen and (time.time() - last_seen) < deps.online_threshold_seconds),
                    'last_message': last_message_field(last),
                    'unread_count': unread,
                    **prefs,
                    'version': last['ts'] if last else 0,
                })

        if not type or type in ('OBJECT', 'DEFECT', 'TASK'):
            keys = {m['thread_key'] for m in messages if m.get('thread_key')}
            for key in keys:
                thread_type = next(
                    (value for prefix, value in deps.thread_type_by_prefix.items() if key.startswith(prefix)),
                    None,
                )
                if not thread_type or (type and type != thread_type):
                    continue
                try:
                    check_thread_access(key, uid, role)
                except HTTPException:
                    continue
                thread_messages = [m for m in messages if m.get('thread_key') == key]
                last = max(thread_messages, key=lambda m: m['ts']) if thread_messages else None
                unread = sum(
                    1 for m in thread_messages
                    if str(m.get('user_id')) != uid and m['ts'] > thread_last_read(reads, uid, key)
                )
                prefs = thread_user_prefs(meta, key, uid)
                threads.append({
                    'id': key,
                    'type': thread_type,
                    'title': thread_title(key),
                    'avatar_url': None,
                    'subtitle': message_preview(last),
                    'last_message': last_message_field(last),
                    'unread_count': unread,
                    **prefs,
                    'version': last['ts'] if last else 0,
                })

        threads.sort(key=lambda item: item['last_message']['ts'] if item['last_message'] else 0, reverse=True)
        return {'threads': threads}

    @router.get('/api/chat/messages')
    def get_chat_messages(
        with_: str = '',
        thread_key: str = '',
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        with chat_lock:
            messages = purge_old_chat(load_chat())
            save_chat(messages)
        if thread_key:
            check_thread_access(thread_key, str(user['id']), role)
            messages = [m for m in messages if m.get('thread_key') == thread_key]
        elif with_:
            me = str(user['id'])
            messages = [m for m in messages if
                        (str(m.get('user_id')) == me and str(m.get('to_user_id')) == with_)
                        or (str(m.get('user_id')) == with_ and str(m.get('to_user_id')) == me)]
        else:
            messages = [m for m in messages if not m.get('to_user_id') and not m.get('thread_key')]

        reactions = load_chat_reactions()
        my_id = str(user['id'])
        for message in messages:
            message['reactions'] = reactions_summary_for_message(reactions, message['id'], my_id)
            if message.get('user_id') != 'system':
                message['name'] = deps.resolve_current_display_name(message.get('user_id'), message.get('name'))

        if with_:
            other_reads = load_reads().get(with_, {})
            other_last_read = other_reads.get(my_id, 0) if isinstance(other_reads, dict) else int(other_reads or 0)
            for message in messages:
                if str(message.get('user_id')) == my_id:
                    message['read_by_recipient'] = message.get('ts', 0) <= other_last_read
        return {'messages': messages}

    def _unread(user: dict, role: str, by_thread: bool):
        with chat_lock:
            messages = load_chat()
            reads = load_reads()
        meta = load_chat_thread_meta()
        me = str(user['id'])
        result = {} if by_thread else 0
        for message in messages:
            if str(message.get('user_id')) == me:
                continue
            thread_key_value = message.get('thread_key')
            if thread_key_value:
                try:
                    check_thread_access(thread_key_value, me, role)
                except HTTPException:
                    continue
                thread_key = thread_key_value
                prefs_id = thread_key_value
            else:
                to_uid = message.get('to_user_id')
                if to_uid and str(to_uid) != me:
                    continue
                thread_key = 'group' if not to_uid else str(message['user_id'])
                prefs_id = thread_key if not to_uid else chat_thread_id(me, thread_key)
            if message.get('ts', 0) <= thread_last_read(reads, me, thread_key):
                continue
            if thread_user_prefs(meta, prefs_id, me).get('muted'):
                continue
            if by_thread:
                result[thread_key] = result.get(thread_key, 0) + 1
            else:
                result += 1
        return result

    @router.get('/api/chat/unread_count')
    def get_unread_count(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        return {'unread': _unread(user, role, False)}

    @router.get('/api/chat/unread_by_thread')
    def get_unread_by_thread(
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        return {'unread_by_thread': _unread(user, role, True)}

    @router.post('/api/chat/read')
    def mark_chat_read(
        with_: str = '',
        thread_key: str = '',
        user: dict = Depends(deps.get_current_user),
    ):
        key = thread_key or ('group' if not with_ else with_)
        with chat_lock:
            reads = load_reads()
            my_id = str(user['id'])
            if not isinstance(reads.get(my_id), dict):
                reads[my_id] = {}
            reads[my_id][key] = int(time.time())
            save_reads(reads)
        return {'ok': True}

    @router.post('/api/chat/messages/attachment')
    def post_chat_attachment(
        thread_key: str = Form(''),
        to_user_id: str = Form(''),
        file: UploadFile = File(...),
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if thread_key:
            check_thread_access(thread_key, str(user['id']), role)
        else:
            reject_self_chat(user['id'], to_user_id or None)
            validate_dm_recipient(to_user_id or None)
            thread_id = chat_thread_id(user['id'], to_user_id or None)
            thread_meta = load_chat_thread_meta()
            if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
                raise HTTPException(403, "Чат закрыт руководством")

        data = file.file.read()
        if len(data) > 8 * 1024 * 1024:
            raise HTTPException(400, "Файл слишком большой (макс. 8 МБ)")
        sniffed = deps.sniff_chat_attachment(data)
        if sniffed is None:
            raise HTTPException(400, "Недопустимый тип файла")
        _, extension = sniffed
        filename = f'{uuid.uuid4().hex}.{extension}'
        message = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': '',
            'to_user_id': to_user_id or None,
            'thread_key': thread_key or None,
            'attachment': {
                'file': filename,
                'name': file.filename or filename,
                'content_type': file.content_type or '',
            },
        }
        storage.persist_attachment_and_message(
            filename=filename,
            data=data,
            message=message,
        )
        return {'message': message}

    return router, ChatHandlers(
        chat_lock=chat_lock,
        archive_chat_messages=archive_chat_messages,
        load_chat=load_chat,
        save_chat=save_chat,
        purge_old_chat=purge_old_chat,
        load_reads=load_reads,
        save_reads=save_reads,
        load_chat_thread_meta=load_chat_thread_meta,
        save_chat_thread_meta=save_chat_thread_meta,
        load_chat_reactions=load_chat_reactions,
        save_chat_reactions=save_chat_reactions,
        reactions_summary_for_message=reactions_summary_for_message,
        chat_thread_id=chat_thread_id,
        reject_self_chat=reject_self_chat,
        validate_dm_recipient=validate_dm_recipient,
        object_chat_participants=object_chat_participants,
        mangel_chat_participants=mangel_chat_participants,
        task_chat_participants=task_chat_participants,
        check_thread_access=check_thread_access,
        thread_title=thread_title,
        message_preview=message_preview,
        thread_last_read=thread_last_read,
        thread_user_prefs=thread_user_prefs,
        get_my_chat_threads=get_my_chat_threads,
        get_normalized_chat_threads=get_normalized_chat_threads,
        get_chat_messages=get_chat_messages,
        get_unread_count=get_unread_count,
        get_unread_by_thread=get_unread_by_thread,
        mark_chat_read=mark_chat_read,
        post_chat_attachment=post_chat_attachment,
        delete_messages_by_ids=storage.delete_messages_by_ids,
    )
