"""Phase 27 regressions for identity-bound media ownership."""
from __future__ import annotations

import os
import sys
import threading
from copy import deepcopy
from datetime import date

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from core.media_paths import resolve_owned_media_path  # noqa: E402
from routes.critical_alerts import CriticalAlertsDeps, create_critical_alerts_router  # noqa: E402
from routes.workforce_profiles import WorkforceProfilesDeps, create_workforce_profiles_router  # noqa: E402


def test_resolver_rejects_same_root_symlink_alias(tmp_path):
    root = tmp_path / 'media'
    root.mkdir()
    target = root / 'target.jpg'
    target.write_bytes(b'secret')
    alias = root / 'alias.jpg'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    assert resolve_owned_media_path(str(root), 'target.jpg') == str(target)
    assert resolve_owned_media_path(str(root), 'alias.jpg') is None


def _build_avatar_handlers(tmp_path, profiles):
    avatar_root = tmp_path / 'avatars'
    profiles_file = tmp_path / 'worker_profiles.json'
    state = deepcopy(profiles)

    def update_json_transaction(_path, default, mutator):
        nonlocal state
        working = deepcopy(state if state is not None else default)
        result = mutator(working)
        state = working
        return result

    deps = WorkforceProfilesDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'worker',
        require_owner=lambda: None,
        load_roles=lambda: {'42': 'worker'},
        load_worker_profiles=lambda: deepcopy(state),
        sanitize_display_name=lambda value, fallback: str(value or fallback),
        update_json_transaction=update_json_transaction,
        atomic_write_json=lambda *_args, **_kwargs: None,
        worker_profiles_file=lambda: str(profiles_file),
        work_types_catalog=lambda: {},
        skill_options=(),
        normalize_profile_skills=lambda profile: (list(profile.get('skills_v2') or []), False),
        legacy_skill_names_from_v2=lambda _skills: [],
        set_skill_verification=lambda skills, _skill_id, _verified: (skills, False),
        skill_levels={'beginner', 'advanced'},
        business_today=date.today,
        business_today_str=lambda: date.today().isoformat(),
        last_seen_for=lambda _uid: None,
        online_threshold_seconds=300,
        load_checkin_meta=lambda: [],
        is_active_photo_checkin_session=lambda _session: False,
        cached_get_used_range=lambda _name: [],
        avatar_dir=lambda: str(avatar_root),
        avatar_max_bytes=4 * 1024 * 1024,
        sniff_image=lambda _raw: 'image/jpeg',
        allowed_image_mime_ext={'image/jpeg': 'jpg', 'image/png': 'png'},
    )
    _router, handlers = create_workforce_profiles_router(deps)
    return handlers, avatar_root


def test_avatar_requires_durable_avatar_metadata(tmp_path):
    handlers, avatar_root = _build_avatar_handlers(tmp_path, {'42': {'avatar': False}})
    (avatar_root / '42.jpg').write_bytes(b'orphan')

    with pytest.raises(HTTPException) as exc:
        handlers.get_avatar('42', user={'id': '42'})

    assert exc.value.status_code == 404


def test_avatar_with_extension_metadata_does_not_fall_back_to_stale_format(tmp_path):
    handlers, avatar_root = _build_avatar_handlers(
        tmp_path, {'42': {'avatar': True, 'avatar_ext': 'png'}}
    )
    (avatar_root / '42.jpg').write_bytes(b'stale')

    with pytest.raises(HTTPException) as exc:
        handlers.get_avatar('42', user={'id': '42'})

    assert exc.value.status_code == 404


def test_avatar_with_matching_extension_remains_accessible(tmp_path):
    handlers, avatar_root = _build_avatar_handlers(
        tmp_path, {'42': {'avatar': True, 'avatar_ext': 'png'}}
    )
    avatar = avatar_root / '42.png'
    avatar.write_bytes(b'image')

    response = handlers.get_avatar('42', user={'id': '42'})

    assert isinstance(response, FileResponse)
    assert response.path == str(avatar)


def test_legacy_avatar_without_extension_metadata_still_resolves(tmp_path):
    handlers, avatar_root = _build_avatar_handlers(tmp_path, {'42': {'avatar': True}})
    avatar = avatar_root / '42.jpg'
    avatar.write_bytes(b'legacy')

    response = handlers.get_avatar('42', user={'id': '42'})

    assert isinstance(response, FileResponse)
    assert response.path == str(avatar)


def test_avatar_symlink_alias_is_rejected_even_inside_avatar_root(tmp_path):
    handlers, avatar_root = _build_avatar_handlers(
        tmp_path,
        {
            '42': {'avatar': True, 'avatar_ext': 'jpg'},
            '99': {'avatar': True, 'avatar_ext': 'jpg'},
        },
    )
    target = avatar_root / '99.jpg'
    target.write_bytes(b'other-user')
    alias = avatar_root / '42.jpg'
    try:
        alias.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_avatar('42', user={'id': '42'})

    assert exc.value.status_code == 404


def _build_critical_handlers(tmp_path, alerts):
    photo_root = tmp_path / 'critical-alert-photos'
    alerts_file = tmp_path / 'critical-alerts.json'
    photo_root.mkdir()
    state = deepcopy(alerts)

    def safe_load(path, default):
        if path == str(alerts_file):
            return deepcopy(state)
        return deepcopy(default)

    deps = CriticalAlertsDeps(
        get_current_user=lambda: {'id': '1'},
        get_role=lambda: 'owner',
        require_owner=lambda: None,
        critical_alerts_file=lambda: str(alerts_file),
        critical_alert_photo_dir=lambda: str(photo_root),
        safe_load_json=safe_load,
        atomic_write_json=lambda *_args, **_kwargs: None,
        update_json_transaction=lambda _path, _default, mutator: mutator(state),
        load_roles=lambda: {'1': 'owner', '42': 'worker'},
        send_telegram_message=lambda *_args, **_kwargs: None,
        chat_lock=threading.Lock(),
        load_chat=lambda: [],
        save_chat=lambda _messages: None,
        chat_thread_id=lambda *_args: 'group',
        sniff_image=lambda _data: 'image/jpeg',
        allowed_image_mime_ext={'image/jpeg': 'jpg'},
    )
    _router, handlers = create_critical_alerts_router(deps)
    return handlers, photo_root


def test_critical_alert_photo_cannot_symlink_to_sibling_alert(tmp_path):
    alerts = [
        {
            'id': 'alert-a',
            'target_user_id': '42',
            'kind': 'manual',
            'title': 'A',
            'resolution_photos': ['photo.jpg'],
        },
        {
            'id': 'alert-b',
            'target_user_id': '42',
            'kind': 'manual',
            'title': 'B',
            'resolution_photos': ['secret.jpg'],
        },
    ]
    handlers, photo_root = _build_critical_handlers(tmp_path, alerts)
    a_dir = photo_root / 'alert-a'
    b_dir = photo_root / 'alert-b'
    a_dir.mkdir()
    b_dir.mkdir()
    secret = b_dir / 'secret.jpg'
    secret.write_bytes(b'secret')
    alias = a_dir / 'photo.jpg'
    try:
        alias.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_critical_alert_photo(
            'alert-a', 'photo.jpg', user={'id': '1'}, role='owner'
        )

    assert exc.value.status_code == 404
