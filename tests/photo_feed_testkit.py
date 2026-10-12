"""Reusable isolated harness for extracted photo-feed behavior tests."""
from __future__ import annotations

import copy
import json
import os
import tempfile

from routes.photo_feed import PhotoFeedRouteDeps, create_photo_feed_router


class PhotoFeedHarness:
    def __init__(self, worker_profiles=None):
        self.tmp = tempfile.TemporaryDirectory(prefix='photo-feed-domain-')
        root = self.tmp.name
        self.photo_dir = os.path.join(root, 'photos')
        self.checkin_dir = os.path.join(root, 'checkin')
        self.meta_path = os.path.join(root, 'feed_photos.json')
        self.reactions_path = os.path.join(root, 'feed_photo_reactions.json')
        os.makedirs(self.photo_dir, exist_ok=True)
        os.makedirs(self.checkin_dir, exist_ok=True)
        self.worker_profiles = copy.deepcopy(worker_profiles or {})
        self.saved = {}
        self.alerts = []
        self._write_json(self.meta_path, [])
        self._write_json(self.reactions_path, {})

        def safe_load(path, default):
            try:
                with open(path, encoding='utf-8') as handle:
                    return json.load(handle)
            except (FileNotFoundError, json.JSONDecodeError):
                return copy.deepcopy(default)

        def atomic_write(path, value):
            self._write_json(path, value)

        def resolve_current_display_name(uid, fallback):
            profile = self.worker_profiles.get(str(uid), {})
            return str(profile.get('name') or fallback or uid)

        def is_feed_saved(saved, uid, item_type, item_id):
            return bool(saved.get(str(uid), {}).get(item_type, {}).get(str(item_id)))

        _, self.handlers = create_photo_feed_router(PhotoFeedRouteDeps(
            get_current_user=lambda: {},
            get_role=lambda: 'worker',
            safe_load_json=safe_load,
            atomic_write_json=atomic_write,
            photo_dir=lambda: self.photo_dir,
            photo_meta_file=lambda: self.meta_path,
            photo_reactions_file=lambda: self.reactions_path,
            checkin_photo_base=lambda: self.checkin_dir,
            photo_max_bytes=8 * 1024 * 1024,
            photo_max_count=200,
            photo_max_files=10,
            sniff_image=lambda raw: 'image/jpeg' if raw else None,
            allowed_image_mime_ext={'image/jpeg': 'jpg'},
            load_feed_saved=lambda: copy.deepcopy(self.saved),
            is_feed_saved=is_feed_saved,
            sanitize_display_name=lambda raw, fallback: str(raw or fallback),
            resolve_current_display_name=resolve_current_display_name,
            resolve_object_name=lambda object_id: str(object_id or ''),
            emit_comment_activity_alerts=lambda *args, **kwargs: self.alerts.append((args, kwargs)),
            comment_alert_recipients=lambda *args, **kwargs: set(),
        ))

    @staticmethod
    def _write_json(path, value):
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(value, handle, ensure_ascii=False)

    def close(self):
        self.tmp.cleanup()

    @property
    def metadata(self):
        with open(self.meta_path, encoding='utf-8') as handle:
            return json.load(handle)

    @metadata.setter
    def metadata(self, value):
        self._write_json(self.meta_path, value)

    @property
    def reactions(self):
        with open(self.reactions_path, encoding='utf-8') as handle:
            return json.load(handle)

    def create_photo_file(self, filename='keep.jpg', data=b'jpg'):
        path = os.path.join(self.photo_dir, filename)
        with open(path, 'wb') as handle:
            handle.write(data)
        return path
