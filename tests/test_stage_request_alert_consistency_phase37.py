"""Phase 37 regressions for stage-request alert fan-out consistency."""
from __future__ import annotations

import os
import sys
from copy import deepcopy

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from services.stages import StageService, StageServiceDeps  # noqa: E402


class FakeObjects:
    VALID_STAGE_STATUS = ('Не начат', 'В работе', 'Готово')

    @staticmethod
    def all_stages(object_id):
        if object_id != 'OBJ-1':
            return []
        return [{
            'object_id': 'OBJ-1',
            '_row': 12,
            'ID строки этапа': 'OBJ-1-S1',
            'Название этапа': 'Монтаж',
        }]


class FakeRoadmap:
    STAGE_REQUESTS_FILE = 'stage-requests.json'
    ROADMAP_FILE = 'roadmap.json'
    REQUEST_KINDS = ('delete_stage', 'change_status')

    @staticmethod
    def _default_requests():
        return []

    @staticmethod
    def _default_store():
        return {'categories': {}, 'items': {}, 'notes': {}, 'stage_blocks': {}}

    @staticmethod
    def new_stage_request(
        requests,
        object_id,
        stage_key,
        stage_row,
        kind,
        requested_by,
        requester_name,
        *,
        payload,
    ):
        request = {
            'id': 'req-1',
            'object_id': object_id,
            'stage_key': stage_key,
            'stage_row': stage_row,
            'kind': kind,
            'payload': deepcopy(payload),
            'requested_by': str(requested_by),
            'requester_name': requester_name,
            'status': 'pending',
        }
        requests.append(request)
        return request

    @staticmethod
    def find_stage_request(requests, request_id):
        return next((item for item in requests if item.get('id') == request_id), None)


def _build_service(*, alert_error=None, attach_error=None, primary_error=None):
    roadmap = FakeRoadmap()
    stores = {roadmap.STAGE_REQUESTS_FILE: []}
    transaction_calls = 0
    alert_calls = []

    def safe_load_json(path, default):
        return deepcopy(stores.get(path, default))

    def update_json_transaction(path, default_factory, mutator):
        nonlocal transaction_calls
        transaction_calls += 1
        if transaction_calls == 1 and primary_error is not None:
            raise primary_error
        if transaction_calls == 2 and attach_error is not None:
            raise attach_error
        default = default_factory() if callable(default_factory) else deepcopy(default_factory)
        working = deepcopy(stores.get(path, default))
        result = mutator(working)
        stores[path] = working
        return deepcopy(result)

    def create_critical_alert(**kwargs):
        alert_calls.append(deepcopy(kwargs))
        if alert_error is not None:
            raise alert_error
        return {'id': 'alert-1'}

    service = StageService(StageServiceDeps(
        cached_get_used_range=lambda _tab: [],
        load_repo_objekte_lib=lambda: FakeObjects(),
        roadmap_lib=lambda: roadmap,
        safe_load_json=safe_load_json,
        update_json_transaction=update_json_transaction,
        business_today_str=lambda: '2026-10-14',
        append_object_history_best_effort=lambda *args, **kwargs: None,
        get_worker_profile=lambda _uid: {'name': 'Worker'},
        sanitize_display_name=lambda raw, fallback: raw or fallback,
        load_roles=lambda: {'1': 'owner', '77': 'worker'},
        create_critical_alert=create_critical_alert,
        send_telegram_message=lambda *args, **kwargs: None,
    ))
    return service, stores, alert_calls


def _create(service):
    return service.create_stage_request(
        'OBJ-1',
        12,
        kind='change_status',
        new_status='В работе',
        user={'id': '77'},
        role='worker',
    )


def test_stage_request_survives_critical_alert_creation_failure(caplog):
    service, stores, _alert_calls = _build_service(
        alert_error=OSError('simulated critical alert failure')
    )

    result = _create(service)

    assert result['id'] == 'req-1'
    assert 'critical_alert_id' not in result
    assert stores[FakeRoadmap.STAGE_REQUESTS_FILE][0]['id'] == 'req-1'
    assert 'critical_alert_id' not in stores[FakeRoadmap.STAGE_REQUESTS_FILE][0]
    assert 'Failed to create critical alert after stage request commit' in caplog.text


def test_stage_request_survives_alert_id_attachment_failure(caplog):
    service, stores, alert_calls = _build_service(
        attach_error=OSError('simulated request metadata failure')
    )

    result = _create(service)

    assert alert_calls and alert_calls[0]['ref_id'] == 'req-1'
    assert result['id'] == 'req-1'
    assert 'critical_alert_id' not in result
    assert 'critical_alert_id' not in stores[FakeRoadmap.STAGE_REQUESTS_FILE][0]
    assert 'Failed to attach critical alert after stage request commit' in caplog.text


def test_stage_request_persists_alert_id_on_success():
    service, stores, _alert_calls = _build_service()

    result = _create(service)

    assert result['critical_alert_id'] == 'alert-1'
    assert stores[FakeRoadmap.STAGE_REQUESTS_FILE][0]['critical_alert_id'] == 'alert-1'


def test_primary_stage_request_transaction_failure_remains_strict():
    service, stores, alert_calls = _build_service(
        primary_error=OSError('simulated request storage failure')
    )

    with pytest.raises(OSError, match='simulated request storage failure'):
        _create(service)

    assert stores[FakeRoadmap.STAGE_REQUESTS_FILE] == []
    assert alert_calls == []
