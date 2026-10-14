"""Mängel (defect tickets) CRUD helpers — Фаза 3."""
import json
import os
import uuid
import time
import threading

# 31.07 (доп.раунд, П4): тот же MINIAPP_DATA_ROOT, что main.py/roadmap_lib.py.
DATA_ROOT = os.environ.get('MINIAPP_DATA_ROOT', '/home/grandmont/agent/miniapp')
MANGEL_FILE = os.path.join(DATA_ROOT, 'mangel_tickets.json')


def _atomic_write(path: str, data):
    """30.07 (Release-аудит P1): было plain open(w)+json.dump на всех 4 write
    call sites ниже -- crash посреди записи оставлял бы обрезанный JSON. Тот же
    temp-file+os.replace паттерн, что и main.py:_atomic_write_json (не импортируем
    оттуда напрямую -- mangel_lib исторически не зависит от main.py, чтобы
    оставаться переиспользуемым Telegram-ботом отдельно)."""
    tmp_path = f'{path}.tmp-{os.getpid()}'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)
# 27.07 (B8): расширено с 3 до статус-набора из плана (NEW/ASSIGNED/IN_PROGRESS/
# NEEDS_REVIEW/DONE/REJECTED) -- старые немецкие значения сохранены как есть для
# обратной совместимости с уже существующими тикетами, needs_review/rejected добавлены
# поверх, не переименовывая старые (gemeldet=new, in Bearbeitung=in_progress, behoben=done).
MANGEL_STATUSES = ('gemeldet', 'in Bearbeitung', 'behoben', 'needs_review', 'rejected')
_mangel_lock = threading.Lock()


def _load() -> list:
    if os.path.exists(MANGEL_FILE):
        with open(MANGEL_FILE, encoding='utf-8') as f:
            return json.load(f)
    return []


def _save(tickets: list):
    with _mangel_lock:
        _atomic_write(MANGEL_FILE, tickets)


def list_tickets(object_id: str = None) -> list:
    tickets = _load()
    # 04.08 (Раунд 3, задача 3.3): soft-deleted тикеты не возвращаются ни в
    # списках, ни в get_ticket -- они помечены deleted_at, но остаются на диске
    # (не физически удаляем: сохраняем историю чата/audit/фото).
    tickets = [t for t in tickets if not t.get('deleted_at')]
    if object_id:
        tickets = [t for t in tickets if t.get('object_id') == object_id]
    return sorted(tickets, key=lambda t: t.get('created_at', ''), reverse=True)


def create_ticket(object_id: str, description: str, created_by: str, photo_paths: list = None, assigned_worker_id: str = '') -> dict:
    with _mangel_lock:
        tickets = _load()
        ticket = {
            'id': str(uuid.uuid4()),
            'object_id': object_id,
            'description': description,
            'photo_paths': photo_paths or [],
            'status': 'gemeldet',
            'created_by': str(created_by),
            'assigned_worker_id': assigned_worker_id or None,
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'comments': [],
        }
        tickets.append(ticket)
        _atomic_write(MANGEL_FILE, tickets)
    return ticket


def update_status(ticket_id: str, status: str) -> dict:
    if status not in MANGEL_STATUSES:
        raise ValueError(f"Статус должен быть одним из: {', '.join(MANGEL_STATUSES)}")
    with _mangel_lock:
        tickets = _load()
        for t in tickets:
            if t['id'] == ticket_id and not t.get('deleted_at'):
                t['status'] = status
                _atomic_write(MANGEL_FILE, tickets)
                return t
    raise KeyError(f"Тикет {ticket_id!r} не найден")


def add_comment(ticket_id: str, user_id: str, text: str, name: str = None) -> dict:
    with _mangel_lock:
        tickets = _load()
        for t in tickets:
            if t['id'] == ticket_id and not t.get('deleted_at'):
                comment = {
                    'id': str(uuid.uuid4()),
                    'user_id': str(user_id),
                    'name': name or str(user_id),
                    'text': text,
                    'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                }
                t.setdefault('comments', []).append(comment)
                _atomic_write(MANGEL_FILE, tickets)
                return comment
    raise KeyError(f"Тикет {ticket_id!r} не найден")


def get_ticket(ticket_id: str) -> dict:
    for t in _load():
        if t['id'] == ticket_id and not t.get('deleted_at'):
            return t
    raise KeyError(f"Тикет {ticket_id!r} не найден")


def soft_delete_ticket(ticket_id: str, deleted_by: str) -> dict:
    """04.08 (Раунд 3, задача 3.3): Owner-only мягкое удаление. Тикет остаётся
    в файле с меткой deleted_at/deleted_by -- фото на диске, история чата и
    audit не трогаются. Повторное удаление уже удалённого безопасно (идемпотентно)."""
    with _mangel_lock:
        tickets = _load()
        for t in tickets:
            if t['id'] == ticket_id:
                if not t.get('deleted_at'):
                    t['deleted_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
                    t['deleted_by'] = str(deleted_by)
                    _atomic_write(MANGEL_FILE, tickets)
                return t
    raise KeyError(f"Тикет {ticket_id!r} не найден")


def count_by_status() -> dict:
    counts = {s: 0 for s in MANGEL_STATUSES}
    for t in _load():
        if t.get('deleted_at'):
            continue
        s = t.get('status', 'gemeldet')
        if s in counts:
            counts[s] += 1
    return counts
