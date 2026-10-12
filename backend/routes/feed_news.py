"""Weather/news/birthday feed domain extracted from ``backend.main``.

Owns feed read/saved state, weather reactions, news reactions/comments and the
birthday feed. Photo-feed storage stays in its own domain and is injected here
only where unread/saved calculations need to see photo metadata.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Collection, Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel


FEED_SAVE_TYPES = {'photo', 'news', 'weather'}
FEED_SAVE_TYPE_ALIASES = {'photos': 'photo', 'info': 'weather'}


class NewsReactionIn(BaseModel):
    reaction: str


class NewsCommentBody(BaseModel):
    text: str
    reply_to: str | None = None


class FeedReadBody(BaseModel):
    tab: str


class FeedSavedBody(BaseModel):
    item_type: str
    item_id: str
    saved: bool


@dataclass(frozen=True)
class FeedNewsRouteDeps:
    get_current_user: Callable[..., dict]
    get_role: Callable[..., str]
    safe_load_json: Callable[[str, Any], Any]
    atomic_write_json: Callable[[str, Any], None]
    update_json_transaction: Callable[[str, Any, Callable[[Any], Any]], Any]
    weather_feed_file: Callable[[], str]
    news_feed_file: Callable[[], str]
    weather_reactions_file: Callable[[], str]
    news_reactions_file: Callable[[], str]
    news_reads_file: Callable[[], str]
    news_comments_file: Callable[[], str]
    feed_reads_file: Callable[[], str]
    feed_saved_file: Callable[[], str]
    birthday_alerts_file: Callable[[], str]
    load_photo_meta: Callable[[], list]
    load_worker_profiles: Callable[[], dict]
    load_roles: Callable[[], dict]
    sanitize_display_name: Callable[[Any, str], str]
    resolve_current_display_name: Callable[[Any, Any], str]
    business_today: Callable[[], Any]
    business_today_str: Callable[[], str]
    create_critical_alert: Callable[..., Any]
    emit_comment_activity_alerts: Callable[..., Any]
    comment_alert_recipients: Callable[..., Collection[str]]


@dataclass(frozen=True)
class FeedNewsHandlers:
    weather_entry_key: Callable[[dict], str]
    load_weather_reactions: Callable[[], dict]
    save_weather_reactions: Callable[[dict], None]
    load_feed_saved: Callable[[], dict]
    feed_saved_bucket: Callable[[dict, str, str], dict]
    is_feed_saved: Callable[[dict, str, str, str], bool]
    feed_item_exists: Callable[[str, str], bool]
    load_news_reactions: Callable[[], dict]
    save_news_reactions: Callable[[dict], None]
    load_news_reads: Callable[[], dict]
    save_news_reads: Callable[[dict], None]
    load_birthday_alerts: Callable[[], list]
    save_birthday_alerts: Callable[[list], None]
    check_upcoming_birthdays: Callable[[], None]
    load_news_comments: Callable[[], dict]
    save_news_comments: Callable[[dict], None]
    load_feed_reads: Callable[[], dict]
    save_feed_reads: Callable[[dict], None]
    news_post_title: Callable[[str], str]
    get_weather_feed: Callable[..., dict]
    react_weather_entry: Callable[..., dict]
    get_birthday_feed: Callable[..., dict]
    get_news_feed: Callable[..., dict]
    react_news_post: Callable[..., dict]
    add_news_comment: Callable[..., dict]
    get_news_comments: Callable[..., dict]
    delete_news_comment: Callable[..., dict]
    mark_feed_read: Callable[..., dict]
    set_feed_saved: Callable[..., dict]
    get_feed_unread: Callable[..., dict]


def create_feed_news_router(deps: FeedNewsRouteDeps) -> tuple[APIRouter, FeedNewsHandlers]:
    router = APIRouter()

    def weather_entry_key(entry: dict) -> str:
        return f"{entry.get('object', '')}::{entry.get('created', '')}"

    def load_weather_reactions() -> dict:
        return deps.safe_load_json(deps.weather_reactions_file(), {})

    def save_weather_reactions(data: dict):
        deps.atomic_write_json(deps.weather_reactions_file(), data)

    def load_feed_saved() -> dict:
        return deps.safe_load_json(deps.feed_saved_file(), {})

    def feed_saved_bucket(saved: dict, uid: str, item_type: str) -> dict:
        user_saved = saved.get(str(uid), {})
        if not isinstance(user_saved, dict):
            return {}
        bucket = user_saved.get(item_type, {})
        return bucket if isinstance(bucket, dict) else {}

    def is_feed_saved(saved: dict, uid: str, item_type: str, item_id: str) -> bool:
        return str(item_id) in feed_saved_bucket(saved, uid, item_type)

    def feed_item_exists(item_type: str, item_id: str) -> bool:
        if item_type == 'photo':
            return any(str(p.get('id')) == item_id for p in deps.load_photo_meta())
        if item_type == 'news':
            path = deps.news_feed_file()
            if not os.path.exists(path):
                return False
            try:
                with open(path, encoding='utf-8') as handle:
                    return any(str(post.get('id')) == item_id for post in json.load(handle))
            except Exception:
                return False
        if item_type == 'weather':
            path = deps.weather_feed_file()
            if not os.path.exists(path):
                return False
            try:
                with open(path, encoding='utf-8') as handle:
                    return any(weather_entry_key(entry) == item_id for entry in json.load(handle))
            except Exception:
                return False
        return False

    @router.get('/api/feed/weather')
    def get_weather_feed(user: dict = Depends(deps.get_current_user)):
        path = deps.weather_feed_file()
        if not os.path.exists(path):
            return {'feed': []}
        with open(path, encoding='utf-8') as handle:
            feed = json.load(handle)
        reactions = load_weather_reactions()
        saved = load_feed_saved()
        uid = str(user['id'])
        for entry in feed:
            key = weather_entry_key(entry)
            entry_reactions = reactions.get(key, {})
            entry['likes'] = len(entry_reactions)
            entry['liked_by_me'] = uid in entry_reactions
            entry['saved_by_me'] = is_feed_saved(saved, uid, 'weather', key)
        return {'feed': feed}

    @router.post('/api/feed/weather/react')
    def react_weather_entry(body: dict, user: dict = Depends(deps.get_current_user)):
        key = f"{body.get('object', '')}::{body.get('created', '')}"
        reactions = load_weather_reactions()
        entry_reactions = reactions.setdefault(key, {})
        uid = str(user['id'])
        liked = bool(body.get('liked'))
        if liked:
            entry_reactions[uid] = True
        else:
            entry_reactions.pop(uid, None)
        save_weather_reactions(reactions)
        return {'likes': len(entry_reactions), 'liked_by_me': liked}

    def load_news_reactions() -> dict:
        return deps.safe_load_json(deps.news_reactions_file(), {})

    def save_news_reactions(data: dict):
        deps.atomic_write_json(deps.news_reactions_file(), data)

    def load_news_reads() -> dict:
        return deps.safe_load_json(deps.news_reads_file(), {})

    def save_news_reads(data: dict):
        deps.atomic_write_json(deps.news_reads_file(), data)

    def load_birthday_alerts() -> list:
        return deps.safe_load_json(deps.birthday_alerts_file(), [])

    def save_birthday_alerts(items: list):
        deps.atomic_write_json(deps.birthday_alerts_file(), items)

    def check_upcoming_birthdays():
        profiles = deps.load_worker_profiles()
        today = deps.business_today()
        in_three_days = today + timedelta(days=3)
        roles = deps.load_roles()
        owner_id = next((uid for uid, role in roles.items() if role == 'owner'), None)
        to_create = []

        def mutate(alerts):
            already = {item.get('idem') for item in alerts if item.get('idem')}

            def emit(uid, name, occurrence, kind, idem, title):
                alerts.append({
                    'user_id': uid,
                    'name': name,
                    'year': occurrence.year,
                    'kind': kind,
                    'idem': idem,
                    'date': occurrence.strftime('%Y-%m-%d'),
                    'created_at': int(time.time()),
                })
                to_create.append((owner_id or uid, title, idem))

            for uid, profile in profiles.items():
                if roles.get(str(uid)) != 'worker':
                    continue
                birthday = profile.get('birthday')
                if not birthday:
                    continue
                try:
                    birthday_date = datetime.strptime(birthday, '%Y-%m-%d').date()
                except ValueError:
                    continue
                name = deps.sanitize_display_name(profile.get('name'), uid)
                if (birthday_date.month, birthday_date.day) == (in_three_days.month, in_three_days.day):
                    idem = f'birthday:{uid}:{in_three_days.year}:3days'
                    if idem not in already:
                        emit(
                            uid, name, in_three_days, '3days', idem,
                            f'🎂 Через 3 дня день рождения у {name}. Не забудьте подготовить подарок и поздравление.',
                        )
                if (birthday_date.month, birthday_date.day) == (today.month, today.day):
                    idem = f'birthday:{uid}:{today.year}:today'
                    if idem not in already:
                        emit(uid, name, today, 'today', idem, f'🎂 Сегодня день рождения у {name}')

        deps.update_json_transaction(deps.birthday_alerts_file(), [], mutate)
        for target_user_id, title, idem in to_create:
            try:
                deps.create_critical_alert(
                    target_user_id=target_user_id,
                    kind='birthday',
                    title=title,
                    ref_id=idem,
                )
            except Exception:
                pass

    @router.get('/api/feed/birthdays')
    def get_birthday_feed(user: dict = Depends(deps.get_current_user)):
        check_upcoming_birthdays()
        today = deps.business_today_str()
        upcoming = [item for item in load_birthday_alerts() if item['date'] >= today]
        return {'birthdays': upcoming}

    def load_news_comments() -> dict:
        return deps.safe_load_json(deps.news_comments_file(), {})

    def save_news_comments(data: dict):
        deps.atomic_write_json(deps.news_comments_file(), data)

    def load_feed_reads() -> dict:
        return deps.safe_load_json(deps.feed_reads_file(), {})

    def save_feed_reads(data: dict):
        deps.atomic_write_json(deps.feed_reads_file(), data)

    def news_post_title(post_id: str) -> str:
        try:
            path = deps.news_feed_file()
            if os.path.exists(path):
                with open(path, encoding='utf-8') as handle:
                    for post in json.load(handle):
                        if post.get('id') == post_id:
                            return post.get('title', '') or ''
        except Exception:
            pass
        return ''

    @router.get('/api/feed/news')
    def get_news_feed(user: dict = Depends(deps.get_current_user)):
        path = deps.news_feed_file()
        if not os.path.exists(path):
            return {'feed': []}
        with open(path, encoding='utf-8') as handle:
            feed = json.load(handle)
        reactions = load_news_reactions()
        comments = load_news_comments()
        saved = load_feed_saved()
        uid = str(user['id'])
        for post in feed:
            post['my_reaction'] = reactions.get(post['id'], {}).get(uid)
            post['saved_by_me'] = is_feed_saved(saved, uid, 'news', post['id'])
            post_comments = comments.get(post['id'], [])
            post['comment_count'] = len(post_comments)
            post['last_comment_at'] = max((c.get('ts', 0) for c in post_comments), default=0)
        reads = load_news_reads()
        user_reads = reads.setdefault(uid, {})
        for post in feed:
            category = post.get('category') or 'Другое'
            user_reads[category] = user_reads.get(category, 0) + 1
        save_news_reads(reads)
        return {'feed': feed}

    @router.post('/api/feed/news/{post_id}/react')
    def react_news_post(
        post_id: str,
        body: NewsReactionIn,
        user: dict = Depends(deps.get_current_user),
    ):
        if body.reaction not in ('like', 'dislike', 'none'):
            raise HTTPException(400, 'reaction должна быть like/dislike/none')
        path = deps.news_feed_file()
        if not os.path.exists(path):
            raise HTTPException(404, 'лента новостей пуста')
        with open(path, encoding='utf-8') as handle:
            feed = json.load(handle)
        post = next((item for item in feed if item['id'] == post_id), None)
        if not post:
            raise HTTPException(404, 'пост не найден')
        reactions = load_news_reactions()
        post_reactions = reactions.setdefault(post_id, {})
        uid = str(user['id'])
        previous = post_reactions.get(uid)
        if previous == 'like':
            post['likes'] = max(0, post.get('likes', 0) - 1)
        elif previous == 'dislike':
            post['dislikes'] = max(0, post.get('dislikes', 0) - 1)
        if body.reaction == 'none':
            post_reactions.pop(uid, None)
        else:
            post_reactions[uid] = body.reaction
            if body.reaction == 'like':
                post['likes'] = post.get('likes', 0) + 1
            else:
                post['dislikes'] = post.get('dislikes', 0) + 1
        save_news_reactions(reactions)
        deps.atomic_write_json(path, feed)
        return {
            'ok': True,
            'likes': post['likes'],
            'dislikes': post['dislikes'],
            'my_reaction': body.reaction if body.reaction != 'none' else None,
        }

    @router.post('/api/feed/news/{post_id}/comments')
    def add_news_comment(
        post_id: str,
        body: NewsCommentBody,
        user: dict = Depends(deps.get_current_user),
    ):
        text = (body.text or '').strip()[:500]
        if not text:
            raise HTTPException(400, 'Комментарий не может быть пустым')
        comments = load_news_comments()
        post_comments = comments.setdefault(post_id, [])
        prior_ids = {comment.get('user_id') for comment in post_comments}
        actor_name = deps.sanitize_display_name(user.get('first_name'), str(user['id']))
        new_id = uuid.uuid4().hex
        post_comments.append({
            'id': new_id,
            'user_id': str(user['id']),
            'name': actor_name,
            'text': text,
            'reply_to': body.reply_to or None,
            'ts': int(time.time()),
        })
        save_news_comments(comments)
        try:
            deps.emit_comment_activity_alerts(
                'news_comment', post_id, new_id, str(user['id']), actor_name,
                f'{actor_name} прокомментировал новость', news_post_title(post_id),
                deps.comment_alert_recipients(user['id'], prior_ids),
            )
        except Exception:
            pass
        return {'comments': post_comments}

    @router.get('/api/feed/news/{post_id}/comments')
    def get_news_comments(post_id: str, user: dict = Depends(deps.get_current_user)):
        comments = load_news_comments().get(post_id, [])
        for comment in comments:
            comment['name'] = deps.resolve_current_display_name(
                comment.get('user_id'), comment.get('name')
            )
        return {'comments': comments}

    @router.delete('/api/feed/news/{post_id}/comments/{comment_id}')
    def delete_news_comment(
        post_id: str,
        comment_id: str,
        user: dict = Depends(deps.get_current_user),
        role: str = Depends(deps.get_role),
    ):
        comments = load_news_comments()
        post_comments = comments.get(post_id, [])
        comment = next((item for item in post_comments if item.get('id') == comment_id), None)
        if not comment:
            raise HTTPException(404, 'Комментарий не найден')
        if str(comment.get('user_id')) != str(user['id']) and role != 'owner':
            raise HTTPException(403, 'Можно удалить только свой комментарий')
        comments[post_id] = [item for item in post_comments if item.get('id') != comment_id]
        save_news_comments(comments)
        return {'comments': comments[post_id]}

    @router.post('/api/feed/read')
    def mark_feed_read(body: FeedReadBody, user: dict = Depends(deps.get_current_user)):
        if body.tab not in ('news', 'photos', 'info'):
            raise HTTPException(400, 'tab должен быть news/photos/info')
        reads = load_feed_reads()
        user_record = reads.setdefault(str(user['id']), {})
        user_record[f'last_{body.tab}_read_at'] = int(time.time())
        save_feed_reads(reads)
        return {
            'ok': True,
            'tab': body.tab,
            'read_at': user_record[f'last_{body.tab}_read_at'],
        }

    @router.post('/api/feed/saved')
    def set_feed_saved(body: FeedSavedBody, user: dict = Depends(deps.get_current_user)):
        raw_item_type = (body.item_type or '').strip()
        item_type = FEED_SAVE_TYPE_ALIASES.get(raw_item_type, raw_item_type)
        item_id = (body.item_id or '').strip()
        if item_type not in FEED_SAVE_TYPES:
            raise HTTPException(400, 'item_type должен быть photo/news/weather')
        if not item_id:
            raise HTTPException(400, 'item_id обязателен')
        if not feed_item_exists(item_type, item_id):
            raise HTTPException(404, 'Пост не найден')
        uid = str(user['id'])

        def mutate(saved: dict):
            user_saved = saved.setdefault(uid, {})
            if not isinstance(user_saved, dict):
                user_saved = {}
                saved[uid] = user_saved
            bucket = user_saved.setdefault(item_type, {})
            if not isinstance(bucket, dict):
                bucket = {}
                user_saved[item_type] = bucket
            if body.saved:
                bucket[item_id] = int(time.time())
            else:
                bucket.pop(item_id, None)
            return {
                'ok': True,
                'item_type': item_type,
                'item_id': item_id,
                'saved_by_me': item_id in bucket,
            }

        return deps.update_json_transaction(deps.feed_saved_file(), {}, mutate)

    @router.get('/api/feed/unread')
    def get_feed_unread(user: dict = Depends(deps.get_current_user)):
        uid = str(user['id'])
        user_record = load_feed_reads().get(uid, {})
        news_read = user_record.get('last_news_read_at', 0)
        photos_read = user_record.get('last_photos_read_at', 0)
        info_read = user_record.get('last_info_read_at', 0)

        news_unread = 0
        news_path = deps.news_feed_file()
        if os.path.exists(news_path):
            try:
                with open(news_path, encoding='utf-8') as handle:
                    feed = json.load(handle)
            except Exception:
                feed = []
            comments = load_news_comments()
            for post in feed:
                created = post.get('created', 0) or 0
                post_comments = comments.get(post['id'], [])
                last_comment = max((c.get('ts', 0) for c in post_comments), default=0)
                if created > news_read or last_comment > news_read:
                    news_unread += 1

        photos_unread = 0
        try:
            for photo in deps.load_photo_meta():
                if (photo.get('ts', 0) or 0) > photos_read:
                    photos_unread += 1
        except Exception:
            pass

        info_unread = 0
        weather_path = deps.weather_feed_file()
        if os.path.exists(weather_path):
            try:
                with open(weather_path, encoding='utf-8') as handle:
                    weather_feed = json.load(handle)
                for entry in weather_feed:
                    created = entry.get('created', 0)
                    if isinstance(created, (int, float)):
                        created_ts = int(created)
                    else:
                        raw = str(created or '').strip()
                        if raw.isdigit():
                            created_ts = int(raw)
                        else:
                            created_ts = int(datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()) if raw else 0
                    if created_ts > info_read:
                        info_unread += 1
            except Exception:
                info_unread = 0
        return {'news': news_unread, 'photos': photos_unread, 'info': info_unread}

    return router, FeedNewsHandlers(
        weather_entry_key=weather_entry_key,
        load_weather_reactions=load_weather_reactions,
        save_weather_reactions=save_weather_reactions,
        load_feed_saved=load_feed_saved,
        feed_saved_bucket=feed_saved_bucket,
        is_feed_saved=is_feed_saved,
        feed_item_exists=feed_item_exists,
        load_news_reactions=load_news_reactions,
        save_news_reactions=save_news_reactions,
        load_news_reads=load_news_reads,
        save_news_reads=save_news_reads,
        load_birthday_alerts=load_birthday_alerts,
        save_birthday_alerts=save_birthday_alerts,
        check_upcoming_birthdays=check_upcoming_birthdays,
        load_news_comments=load_news_comments,
        save_news_comments=save_news_comments,
        load_feed_reads=load_feed_reads,
        save_feed_reads=save_feed_reads,
        news_post_title=news_post_title,
        get_weather_feed=get_weather_feed,
        react_weather_entry=react_weather_entry,
        get_birthday_feed=get_birthday_feed,
        get_news_feed=get_news_feed,
        react_news_post=react_news_post,
        add_news_comment=add_news_comment,
        get_news_comments=get_news_comments,
        delete_news_comment=delete_news_comment,
        mark_feed_read=mark_feed_read,
        set_feed_saved=set_feed_saved,
        get_feed_unread=get_feed_unread,
    )
