"""Phase 26 security regressions for critical-alert photo retrieval."""
from __future__ import annotations

import inspect
import os
import sys
import threading
from copy import deepcopy

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from routes.critical_alerts import CriticalAlertsDeps, create_critical_alerts_router  # noqa: E402


def _build_handlers(tmp_path, alerts):
    photo_root = tmp_path / 'critical-alert-photos'
    alerts_file = tmp_path / 'critical-alerts.json'
    photo_root.mkdir()

    state = deepcopy(alerts)

    def safe_load(path, default):
        if path == str(alerts_file):
            return deepcopy(state)
        return deepcopy(default)

    def update_json_transaction(_path, default, mutator):
        nonlocal state
        working = deepcopy(state if state is not None else default)
        result = mutator(working)
        state = working
        return result

    deps = CriticalAlertsDeps(
        get_current_user=lambda: {'id': '42'},
        get_role=lambda: 'worker',
        require_owner=lambda: None,
        critical_alerts_file=lambda: str(alerts_file),
        critical_alert_photo_dir=lambda: str(photo_root),
        safe_load_json=safe_load,
        atomic_write_json=lambda *_args, **_kwargs: None,
        update_json_transaction=update_json_transaction,
        load_roles=lambda: {'42': 'worker', '1': 'owner'},
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


def _alert(*, photos=None):
    return {
        'id': 'alert-1',
        'target_user_id': '42',
        'kind': 'manual',
        'title': 'Test',
        'resolution_photos': list(photos or []),
    }


def test_registered_photo_remains_accessible_to_alert_owner(tmp_path):
    handlers, photo_root = _build_handlers(tmp_path, [_alert(photos=['good.jpg'])])
    alert_dir = photo_root / 'alert-1'
    alert_dir.mkdir()
    photo = alert_dir / 'good.jpg'
    photo.write_bytes(b'image')

    response = handlers.get_critical_alert_photo(
        'alert-1', 'good.jpg', user={'id': '42'}, role='worker',
    )

    assert isinstance(response, FileResponse)
    assert os.path.realpath(response.path) == os.path.realpath(photo)


def test_unlisted_file_is_not_served_even_to_owner(tmp_path):
    handlers, photo_root = _build_handlers(tmp_path, [_alert(photos=['good.jpg'])])
    alert_dir = photo_root / 'alert-1'
    alert_dir.mkdir()
    (alert_dir / 'orphan.jpg').write_bytes(b'orphan')

    with pytest.raises(HTTPException) as exc:
        handlers.get_critical_alert_photo(
            'alert-1', 'orphan.jpg', user={'id': '1'}, role='owner',
        )

    assert exc.value.status_code == 404


def test_registered_symlink_escape_is_rejected(tmp_path):
    handlers, photo_root = _build_handlers(tmp_path, [_alert(photos=['good.jpg'])])
    alert_dir = photo_root / 'alert-1'
    alert_dir.mkdir()
    outside = tmp_path / 'outside.jpg'
    outside.write_bytes(b'secret')
    link = alert_dir / 'good.jpg'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip('symlinks unavailable on this platform')

    with pytest.raises(HTTPException) as exc:
        handlers.get_critical_alert_photo(
            'alert-1', 'good.jpg', user={'id': '1'}, role='owner',
        )

    assert exc.value.status_code == 404


def test_registered_photo_of_other_worker_remains_forbidden(tmp_path):
    handlers, photo_root = _build_handlers(tmp_path, [_alert(photos=['good.jpg'])])
    alert_dir = photo_root / 'alert-1'
    alert_dir.mkdir()
    (alert_dir / 'good.jpg').write_bytes(b'image')

    with pytest.raises(HTTPException) as exc:
        handlers.get_critical_alert_photo(
            'alert-1', 'good.jpg', user={'id': '99'}, role='worker',
        )

    assert exc.value.status_code == 403


def test_phase26_photo_get_requires_metadata_membership_and_owned_path_resolution():
    import routes.critical_alerts as critical_routes

    source = inspect.getsource(critical_routes.create_critical_alerts_router)
    block = source.split('    def get_critical_alert_photo', 1)[1].split('    @router.get', 1)[0]

    assert "filename not in (alert.get('resolution_photos') or [])" in block
    assert 'resolve_owned_media_path(' in block
    assert block.index("filename not in (alert.get('resolution_photos') or [])") < block.index('FileResponse(path)')
    assert block.index('resolve_owned_media_path(') < block.index('FileResponse(path)')
