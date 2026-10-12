import json
import os
import time
from datetime import datetime, timezone
from typing import Callable


def read_app_version(app_version_file: str) -> dict:
    if not os.path.isfile(app_version_file):
        return {"version": "unknown", "commit": "unknown"}
    try:
        with open(app_version_file, encoding="utf-8") as f:
            data = json.load(f)
        return {"version": data.get("version", "unknown"), "commit": data.get("commit", "unknown")}
    except (json.JSONDecodeError, OSError):
        return {"version": "unknown", "commit": "unknown"}


def health_response(app_version_file: str) -> dict:
    version_info = read_app_version(app_version_file)
    return {
        "status": "ok",
        "service": "grandmont-group-miniapp",
        "version": version_info["version"],
        "commit": version_info["commit"],
        "time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
    }


def readiness_response(
    *,
    object_photo_dir: str,
    chat_attach_dir: str,
    tools_lib_path: str,
    mangel_lib_path: str,
    backend_dir: str,
    roles_file: str,
) -> dict:
    checks = {}

    storage_dirs = [
        ("object_photos", object_photo_dir),
        ("chat_attachments", chat_attach_dir),
    ]
    storage_ok = all(os.path.isdir(path) and os.access(path, os.W_OK) for _, path in storage_dirs)
    checks["storage"] = "ok" if storage_ok else "error"

    try:
        probe_path = os.path.join(object_photo_dir, f".health-probe-{os.getpid()}")
        with open(probe_path, "wb") as f:
            f.write(b"ok")
        os.remove(probe_path)
        checks["uploads"] = "ok"
    except OSError:
        checks["uploads"] = "error"

    checks["tools_lib"] = "ok" if os.path.isfile(tools_lib_path) else "missing"
    checks["mangel_lib"] = "ok" if os.path.isfile(mangel_lib_path) else "missing"
    checks["work_types"] = "ok" if os.path.isfile(os.path.join(backend_dir, "work_types.py")) else "missing"
    checks["profile_skills"] = "ok" if os.path.isfile(os.path.join(backend_dir, "profile_skills.py")) else "missing"
    checks["assignment_matching"] = "ok" if os.path.isfile(os.path.join(backend_dir, "assignment_matching.py")) else "missing"
    checks["roles_file"] = "ok" if os.path.isfile(roles_file) else "missing"

    overall_ok = all(v == "ok" for v in checks.values())
    return {
        "status": "ready" if overall_ok else "degraded",
        "checks": checks,
    }


def diagnostics_response(
    *,
    data_root: str,
    roles_file: str,
    sheets_cache: dict,
    sheets_cache_ttl: int,
    activity_alerts_file: str,
    news_feed_file: str,
    chat_file: str,
    plan_sync_state_file: str,
    contract_ingest_state_file: str,
    contracts_drive_folder_id: str,
    app_version_file: str,
    safe_load_json: Callable,
    outbox_dead_letter_count: Callable[[], int],
) -> dict:
    now = time.time()
    result = {}

    data_root_ok = os.path.isdir(data_root) and os.access(data_root, os.R_OK)
    roles_ok = os.path.isfile(roles_file)
    result["backend"] = "ok" if (data_root_ok and roles_ok) else "degraded"

    sheets_entries = [(ts, tab) for tab, (ts, _) in sheets_cache.items()]
    if sheets_entries:
        last_ts, _ = max(sheets_entries, key=lambda x: x[0])
        age = int(now - last_ts)
        result["sheets"] = "ok" if age < sheets_cache_ttl * 4 else "stale"
        result["sheets_last_read_s"] = age
    else:
        result["sheets"] = "not_loaded"
        result["sheets_last_read_s"] = None

    obj_cache = sheets_cache.get("Объекты")
    if obj_cache:
        _, rows = obj_cache
        obj_count = max(0, len(rows) - 1)
        result["objects"] = f"{obj_count} objects"
    else:
        result["objects"] = "not_loaded"

    feed_ok = os.path.isfile(activity_alerts_file)
    news_ok = os.path.isfile(news_feed_file)
    result["feed"] = ("ok" if feed_ok else "missing_alerts") + ("" if news_ok else "+news_missing")
    if result["feed"] == "ok":
        result["feed"] = "ok"

    result["chat"] = "ok" if os.path.isfile(chat_file) else "missing"

    sync_state = safe_load_json(plan_sync_state_file, {})
    if sync_state.get("last_sync_at"):
        sync_age = int(now - sync_state["last_sync_at"])
        result["dailyplan_sync"] = "ok" if sync_age < 3600 else "stale"
        result["dailyplan_sync_age_s"] = sync_age
    elif os.path.isfile(plan_sync_state_file):
        result["dailyplan_sync"] = "file_exists_no_sync"
        result["dailyplan_sync_age_s"] = None
    else:
        result["dailyplan_sync"] = "not_configured"
        result["dailyplan_sync_age_s"] = None

    dead_letter_count = outbox_dead_letter_count()
    result["finish_outbox"] = "red" if dead_letter_count > 0 else "ok"
    result["finish_outbox_dead_letter_count"] = dead_letter_count

    result["drive_contracts"] = "configured" if contracts_drive_folder_id else "not_configured"
    result["contracts_ingested"] = len(
        safe_load_json(contract_ingest_state_file, {}).get("contracts", {})
        if os.path.isfile(contract_ingest_state_file)
        else {}
    )

    version_info = read_app_version(app_version_file)
    result["build_sha"] = version_info["commit"]
    result["build_version"] = version_info["version"]

    # 22.09 (iPhone screenshot audit): `overall` used to ONLY look at
    # backend/sheets/chat, silently ignoring dailyplan_sync/drive_contracts/
    # finish_outbox entirely -- a real device showed a green "✅ Все системы
    # работают" headline directly above three yellow warning rows for exactly
    # those ignored fields, visibly contradicting itself. Explicit severity
    # model instead of an ad-hoc field allowlist:
    #   REQUIRED_OK: backend/sheets/chat must be "ok" (or sheets "stale",
    #     which is a real degradation, not a fresh problem) -- any other
    #     value here is a genuine core failure -> overall "error".
    #   OPTIONAL: dailyplan_sync/drive_contracts are integrations that are
    #     legitimately "not_configured" in normal operation -- that state
    #     does NOT drag the core status down, but it IS surfaced as an
    #     explicit warning rather than silently blended into a plain "ok".
    #   finish_outbox: "red" (a dead-letter outbox record exists) is a real
    #     operational problem, not an optional-integration absence -> warning.
    REQUIRED_OK_VALUES = {"backend": {"ok"}, "sheets": {"ok", "stale"}, "chat": {"ok"}}
    required_failed = [k for k, ok_values in REQUIRED_OK_VALUES.items() if result.get(k) not in ok_values]

    OPTIONAL_NOT_CONFIGURED_IS_FINE = {"dailyplan_sync": {"not_configured"}, "drive_contracts": {"not_configured"}}
    optional_warnings = [
        k for k, fine_values in OPTIONAL_NOT_CONFIGURED_IS_FINE.items()
        if result.get(k) not in ("ok", "configured") and result.get(k) not in fine_values
    ]
    if result.get("finish_outbox") == "red":
        optional_warnings.append("finish_outbox")
    # dailyplan_sync/drive_contracts being "not_configured" is expected/fine,
    # but still worth a visible (non-blocking) heads-up rather than looking
    # identical to a fully-configured "ok" -- distinct from optional_warnings
    # above, which are genuine unexpected states.
    optional_not_configured = [
        k for k, fine_values in OPTIONAL_NOT_CONFIGURED_IS_FINE.items()
        if result.get(k) in fine_values
    ]

    if required_failed:
        result["overall"] = "error"
    elif optional_warnings or optional_not_configured:
        result["overall"] = "warning"
    else:
        result["overall"] = "ok"
    result["overall_required_failed"] = required_failed
    result["overall_warnings"] = optional_warnings
    result["overall_not_configured"] = optional_not_configured
    return result
