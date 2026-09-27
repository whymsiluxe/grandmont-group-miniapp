"""Worker-facing shadow-mode glue between Mini App legacy paths and the
Grandmont Core client (backend/core/grandmont_core_client.py).

Shadow mode: the legacy Mini App behavior (flat-JSON assignments/absences)
stays authoritative and unchanged for every response returned to a worker.
Every function here is best-effort and side-effect-only from the caller's
point of view -- it never raises, never mutates its inputs, and no-ops
immediately (no logging, no network) when Core integration is disabled.

Scope: worker identity resolution, worker-facing assignment shadow reads,
and worker-created absence shadow create. Does not touch DailyPlan,
checkin, execution, or CRM, and does not reinterpret Core statuses.
"""
from __future__ import annotations

import logging
from typing import Any

try:
    from .grandmont_core_client import CoreErrorKind, CoreIntegrationError, get_core_client
except ImportError:
    from grandmont_core_client import CoreErrorKind, CoreIntegrationError, get_core_client  # noqa: E402

logger = logging.getLogger('grandmont_core.shadow')


def _client_if_enabled():
    client = get_core_client()
    if not client.config.enabled:
        return None
    return client


def resolve_worker_uuid(telegram_user_id: Any) -> tuple[str | None, str | None]:
    """Resolve a Telegram user id to (core_worker_id, core_organization_id)
    via the Core identity bridge (miniapp:<telegram_user_id> -> Worker UUID).

    Returns (None, None) when integration is disabled, the worker is
    unmapped (404), or any Core failure occurs -- callers must treat all
    three uniformly as 'no shadow data available', never as a Mini-facing
    error. Never auto-creates a Worker, never invents a canonical UUID.
    """
    client = _client_if_enabled()
    if client is None:
        return None, None
    try:
        result = client.resolve_worker_identity(str(telegram_user_id))
    except CoreIntegrationError as e:
        logger.warning(
            "core shadow: identity resolve failed for legacy_id=%s kind=%s",
            telegram_user_id, e.kind.value,
        )
        return None, None
    if not result.ok or not result.data:
        logger.info("core shadow: no identity mapping for legacy_id=%s (unmapped)", telegram_user_id)
        return None, None
    worker = (result.data or {}).get('worker') or {}
    worker_id = worker.get('id')
    org_id = worker.get('organization_id')
    if not worker_id:
        return None, None
    return worker_id, org_id


def shadow_compare_assignments(telegram_user_id: Any, legacy_assignments: list[dict]) -> None:
    """Best-effort: resolve identity, fetch the worker's Core assignments,
    and log meaningful mismatches. Never raises, never mutates
    legacy_assignments, has no return value used by the caller -- the
    legacy list stays the sole source of truth for the worker-facing
    response. Does not reinterpret Core assignment statuses.
    """
    if _client_if_enabled() is None:
        return
    try:
        client = get_core_client()
        worker_id, _org_id = resolve_worker_uuid(telegram_user_id)
        if not worker_id:
            return
        result = client.list_assignments(worker_id=worker_id)
        core_assignments = (result.data or {}).get('assignments') or []
        legacy_count = len(legacy_assignments)
        core_count = len(core_assignments)
        if legacy_count != core_count:
            logger.warning(
                "core shadow mismatch: assignment count differs legacy=%s core=%s worker_id=%s",
                legacy_count, core_count, worker_id,
            )
        else:
            logger.info(
                "core shadow: assignment counts match (legacy=%s core=%s) worker_id=%s",
                legacy_count, core_count, worker_id,
            )
    except CoreIntegrationError as e:
        logger.warning("core shadow: assignment fetch failed kind=%s", e.kind.value)
    except Exception:
        logger.exception("core shadow: unexpected error comparing assignments")


def shadow_create_absence(telegram_user_id: Any, legacy_entry: dict) -> None:
    """Best-effort: resolve identity and send the same absence as a Core
    absence.create command, reusing the legacy entry's own id as the stable
    idempotency key so a caller-side retry of the same logical Mini
    operation reuses the same key (never generated inside the transport).
    Never raises -- a Core failure must not turn an already-successful
    legacy absence creation into a Mini-facing error. If no identity
    mapping exists, this is a controlled no-op, not an auto-create.
    """
    if _client_if_enabled() is None:
        return
    try:
        client = get_core_client()
        worker_id, org_id = resolve_worker_uuid(telegram_user_id)
        if not worker_id or not org_id:
            return
        idempotency_key = f"miniapp-abwesenheit-{legacy_entry.get('id')}"
        client.create_absence(
            organization_id=org_id,
            worker_id=worker_id,
            type_=legacy_entry.get('reason', ''),
            starts_at=legacy_entry.get('date_from', ''),
            ends_at=legacy_entry.get('date_to', ''),
            idempotency_key=idempotency_key,
            note=(legacy_entry.get('note') or None),
        )
        logger.info(
            "core shadow: absence create sent worker_id=%s idempotency_key=%s",
            worker_id, idempotency_key,
        )
    except CoreIntegrationError as e:
        logger.warning("core shadow: absence create failed kind=%s", e.kind.value)
    except Exception:
        logger.exception("core shadow: unexpected error creating shadow absence")
