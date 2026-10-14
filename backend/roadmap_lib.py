"""Roadmap (План работ) data store -- categories/items/notes/photo-links keyed by
stage_key ('{object_id}-S{№ этапа}', already exists as 'ID строки этапа' column in the
Google Sheets 'Этапы' tab). Stage identity/order/status/description stay in Sheets
(objekte_lib.py) -- this module only adds the roadmap-specific detail that has no home
there: checklist categories, checklist items, per-item/per-stage notes, and structural
change-requests from workers pending owner approval.

Deliberately NOT a second stages store: every function here takes a stage_key that must
already exist in Sheets (validated by the caller in main.py via objekte_lib.find_stage_row
or equivalent), and holds no object_id/stage name/status duplicate of its own.
"""
import json
import os
import time
import uuid

# 31.07 (доп.раунд, П4): тот же MINIAPP_DATA_ROOT, что main.py использует для остальных
# runtime JSON -- без этого тесты/CI с изолированным DATA_ROOT всё равно писали бы
# roadmap.json/roadmap_stage_requests.json в реальный prod-путь.
DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')

ROADMAP_FILE = os.path.join(DATA_ROOT, 'roadmap.json')


def _default_store():
    return {"categories": {}, "items": {}, "notes": {}, "stage_blocks": {}}
    # categories:   stage_key -> [ {id, title, order} ]
    # items:        stage_key -> [ {id, category_id, title, description, status, required,
    #                               safety_critical, weight, order, assigned_user_id,
    #                               completed_by, completed_at, created_at, updated_at} ]
    # notes:        stage_key -> [ {id, item_id (nullable -- null = stage-level note),
    #                               author_id, author_name, text, created_at} ]
    # stage_blocks: stage_key -> {quick_reason, comment, photo_url, who_decides,
    #                             expected_date, blocked_at, blocked_by} -- этап-level
    #               "Сообщить о проблеме" badge, НЕ влияет на статус этапа/пункта.


# 29.07 v2 (feature freeze): checklist упрощён до open/done -- никакого blocked/skipped
# на уровне пункта, blocker -- только stage-level badge (см. stage_blocks выше).
ITEM_STATUSES = ('open', 'done')


def new_category(store: dict, stage_key: str, title: str) -> dict:
    cats = store['categories'].setdefault(stage_key, [])
    order = max([c['order'] for c in cats], default=0) + 1
    cat = {"id": uuid.uuid4().hex, "title": title, "order": order}
    cats.append(cat)
    return cat


def rename_category(store: dict, stage_key: str, category_id: str, title: str) -> bool:
    for c in store['categories'].get(stage_key, []):
        if c['id'] == category_id:
            c['title'] = title
            return True
    return False


def delete_category(store: dict, stage_key: str, category_id: str) -> bool:
    cats = store['categories'].get(stage_key, [])
    before = len(cats)
    store['categories'][stage_key] = [c for c in cats if c['id'] != category_id]
    # Items in a deleted category become uncategorized (category_id=None), not deleted --
    # matches the ТЗ instruction "owner can delete an EMPTY category"; caller in main.py
    # enforces the emptiness check before calling this, this function just performs it.
    for item in store['items'].get(stage_key, []):
        if item.get('category_id') == category_id:
            item['category_id'] = None
    return len(store['categories'][stage_key]) < before


def new_item(store: dict, stage_key: str, title: str, category_id: str | None = None,
             description: str = '', required: bool = True, safety_critical: bool = False,
             weight: int = 1) -> dict:
    items = store['items'].setdefault(stage_key, [])
    order = max([i['order'] for i in items if i.get('category_id') == category_id], default=0) + 1
    now = int(time.time())
    item = {
        "id": uuid.uuid4().hex, "category_id": category_id, "title": title,
        "description": description, "status": "open", "required": required,
        "safety_critical": safety_critical, "weight": max(1, int(weight)), "order": order,
        "assigned_user_id": None, "completed_by": None, "completed_at": None,
        "created_at": now, "updated_at": now,
    }
    items.append(item)
    return item


def _find_item(store: dict, stage_key: str, item_id: str) -> dict | None:
    return next((i for i in store['items'].get(stage_key, []) if i['id'] == item_id), None)


def update_item_status(store: dict, stage_key: str, item_id: str, status: str, user_id: str) -> dict | None:
    if status not in ITEM_STATUSES:
        raise ValueError(f'недопустимый статус пункта: {status}')
    item = _find_item(store, stage_key, item_id)
    if not item:
        return None
    # 30.07 (аудит): легаси-пункты со status='blocked' (из отменённого 5-статусного
    # workflow, ITEM_STATUSES с тех пор сузился до open/done) нельзя обычным
    # open/done-переходом смахнуть в done -- сперва нужно снять blocked явно.
    if item.get('status') == 'blocked' and status == 'done':
        raise ValueError('пункт заблокирован -- сначала снимите блокировку')
    item['updated_at'] = int(time.time())
    item['status'] = status
    if status == 'done':
        item['completed_by'] = user_id
        item['completed_at'] = int(time.time())
    else:
        item['completed_by'] = None
        item['completed_at'] = None
    return item


def delete_item(store: dict, stage_key: str, item_id: str) -> bool:
    items = store['items'].get(stage_key, [])
    before = len(items)
    store['items'][stage_key] = [i for i in items if i['id'] != item_id]
    return len(store['items'][stage_key]) < before


def edit_item(store: dict, stage_key: str, item_id: str, **fields) -> dict | None:
    item = _find_item(store, stage_key, item_id)
    if not item:
        return None
    allowed = {'title', 'description', 'required', 'safety_critical', 'weight',
               'category_id', 'assigned_user_id'}
    for k, v in fields.items():
        if k in allowed:
            item[k] = v
    item['updated_at'] = int(time.time())
    return item


def stage_progress(store: dict, stage_key: str) -> dict:
    """Weighted progress -- completed weight / total weight по всем пунктам чек-листа."""
    items = store['items'].get(stage_key, [])
    if not items:
        return {"completed_weight": 0, "total_weight": 0, "percent": 0,
                "required_open": 0, "required_total": 0}
    total_weight = sum(i['weight'] for i in items)
    completed_weight = sum(i['weight'] for i in items if i['status'] == 'done')
    required = [i for i in items if i['required']]
    required_open = sum(1 for i in required if i['status'] != 'done')
    percent = round(100 * completed_weight / total_weight) if total_weight else 0
    return {
        "completed_weight": completed_weight, "total_weight": total_weight,
        "percent": percent, "required_open": required_open, "required_total": len(required),
    }


def new_note(store: dict, stage_key: str, author_id: str, author_name: str, text: str,
             item_id: str | None = None) -> dict:
    notes = store['notes'].setdefault(stage_key, [])
    note = {
        "id": uuid.uuid4().hex, "item_id": item_id, "author_id": str(author_id),
        "author_name": author_name, "text": text.strip()[:1000], "created_at": int(time.time()),
    }
    notes.append(note)
    return note


def set_stage_block_meta(store: dict, stage_key: str, previous_status: str, blocked_by: str,
                          quick_reason: str = '', comment: str = '', photo_url: str = '',
                          who_decides: str = '', expected_date: str = '') -> dict:
    """Сохраняет meta STAGE-блокировки (не item). previous_status приходит из
    objekte_lib.stage_block() -- та же причина, что и в roadmap-item unblock: нужно
    восстановить РЕАЛЬНЫЙ статус до блокировки, не хардкод."""
    meta = {
        "previous_status": previous_status, "blocked_by": str(blocked_by),
        "quick_reason": (quick_reason or '').strip()[:200] or None,
        "comment": (comment or '').strip()[:1000] or None,
        "photo_url": photo_url or None,
        "who_decides": (who_decides or '').strip()[:200] or None,
        "expected_date": expected_date or None,
        "blocked_at": int(time.time()),
    }
    store['stage_blocks'][stage_key] = meta
    return meta


def get_stage_block_meta(store: dict, stage_key: str) -> dict | None:
    return store['stage_blocks'].get(stage_key)


def clear_stage_block_meta(store: dict, stage_key: str) -> None:
    store['stage_blocks'].pop(stage_key, None)


def stage_notes(store: dict, stage_key: str, item_id: str | None = None) -> list:
    notes = store['notes'].get(stage_key, [])
    if item_id is not None:
        return [n for n in notes if n.get('item_id') == item_id]
    return notes


def stage_snapshot(store: dict, stage_key: str) -> dict:
    """Everything the frontend needs for one stage's roadmap detail in a single read --
    avoids N+1 category/item/progress calls per stage (ТЗ п.47 'не делать отдельный запрос
    для каждого этапа')."""
    return {
        "categories": sorted(store['categories'].get(stage_key, []), key=lambda c: c['order']),
        "items": sorted(store['items'].get(stage_key, []), key=lambda i: i['order']),
        "notes_count": len(store['notes'].get(stage_key, [])),
        "progress": stage_progress(store, stage_key),
        "stage_block": store['stage_blocks'].get(stage_key),
    }


# ═══════════ Stage change requests -- worker→owner approval (29.07) ═══════════
# Owner decision (this session): worker keeps free create-stage access (existing
# behavior, unchanged), but delete / status-change / structural edits of an EXISTING
# stage now go through a request the owner approves or rejects -- "через алерт", i.e.
# the existing critical_alerts Telegram-push+ack mechanism in main.py, not a new UI
# surface. This store only holds the request queue; main.py wires it to
# _create_critical_alert() and applies the actual objekte_lib/roadmap_lib mutation
# only after the owner approves.
STAGE_REQUESTS_FILE = os.path.join(DATA_ROOT, 'roadmap_stage_requests.json')

REQUEST_KINDS = ('delete_stage', 'change_status')


def _default_requests():
    return []


def new_stage_request(requests: list, object_id: str, stage_key: str, stage_row: int,
                       kind: str, requested_by: str, requested_by_name: str,
                       payload: dict | None = None) -> dict:
    if kind not in REQUEST_KINDS:
        raise ValueError(f'недопустимый тип запроса: {kind}')
    req = {
        "id": uuid.uuid4().hex, "object_id": object_id, "stage_key": stage_key,
        "stage_row": stage_row, "kind": kind, "payload": payload or {},
        "requested_by": str(requested_by), "requested_by_name": requested_by_name,
        "status": "pending", "created_at": int(time.time()),
        "decided_at": None, "decided_by": None, "critical_alert_id": None,
    }
    requests.append(req)
    return req


def find_stage_request(requests: list, request_id: str) -> dict | None:
    return next((r for r in requests if r['id'] == request_id), None)


def decide_stage_request(requests: list, request_id: str, approve: bool, decided_by: str) -> dict | None:
    req = find_stage_request(requests, request_id)
    if not req or req['status'] != 'pending':
        return None
    req['status'] = 'approved' if approve else 'rejected'
    req['decided_at'] = int(time.time())
    req['decided_by'] = str(decided_by)
    return req


def pending_requests_for_object(requests: list, object_id: str) -> list:
    return [r for r in requests if r['object_id'] == object_id and r['status'] == 'pending']
