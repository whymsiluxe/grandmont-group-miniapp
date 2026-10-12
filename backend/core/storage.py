"""JSON storage safety layer — Phase A extraction from main.py.

CRITICAL_JSON_PATHS stays a mutable module-level set here, imported BY
REFERENCE into main.py (not copied) -- main.py populates it at end-of-file
(after all critical-store file-path constants are known) via
CRITICAL_JSON_PATHS.update({...}), the same object this module exports. This
is deliberate (Phase A dependency-map risk R3): the set is empty during this
module's own import and only fully populated once main.py finishes loading --
anything that needs the final set must read it at call time, never snapshot
it at import time.

Zero coupling to anything else in main.py besides stdlib (os, json, time,
copy, datetime) -- the one genuinely low-risk cluster in the whole Phase A
extraction, per the dependency map.
"""
import copy
import json
import os
import threading
import time
from datetime import datetime, timezone


CRITICAL_JSON_PATHS: set = set()


class CorruptJsonError(Exception):
    def __init__(self, path: str):
        self.path = path
        super().__init__(f'{path} содержит повреждённый JSON')


def _corrupt_lock_path(path: str) -> str:
    return f'{path}.corrupt-lock'


def _quarantine_corrupt_json(path: str) -> None:
    """31.07 (доп.раунд, П1): после карантина исходный файл ИСЧЕЗАЕТ с диска --
    следующий запрос видел os.path.exists(path)==False и тихо возвращался к default,
    а mutation создавала НОВЫЙ пустой store поверх (карантин переставал что-либо
    защищать со второго запроса). Постоянный маркер <path>.corrupt-lock переживает
    отсутствие исходного файла -- проверяется _safe_load_json/update_json_transaction
    ДО os.path.exists(path), так что 503 продолжает возвращаться, пока владелец не
    восстановит валидный JSON и не удалит сам маркер вручную (никакого auto-restore)."""
    lock_path = _corrupt_lock_path(path)
    if os.path.exists(lock_path):
        # Идемпотентность: уже закарантинен раньше (напр. предыдущий процесс упал
        # между quarantine и записью маркера) -- не переносить повторно, не терять
        # уже существующую quarantine-копию перезаписью новой.
        return
    ts = time.strftime('%Y%m%d-%H%M%S')
    quarantine_path = f'{path}.corrupt-{ts}'
    try:
        os.replace(path, quarantine_path)
    except OSError as e:
        print(f'ERROR: {path} corrupt JSON -- не удалось перенести в карантин: {e}')
        return
    marker = {
        "original_path": path,
        "quarantine_path": quarantine_path,
        "detected_at": int(time.time()),
        "detected_at_iso": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
    }
    try:
        with open(lock_path, 'w', encoding='utf-8') as f:
            json.dump(marker, f, ensure_ascii=False)
    except OSError as e:
        print(f'ERROR: {path} corrupt JSON -- карантин выполнен, но не удалось записать {lock_path}: {e}')
    print(f'ERROR: {path} corrupt JSON -- карантин в {quarantine_path}, marker {lock_path}, owner-alert нужен вручную')


_json_locks: dict = {}
_json_locks_guard = __import__('threading').Lock()


def _lock_for(path: str):
    """Один threading.Lock на файл — гонки read-modify-write между конкурентными
    запросами (напр. owner подтверждает заявку в момент, когда worker её закрывает)
    иначе тихо теряют одно из двух изменений (10.29 — Fable-аудит)."""
    with _json_locks_guard:
        if path not in _json_locks:
            _json_locks[path] = __import__('threading').Lock()
        return _json_locks[path]


def _atomic_write_json(path: str, data, ensure_ascii: bool = False):
    """Пишет во временный файл + os.replace — процесс, упавший посреди записи
    (напр. systemctl restart), не оставляет обрезанный JSON, который потом валит
    json.load на старте всех эндпоинтов, читающих этот стор (10.29).

    ВАЖНО: сам по себе не защищает от read-modify-write гонки -- если код делает
    `data = _safe_load_json(path, default); data[k] = v; _atomic_write_json(path, data)`,
    лок захватывается только на сам _atomic_write_json, ЧТЕНИЕ происходит СНАРУЖИ лока.
    Два параллельных запроса могут оба прочитать одинаковую версию до того как первый
    успеет записать -- второй молча затирает изменения первого. Для любого места, где
    read-modify-write должен быть атомарным (не просто "запись не оставит corrupt файл"),
    используй update_json_transaction() ниже, не _safe_load_json+_atomic_write_json
    по отдельности (28.07, real bug found by external audit, ТЗ п.5)."""
    with _lock_for(path):
        tmp_path = f'{path}.tmp-{os.getpid()}'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=ensure_ascii)
        os.replace(tmp_path, path)


def update_json_transaction(path: str, default, mutator):
    """Read-modify-write под ОДНИМ захватом лока -- закрывает гонку, которую
    _atomic_write_json сам по себе не решает (см. комментарий выше). mutator получает
    текущую структуру (или default, если файла нет/битый), мутирует её IN-PLACE
    (list.append/dict[k]=v — не return нового объекта, чтобы не плодить два разных
    паттерна использования) и может опционально вернуть значение для вызывающего кода.
    НЕ вызывать _atomic_write_json изнутри mutator -- тот же _lock_for(path) не
    reentrant, будет deadlock (threading.Lock, не RLock).

    Для путей из CRITICAL_JSON_PATHS: corrupt JSON карантинится, транзакция НЕ пишет
    default поверх файла -- поднимает CorruptJsonError, перехватывается глобальным
    _corrupt_json_handler (регистрация -- сразу после app = FastAPI(...)).

    31.07 (доп.раунд, П1): marker-файл (<path>.corrupt-lock) проверяется ДО
    os.path.exists(path) -- после карантина исходный файл физически отсутствует,
    без этой проверки транзакция бы тихо создала НОВЫЙ пустой store поверх карантина
    на первой же mutation. Пока marker существует, store остаётся заблокирован --
    снимается только вручную (см. _quarantine_corrupt_json)."""
    with _lock_for(path):
        if path in CRITICAL_JSON_PATHS and os.path.exists(_corrupt_lock_path(path)):
            raise CorruptJsonError(path)
        if not os.path.exists(path):
            data = default() if callable(default) else copy.deepcopy(default)
        else:
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except json.JSONDecodeError:
                if path in CRITICAL_JSON_PATHS:
                    _quarantine_corrupt_json(path)
                    raise CorruptJsonError(path)
                print(f'ERROR: {path} corrupt JSON in transaction, falling back to default')
                data = default() if callable(default) else copy.deepcopy(default)
        result = mutator(data)
        tmp_path = f'{path}.tmp-{os.getpid()}'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, path)
        return result



def _safe_load_json(path: str, default):
    """Единая точка для всех _load_* сторов -- corrupt JSON (диск full / kill -9
    посреди записи, до _atomic_write_json или на старых файлах без него) не должен
    ронять запрос 500-кой, деградируем к default с явным логом. Раньше только
    roles.json имел эту защиту, 17 других _load_* падали с необработанным
    JSONDecodeError на первом же corrupt-файле.

    Для путей из CRITICAL_JSON_PATHS: НЕ деградирует к default молча (последующий
    write записал бы default поверх повреждённого файла) -- карантинит файл и
    поднимает CorruptJsonError, перехватывается глобальным _corrupt_json_handler.

    31.07 (доп.раунд, П1): marker-файл проверяется ДО os.path.exists(path) -- см.
    подробное обоснование в update_json_transaction выше, тот же принцип."""
    if path in CRITICAL_JSON_PATHS and os.path.exists(_corrupt_lock_path(path)):
        raise CorruptJsonError(path)
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        if path in CRITICAL_JSON_PATHS:
            _quarantine_corrupt_json(path)
            raise CorruptJsonError(path)
        print(f'ERROR: {path} corrupt JSON, falling back to default: {default!r}')
        return default
