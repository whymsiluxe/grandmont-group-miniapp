"""Chat write/action routes extracted from ``backend.main``.

This module builds on the storage/access primitives exposed by ``ChatHandlers``
from routes.chat.  Cross-domain integrations are injected by main.py; no import
back to the monolith is allowed.
"""
from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .chat import ChatHandlers

try:
    from ..core.media_paths import resolve_owned_media_path
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402


class ChatMessageBody(BaseModel):
    text: str = ''
    to_user_id: str | None = None
    thread_key: str | None = None
    reply_to_id: str | None = None
    location: dict | None = None


class BroadcastBody(BaseModel):
    scope: str
    text: str
    object_id: str | None = None


class ChatReactionBody(BaseModel):
    reaction: str


class ChatThreadCloseBody(BaseModel):
    to_user_id: str | None = None


class ChatThreadPrefsBody(BaseModel):
    to_user_id: str | None = None
    thread_key: str | None = None
    muted: bool | None = None
    pinned: bool | None = None
    archived: bool | None = None


@dataclass(frozen=True)
class ChatActionsRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    require_owner: Callable[..., None]
    core: ChatHandlers
    safe_load_json: Callable[[str, Any], Any]
    chat_archive_file: Callable[[], str]
    chat_attach_dir: Callable[[], str]
    chat_reaction_options: tuple[str, ...] | list[str]
    load_roles: Callable[[], dict]
    send_telegram_message: Callable[[int, str], Any]
    find_object_row_by_id: Callable[[str], dict | None]
    append_object_history_best_effort: Callable[..., Any]


@dataclass(frozen=True)
class ChatActionsHandlers:
    normalize_chat_location: Callable[[dict | None], dict | None]
    reply_snapshot: Callable[[dict], dict]
    chat_thread_participants: Callable[[str], list]
    check_message_access: Callable[[dict, str, str], None]
    get_chat_attachment: Callable[..., Any]
    send_manager_broadcast: Callable[..., dict]
    post_chat_message: Callable[..., dict]
    forward_chat_message: Callable[..., dict]
    delete_chat_message: Callable[..., dict]
    delete_chat_thread: Callable[..., dict]
    toggle_chat_reaction: Callable[..., dict]
    set_chat_thread_prefs: Callable[..., dict]
    close_chat_thread: Callable[..., dict]
    reopen_chat_thread: Callable[..., dict]
    get_chat_thread_status: Callable[..., dict]


def create_chat_actions_router(
    deps: ChatActionsRouteDeps,
) -> tuple[APIRouter, ChatActionsHandlers]:
    router = APIRouter()
    core = deps.core

    def normalize_chat_location(raw: dict | None) -> dict | None:
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise HTTPException(400, "Некорректная геолокация")
        try:
            lat = float(raw.get('lat'))
            lon = float(raw.get('lon'))
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "Некорректная геолокация") from exc
        if not math.isfinite(lat) or not math.isfinite(lon) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
            raise HTTPException(400, "Некорректная геолокация")
        normalized = {'lat': round(lat, 6), 'lon': round(lon, 6)}
        if raw.get('accuracy') is not None:
            try:
                accuracy = float(raw.get('accuracy'))
            except (TypeError, ValueError):
                accuracy = None
            if accuracy is not None and math.isfinite(accuracy) and accuracy >= 0:
                normalized['accuracy'] = round(accuracy, 1)
        label = str(raw.get('label') or '').strip()
        if label:
            normalized['label'] = label[:80]
        return normalized

    def reply_snapshot(msg: dict) -> dict:
        return {
            'id': msg['id'],
            'name': msg.get('name', ''),
            'preview': core.message_preview(msg)[:200],
        }

    def chat_thread_participants(thread_id: str) -> list:
        if thread_id == 'group':
            return list(deps.load_roles().keys())
        return thread_id.split('-')

    def check_message_access(msg: dict, uid: str, role: str):
        thread_key = msg.get('thread_key')
        if thread_key:
            core.check_thread_access(thread_key, uid, role)
            return
        thread_id = core.chat_thread_id(msg['user_id'], msg.get('to_user_id'))
        if uid not in chat_thread_participants(thread_id) and role != 'owner':
            raise HTTPException(403, "Нет доступа к этому сообщению")

    @router.get('/api/chat/attachments/{fname}')
    def get_chat_attachment(
        fname: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        safe_fname = os.path.basename(fname)
        if safe_fname != fname:
            raise HTTPException(404, "Файл не найден")

        messages = core.load_chat() + deps.safe_load_json(deps.chat_archive_file(), [])
        candidates = [
            msg for msg in messages
            if isinstance(msg, dict)
            and (msg.get('attachment') or {}).get('file') == safe_fname
        ]
        if not candidates:
            raise HTTPException(404, "Файл не найден")

        if role != 'owner':
            uid = str(user['id'])
            allowed = False
            for msg in candidates:
                try:
                    check_message_access(msg, uid, role)
                    allowed = True
                    break
                except HTTPException:
                    continue
            if not allowed:
                raise HTTPException(403, "Нет доступа к этому файлу")

        path = resolve_owned_media_path(deps.chat_attach_dir(), safe_fname)
        if not path or not os.path.isfile(path):
            raise HTTPException(404, "Файл не найден")
        return FileResponse(path, headers={'X-Content-Type-Options': 'nosniff'})

    @router.post('/api/manager/broadcast')
    def send_manager_broadcast(
        body: BroadcastBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        scope = str(body.scope or '').strip().lower()
        text = str(body.text or '').strip()
        if scope not in ('company', 'object'):
            raise HTTPException(400, "scope должен быть company или object")
        if not text:
            raise HTTPException(400, "Текст объявления пустой")
        if len(text) > 1000:
            raise HTTPException(400, "Объявление слишком длинное (макс. 1000 символов)")

        object_id = ''
        object_name = ''
        thread_key = None
        if scope == 'object':
            object_id = str(body.object_id or '').strip()
            if not object_id:
                raise HTTPException(400, "Укажите object_id для объявления по объекту")
            object_row = deps.find_object_row_by_id(object_id)
            if object_row is None:
                raise HTTPException(404, "Объект не найден")
            object_name = (
                object_row.get('Объект')
                or object_row.get('Название')
                or object_row.get('Адрес')
                or object_id
            )
            thread_key = f'obj:{object_id}'
            recipients = sorted(core.object_chat_participants(object_id))
        else:
            recipients = sorted(str(uid) for uid in deps.load_roles().keys())

        msg = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': text,
            'to_user_id': None,
            'thread_key': thread_key,
            'broadcast': {
                'scope': scope,
                'object_id': object_id or None,
                'object_name': object_name or None,
                'audience_count': len(recipients),
            },
        }
        with core.chat_lock:
            messages = core.load_chat()
            messages.append(msg)
            core.save_chat(messages)

        if scope == 'object':
            deps.append_object_history_best_effort(
                object_id,
                'broadcast_sent',
                'Объявление отправлено',
                user=user,
                subtitle=text[:200],
                meta={
                    'message_id': msg['id'],
                    'thread_key': thread_key,
                    'audience_count': len(recipients),
                },
            )
        return {
            'status': 'ok',
            'thread_key': thread_key or 'group',
            'audience_count': len(recipients),
            'message': msg,
        }

    @router.post('/api/chat/messages')
    def post_chat_message(
        body: ChatMessageBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        text = body.text.strip()
        location = normalize_chat_location(body.location)
        if not text and not location:
            raise HTTPException(400, "Пустое сообщение")
        if len(text) > 1000:
            raise HTTPException(400, "Сообщение слишком длинное (макс. 1000 символов)")

        if body.thread_key:
            core.check_thread_access(body.thread_key, str(user['id']), role)
        else:
            core.reject_self_chat(user['id'], body.to_user_id)
            core.validate_dm_recipient(body.to_user_id)
            thread_id = core.chat_thread_id(user['id'], body.to_user_id)
            thread_meta = core.load_chat_thread_meta()
            if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
                raise HTTPException(403, "Чат закрыт руководством")

        snapshot = None
        if body.reply_to_id:
            with core.chat_lock:
                source = next((m for m in core.load_chat() if m['id'] == body.reply_to_id), None)
            if source is None:
                raise HTTPException(404, "Исходное сообщение не найдено")
            if (source.get('thread_key') or None) != (body.thread_key or None):
                raise HTTPException(403, "Нельзя цитировать сообщение из другого чата")
            if not body.thread_key:
                source_thread = core.chat_thread_id(source['user_id'], source.get('to_user_id'))
                new_thread = core.chat_thread_id(user['id'], body.to_user_id)
                if source_thread != new_thread:
                    raise HTTPException(403, "Нельзя цитировать сообщение из другого чата")
            snapshot = reply_snapshot(source)

        msg = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': text,
            'to_user_id': body.to_user_id,
            'thread_key': body.thread_key,
            'location': location,
            'reply_to': snapshot,
        }
        with core.chat_lock:
            messages = core.load_chat()
            messages.append(msg)
            core.save_chat(messages)
        return {'message': msg}

    @router.post('/api/chat/messages/{msg_id}/forward')
    def forward_chat_message(
        msg_id: str,
        body: ChatMessageBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        uid = str(user['id'])
        with core.chat_lock:
            source = next((m for m in core.load_chat() if m['id'] == msg_id), None)
        if source is None:
            raise HTTPException(404, "Сообщение не найдено")
        check_message_access(source, uid, role)

        if body.thread_key:
            core.check_thread_access(body.thread_key, uid, role)
        else:
            core.reject_self_chat(user['id'], body.to_user_id)
            core.validate_dm_recipient(body.to_user_id)
            thread_id = core.chat_thread_id(user['id'], body.to_user_id)
            thread_meta = core.load_chat_thread_meta()
            if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
                raise HTTPException(403, "Чат закрыт руководством")

        if not source.get('text') and not source.get('attachment') and not source.get('location'):
            raise HTTPException(400, "Нечего пересылать")
        msg = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': source.get('text', ''),
            'to_user_id': body.to_user_id,
            'thread_key': body.thread_key,
            'attachment': source.get('attachment'),
            'location': source.get('location'),
            'voice_transcript': source.get('voice_transcript'),
            'forwarded_from': source.get('name', ''),
        }
        with core.chat_lock:
            messages = core.load_chat()
            messages.append(msg)
            core.save_chat(messages)
        return {'message': msg}

    @router.delete('/api/chat/messages/{msg_id}')
    def delete_chat_message(
        msg_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        with core.chat_lock:
            messages = core.load_chat()
            target = next((m for m in messages if m['id'] == msg_id), None)
            if target is None:
                raise HTTPException(404, 'Сообщение не найдено')
            if role != 'owner' and target['user_id'] != user['id']:
                raise HTTPException(403, 'Можно удалять только свои сообщения')
        deleted = core.delete_messages_by_ids({msg_id})
        if not deleted:
            raise HTTPException(404, 'Сообщение не найдено')
        return {'status': 'ok'}

    @router.delete('/api/chat/threads')
    def delete_chat_thread(
        thread_key: str = '',
        with_: str = '',
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        if not thread_key and not with_:
            raise HTTPException(400, "Укажи thread_key или with_")
        with core.chat_lock:
            messages = core.load_chat()
            if thread_key:
                to_delete = [m for m in messages if m.get('thread_key') == thread_key]
            else:
                core.reject_self_chat(user['id'], with_)
                core.validate_dm_recipient(with_)
                target_thread_id = core.chat_thread_id(user['id'], with_)
                to_delete = [
                    m for m in messages
                    if not m.get('thread_key')
                    and core.chat_thread_id(m.get('user_id'), m.get('to_user_id')) == target_thread_id
                ]
        deleted_ids = {str(m['id']) for m in to_delete}
        deleted_count = core.delete_messages_by_ids(deleted_ids)
        return {'status': 'ok', 'deleted_count': deleted_count}

    @router.post('/api/chat/messages/{msg_id}/reactions')
    def toggle_chat_reaction(
        msg_id: str,
        body: ChatReactionBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if body.reaction not in deps.chat_reaction_options:
            raise HTTPException(400, "Недопустимая реакция")
        uid = str(user['id'])
        with core.chat_lock:
            messages = core.load_chat()
            msg = next((m for m in messages if m['id'] == msg_id), None)
            if msg is None:
                raise HTTPException(404, "Сообщение не найдено")
            check_message_access(msg, uid, role)
            reactions = core.load_chat_reactions()
            existing = next((
                r for r in reactions
                if r['message_id'] == msg_id
                and str(r['user_id']) == uid
                and r['reaction'] == body.reaction
            ), None)
            if existing:
                reactions.remove(existing)
            else:
                reactions.append({
                    'message_id': msg_id,
                    'user_id': uid,
                    'reaction': body.reaction,
                    'created_at': int(time.time()),
                })
            core.save_chat_reactions(reactions)
            summary = core.reactions_summary_for_message(reactions, msg_id, uid)
        return {'reactions': summary}

    @router.post('/api/chat/threads/prefs')
    def set_chat_thread_prefs(
        body: ChatThreadPrefsBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        uid = str(user['id'])
        if body.thread_key:
            core.check_thread_access(body.thread_key, uid, role)
            thread_id = body.thread_key
        else:
            core.reject_self_chat(user['id'], body.to_user_id)
            core.validate_dm_recipient(body.to_user_id)
            thread_id = core.chat_thread_id(user['id'], body.to_user_id)
        with core.chat_lock:
            meta = core.load_chat_thread_meta()
            thread_meta = meta.setdefault(thread_id, {})
            prefs_by_user = thread_meta.setdefault('user_prefs', {})
            current = core.thread_user_prefs(meta, thread_id, uid)
            if body.muted is not None:
                current['muted'] = body.muted
            if body.pinned is not None:
                current['pinned'] = body.pinned
            if body.archived is not None:
                current['archived'] = body.archived
            prefs_by_user[uid] = current
            core.save_chat_thread_meta(meta)
        return {'status': 'ok', 'thread_id': thread_id, 'prefs': current}

    @router.post('/api/chat/threads/close')
    def close_chat_thread(
        body: ChatThreadCloseBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        core.reject_self_chat(user['id'], body.to_user_id)
        core.validate_dm_recipient(body.to_user_id)
        thread_id = core.chat_thread_id(user['id'], body.to_user_id)
        with core.chat_lock:
            meta = core.load_chat_thread_meta()
            thread_meta = meta.setdefault(thread_id, {})
            thread_meta['closed'] = True
            thread_meta['closed_at'] = int(time.time())
            thread_meta['closed_by'] = str(user['id'])
            core.save_chat_thread_meta(meta)
        for uid in chat_thread_participants(thread_id):
            if uid == str(user['id']):
                continue
            try:
                deps.send_telegram_message(int(uid), "🔒 Чат закрыт руководством")
            except Exception:
                pass
        return {'status': 'ok', 'thread_id': thread_id}

    @router.post('/api/chat/threads/reopen')
    def reopen_chat_thread(
        body: ChatThreadCloseBody,
        user: dict = Depends(deps.get_current_user),
        _: None = Depends(deps.require_owner),
    ):
        core.reject_self_chat(user['id'], body.to_user_id)
        core.validate_dm_recipient(body.to_user_id)
        thread_id = core.chat_thread_id(user['id'], body.to_user_id)
        with core.chat_lock:
            meta = core.load_chat_thread_meta()
            if thread_id in meta:
                meta[thread_id]['closed'] = False
                core.save_chat_thread_meta(meta)
        return {'status': 'ok', 'thread_id': thread_id}

    @router.get('/api/chat/threads/status')
    def get_chat_thread_status(
        with_: str = '',
        user: dict = Depends(deps.get_current_user),
    ):
        thread_id = core.chat_thread_id(user['id'], with_ or None)
        meta = core.load_chat_thread_meta()
        return meta.get(thread_id, {'closed': False})

    return router, ChatActionsHandlers(
        normalize_chat_location=normalize_chat_location,
        reply_snapshot=reply_snapshot,
        chat_thread_participants=chat_thread_participants,
        check_message_access=check_message_access,
        get_chat_attachment=get_chat_attachment,
        send_manager_broadcast=send_manager_broadcast,
        post_chat_message=post_chat_message,
        forward_chat_message=forward_chat_message,
        delete_chat_message=delete_chat_message,
        delete_chat_thread=delete_chat_thread,
        toggle_chat_reaction=toggle_chat_reaction,
        set_chat_thread_prefs=set_chat_thread_prefs,
        close_chat_thread=close_chat_thread,
        reopen_chat_thread=reopen_chat_thread,
        get_chat_thread_status=get_chat_thread_status,
    )
