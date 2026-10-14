"""Cross-domain activity/comment bridge.

This module owns the small integration surface between feed comments, photo
comments and chat. It deliberately depends on injected domain handlers rather
than importing ``backend.main`` or sibling route modules, so the composition
root stays in ``main.py`` without creating route-to-route cycles.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from ..core.media_paths import resolve_owned_media_path
    from ..core.storage import update_json_transaction
except ImportError:
    from core.media_paths import resolve_owned_media_path  # noqa: E402
    from core.storage import update_json_transaction  # noqa: E402


class ActivityReadBody(BaseModel):
    kind: str
    ref_id: str


class CommentForwardBody(BaseModel):
    source_type: str  # 'news' | 'photo'
    source_id: str
    comment_id: str
    to_user_id: str | None = None
    thread_key: str | None = None


@dataclass(frozen=True)
class ActivityBridgeDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    activity_alerts_file: Callable[[], str]
    news_feed_file: Callable[[], str]
    photo_dir: Callable[[], str]
    cached_get_used_range: Callable[[str], list]
    load_roles: Callable[[], dict]
    load_news_comments: Callable[[], dict]
    photo_lock: Any
    load_photo_meta: Callable[[], list]
    check_thread_access: Callable[[str, str, str], Any]
    reject_self_chat: Callable[[Any, Any], Any]
    validate_dm_recipient: Callable[[Any], Any]
    chat_thread_id: Callable[[Any, Any], str]
    load_chat_thread_meta: Callable[[], dict]
    chat_lock: Any
    load_chat: Callable[[], list]
    save_chat: Callable[[list], None]


@dataclass(frozen=True)
class ActivityBridgeHandlers:
    load_activity_alerts: Callable[[], list]
    save_activity_alerts: Callable[[list], None]
    news_post_title: Callable[[str], str]
    find_object_row_by_id: Callable[[str], dict | None]
    resolve_object_name: Callable[[str], str]
    comment_alert_recipients: Callable[..., set]
    emit_comment_activity_alerts: Callable[..., None]
    mark_activity_alerts_read: Callable[..., dict]
    forward_comment: Callable[..., dict]
    get_feed_photo_file: Callable[..., FileResponse]


def create_activity_bridge_router(
    deps: ActivityBridgeDeps,
) -> tuple[APIRouter, ActivityBridgeHandlers]:
    router = APIRouter()

    def load_activity_alerts() -> list:
        return deps.safe_load_json(deps.activity_alerts_file(), [])

    def save_activity_alerts(items: list):
        deps.atomic_write_json(deps.activity_alerts_file(), items)

    def news_post_title(post_id: str) -> str:
        try:
            for post in deps.safe_load_json(deps.news_feed_file(), []):
                if post.get('id') == post_id:
                    return post.get('title', '') or ''
        except Exception:
            pass
        return ''

    def find_object_row_by_id(object_id: str) -> dict | None:
        rows = deps.cached_get_used_range('Объекты')
        if not rows:
            return None
        header, data = rows[0], rows[1:]
        for values in data:
            row = dict(zip(header, values))
            if str(row.get('ID объекта', '')) == str(object_id):
                return row
        return None

    def resolve_object_name(object_id: str) -> str:
        if not object_id:
            return ''
        try:
            rows = deps.cached_get_used_range('Объекты')
            if rows:
                header = rows[0]
                for values in rows[1:]:
                    row = dict(zip(header, values))
                    if str(row.get('ID объекта', '')) == str(object_id):
                        return row.get('Объект', '') or str(object_id)
        except Exception:
            pass
        return str(object_id)

    def comment_alert_recipients(actor_id, prior_user_ids, author_id=None) -> set:
        recipients = {
            str(uid) for uid, role in deps.load_roles().items() if role == 'owner'
        }
        recipients |= {str(uid) for uid in prior_user_ids if uid}
        if author_id:
            recipients.add(str(author_id))
        recipients.discard(str(actor_id))
        return recipients

    def emit_comment_activity_alerts(
        kind,
        ref_id,
        comment_id,
        actor_id,
        actor_name,
        title,
        subtitle,
        recipients,
    ):
        if not recipients:
            return
        now = int(time.time())

        def _mutate(items: list):
            existing = {
                (item.get('target_user_id'), item.get('comment_id')) for item in items
            }
            for uid in recipients:
                if (str(uid), comment_id) in existing:
                    continue
                items.append({
                    'id': uuid.uuid4().hex,
                    'target_user_id': str(uid),
                    'kind': kind,
                    'ref_id': str(ref_id),
                    'comment_id': comment_id,
                    'actor_id': str(actor_id),
                    'actor_name': actor_name,
                    'title': title,
                    'subtitle': subtitle,
                    'created_at': now,
                    'read_at': None,
                })

        update_json_transaction(deps.activity_alerts_file(), [], _mutate)

    @router.post('/api/activity-alerts/read')
    def mark_activity_alerts_read(
        body: ActivityReadBody,
        user: dict = Depends(deps.get_current_user),
    ):
        now = int(time.time())
        uid = str(user['id'])

        def _mutate(items: list):
            for item in items:
                if (
                    item.get('target_user_id') == uid
                    and item.get('kind') == body.kind
                    and item.get('ref_id') == str(body.ref_id)
                    and not item.get('read_at')
                ):
                    item['read_at'] = now

        update_json_transaction(deps.activity_alerts_file(), [], _mutate)
        return {'status': 'ok'}

    @router.post('/api/comments/forward')
    def forward_comment(
        body: CommentForwardBody,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        if body.source_type == 'news':
            comments = deps.load_news_comments().get(body.source_id, [])
            comment = next(
                (item for item in comments if item.get('id') == body.comment_id),
                None,
            )
            if not comment:
                raise HTTPException(404, 'Комментарий не найден')
            title = news_post_title(body.source_id) or 'Новость'
            card = (
                f"💬 Комментарий к новости\n«{title}»\n"
                f"{comment.get('name', '')}:\n{comment.get('text', '')}"
            )
        elif body.source_type == 'photo':
            with deps.photo_lock:
                photos = deps.load_photo_meta()
            entry = next(
                (item for item in photos if item['id'] == body.source_id),
                None,
            )
            if not entry:
                raise HTTPException(404, 'Фото не найдено')
            comment = next(
                (
                    item
                    for item in entry.get('comments', [])
                    if item.get('id') == body.comment_id
                ),
                None,
            )
            if not comment:
                raise HTTPException(404, 'Комментарий не найден')
            object_name = resolve_object_name(entry.get('object_id', '')) or 'без объекта'
            card = (
                f"💬 Комментарий к фотографии\nОбъект: {object_name}\n"
                f"{comment.get('name', '')}:\n{comment.get('text', '')}"
            )
        else:
            raise HTTPException(400, 'Неизвестный тип источника')

        if body.thread_key:
            deps.check_thread_access(body.thread_key, str(user['id']), role)
        else:
            deps.reject_self_chat(user['id'], body.to_user_id)
            deps.validate_dm_recipient(body.to_user_id)
            thread_id = deps.chat_thread_id(user['id'], body.to_user_id)
            thread_meta = deps.load_chat_thread_meta()
            if thread_meta.get(thread_id, {}).get('closed') and role != 'owner':
                raise HTTPException(403, 'Чат закрыт руководством')

        message = {
            'id': uuid.uuid4().hex,
            'ts': int(time.time()),
            'user_id': user['id'],
            'name': user.get('first_name', str(user['id'])),
            'text': card,
            'to_user_id': body.to_user_id,
            'thread_key': body.thread_key,
            'forwarded_comment': {
                'source_type': body.source_type,
                'source_id': body.source_id,
                'comment_id': body.comment_id,
            },
        }
        with deps.chat_lock:
            messages = deps.load_chat()
            messages.append(message)
            deps.save_chat(messages)
        return {'message': message, 'status': 'ok'}

    @router.get('/api/feed/photos/{photo_id}/file')
    def get_feed_photo_file(
        photo_id: str,
        index: int = 0,
        user: dict = Depends(deps.get_current_user),
    ):
        with deps.photo_lock:
            photos = deps.load_photo_meta()
        entry = next((item for item in photos if item['id'] == photo_id), None)
        if not entry:
            raise HTTPException(404, 'Фото не найдено')
        files = entry.get('files') or ([entry['file']] if entry.get('file') else [])
        if index < 0 or index >= len(files):
            raise HTTPException(404, 'Файл не найден по этому индексу')
        path = resolve_owned_media_path(deps.photo_dir(), str(files[index] or ''))
        if not path or not os.path.isfile(path):
            raise HTTPException(404, 'Файл отсутствует')
        return FileResponse(path)

    return router, ActivityBridgeHandlers(
        load_activity_alerts=load_activity_alerts,
        save_activity_alerts=save_activity_alerts,
        news_post_title=news_post_title,
        find_object_row_by_id=find_object_row_by_id,
        resolve_object_name=resolve_object_name,
        comment_alert_recipients=comment_alert_recipients,
        emit_comment_activity_alerts=emit_comment_activity_alerts,
        mark_activity_alerts_read=mark_activity_alerts_read,
        forward_comment=forward_comment,
        get_feed_photo_file=get_feed_photo_file,
    )
