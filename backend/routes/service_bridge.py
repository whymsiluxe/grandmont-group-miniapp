"""Service-to-service DailyPlan bridge for CRM Worker Operations integration.

Narrow, additive surface: CRM's worker-ops workspace must not maintain a
second DailyPlan/Acceptance store (see the CRM-side integration brief this
was built for). The Mini App's DailyPlan store (backend/daily_plan_lib.py,
a local JSON file guarded by an flock) is the sole canonical source for
DailyPlan/Acceptance/Amendments -- it cannot be shared via Mongo, so CRM
reaches it over HTTP, the same way CRM already reaches Grandmont Core
(backend/core_client.py there).

Auth model: everything else in this app authenticates a human via Telegram
initData or a session Bearer token minted from it (core/permissions.py).
There is no service-to-service credential anywhere in this app yet -- this
module adds exactly one, modeled on Core's own X-Service-Token pattern
(grandmont_core.api service auth): a single shared secret
(MINIAPP_SERVICE_TOKEN env var) checked via the X-Miniapp-Service-Token
header. It identifies "this request comes from the CRM backend", not a
specific human -- the human (worker) is identified separately, by
Telegram user id, which the caller must supply explicitly (CRM resolves it
via Core's identity bridge before calling here; see CRM's
core_client.resolve_worker_identity/get_worker_identities, source_system
"miniapp"). This endpoint set trusts that CRM has already done that
resolution -- it does not implement or duplicate identity resolution
itself.

Disabled by default: if MINIAPP_SERVICE_TOKEN is unset, every route in this
module returns 503 rather than silently accepting an unauthenticated
request -- never a fail-open default.

Scope, deliberately narrow (see brief items 2/3): DailyPlan read
(today/tomorrow) and Acceptance/Amendment-acknowledgement write, because
those are the explicit P0 canonical-source-of-truth items.

Item 4 (owner decision, see CRM-side integration follow-up): CRM must NOT
start/stop a shift itself -- Mini App's checkin.py (photo capture, GPS
validation, offline idempotency) remains the ONLY writer of shift/check-in
state. This module adds two READ-ONLY endpoints (bridge_shift_active,
bridge_shift_history) so CRM can display canonical shift/worktime data
without maintaining a second store. They return the same computed-hours
values checkin.py's own consumers use (_hours_from_session /
_session_hours_live, injected via deps) -- CRM must not reimplement that
formula. Raw GPS coordinates and photo paths are deliberately excluded from
the response: CRM's UI has no current use for them, and a read-only viewer
has no reason to receive location/biometric-adjacent data it doesn't render.
"""
from __future__ import annotations

import hmac
import os
from dataclasses import dataclass
from datetime import timedelta
from types import SimpleNamespace
from typing import Callable

from fastapi import APIRouter, Depends, Header, HTTPException

try:
    from .. import daily_plan_lib as dpl
except ImportError:
    import daily_plan_lib as dpl  # noqa: E402


def _service_token_configured() -> str | None:
    return os.environ.get('MINIAPP_SERVICE_TOKEN') or None


def verify_service_token(x_miniapp_service_token: str | None = Header(default=None)) -> None:
    """Dependency: 503 if the bridge isn't configured (safe default), 401 if
    configured but the caller didn't send a matching token. Uses
    hmac.compare_digest to avoid a timing side-channel on the comparison,
    same rationale as any bearer-token check."""
    expected = _service_token_configured()
    if not expected:
        raise HTTPException(503, "CRM bridge отключён (MINIAPP_SERVICE_TOKEN не задан)")
    if not x_miniapp_service_token or not hmac.compare_digest(x_miniapp_service_token, expected):
        raise HTTPException(401, "Неверный service token")


@dataclass(frozen=True)
class ServiceBridgeRouteDeps:
    business_today: Callable
    load_roles: Callable
    load_checkin_meta: Callable
    is_active_photo_checkin_session: Callable
    hours_from_session: Callable
    session_hours_live: Callable


def create_service_bridge_router(deps: ServiceBridgeRouteDeps):
    router = APIRouter()

    def _require_known_worker(telegram_user_id: str) -> None:
        roles = deps.load_roles()
        if str(telegram_user_id) not in roles:
            raise HTTPException(404, "Работник не найден в Mini App (нет роли/доступа)")

    def _build_plan_response(worker_id: str, date_str: str) -> dict:
        plan = dpl.get_today_plan_for_worker(worker_id, date_str)
        if not plan:
            return {"has_plan": False, "date": date_str}
        carryovers = dpl.get_carryovers_for_worker(worker_id, date_str)
        acceptance = dpl.get_acceptance(plan["id"], worker_id)
        amendments = dpl.get_pending_amendments(plan["id"], worker_id)
        accepted_items = dpl.get_accepted_snapshot(plan["id"], worker_id) if acceptance else plan["items"]
        return {
            "has_plan": True,
            "plan": {
                "id": plan["id"],
                "object_id": plan["object_id"],
                "stage_key": plan["stage_key"],
                "date": plan["date"],
                "status": plan["status"],
                "version": plan["version"],
                "items": accepted_items,
                "published_at": plan.get("published_at"),
            },
            "acceptance": acceptance,
            "pending_amendments": amendments,
            "carryovers": carryovers,
            "date": date_str,
        }

    @router.get("/api/bridge/daily-plan", dependencies=[Depends(verify_service_token)])
    def bridge_daily_plan(telegram_user_id: str, day: str = 'today'):
        """Same response shape as GET /api/daily-plan/today?day=..., for a
        worker identified by Telegram user id (resolved by the caller via
        Core's identity bridge) instead of the calling human's own session."""
        if day not in ('today', 'tomorrow'):
            raise HTTPException(400, "day должен быть 'today' или 'tomorrow'")
        _require_known_worker(telegram_user_id)
        target_date = deps.business_today() if day == 'today' else deps.business_today() + timedelta(days=1)
        return _build_plan_response(str(telegram_user_id), target_date.strftime('%Y-%m-%d'))

    @router.post("/api/bridge/daily-plan/{plan_id}/accept", dependencies=[Depends(verify_service_token)])
    def bridge_accept_plan(plan_id: str, telegram_user_id: str):
        """Same semantics as POST /api/daily-plan/{id}/accept — CRM's
        "ПЛАН ПОНЯТЕН — БЕРУ В РАБОТУ" action must create/update this exact
        acceptance record, not a CRM-local copy, so Mini App sees it
        immediately and vice versa."""
        _require_known_worker(telegram_user_id)
        plan = dpl.get_plan(plan_id)
        if not plan:
            raise HTTPException(404, "План не найден")
        try:
            acceptance = dpl.accept_plan(plan_id, plan["version"], str(telegram_user_id))
        except PermissionError:
            raise HTTPException(403, "Работник не назначен на этот план")
        except dpl.StaleAcceptanceError as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"status": "accepted", "acceptance": acceptance}

    @router.post(
        "/api/bridge/daily-plan/{plan_id}/amendments/{amendment_id}/accept",
        dependencies=[Depends(verify_service_token)],
    )
    def bridge_accept_amendment(plan_id: str, amendment_id: str, telegram_user_id: str):
        _require_known_worker(telegram_user_id)
        try:
            amendment = dpl.acknowledge_amendment(plan_id, amendment_id, str(telegram_user_id))
        except KeyError:
            raise HTTPException(404, "Amendment не найден")
        except PermissionError:
            raise HTTPException(403, "Работник не назначен на этот план")
        return {"status": "acknowledged", "amendment": amendment}

    def _build_shift_response(session: dict, *, live: bool) -> dict:
        """Canonical shift shape for CRM. Deliberately excludes GPS coordinates,
        accuracy, geo_timestamp and photo paths -- CRM's read-only viewer has no
        current use for them (see module docstring). hours/net_seconds come from
        the SAME functions checkin.py's own dashboard/KPI consumers use, injected
        via deps -- never recomputed here or in CRM."""
        hours = deps.session_hours_live(session) if live else deps.hours_from_session(session)
        pause_seconds = max(0, int(session.get('pause_accumulated_seconds') or 0))
        return {
            "id": session.get("id"),
            "worker_telegram_id": str(session.get("user_id")),
            "object_id": session.get("object_id"),
            "date": session.get("date"),
            "daily_plan_id": session.get("daily_plan_id"),
            "daily_plan_version": session.get("daily_plan_version"),
            "daily_plan_acceptance_id": session.get("daily_plan_acceptance_id"),
            "start_at": session.get("start_at"),
            "finish_at": session.get("finish_at"),
            "is_active": deps.is_active_photo_checkin_session(session),
            "paused": bool(session.get("pause_started_at")),
            "pause_accumulated_seconds": pause_seconds,
            "net_hours": round(hours, 2),
            "net_seconds": round(hours * 3600),
        }

    @router.get("/api/bridge/shift/active", dependencies=[Depends(verify_service_token)])
    def bridge_shift_active(telegram_user_id: str):
        """Read-only canonical active-shift lookup. Never starts/stops/mutates
        anything -- CRM does not own shift lifecycle (owner decision, item 4)."""
        _require_known_worker(telegram_user_id)
        sessions = deps.load_checkin_meta()
        active = next(
            (s for s in sessions
             if str(s.get('user_id')) == str(telegram_user_id) and deps.is_active_photo_checkin_session(s)),
            None,
        )
        if not active:
            return {"has_active_shift": False}
        return {"has_active_shift": True, "shift": _build_shift_response(active, live=True)}

    @router.get("/api/bridge/shift/history", dependencies=[Depends(verify_service_token)])
    def bridge_shift_history(telegram_user_id: str, date_from: str = '', date_to: str = ''):
        """Read-only canonical shift history for one worker, optionally bounded
        by inclusive YYYY-MM-DD business-date range (checkin.py's own date_str,
        not a server timestamp -- consistent with how /api/checkin already
        filters). Never mutates."""
        _require_known_worker(telegram_user_id)
        sessions = deps.load_checkin_meta()
        rows = [s for s in sessions if str(s.get('user_id')) == str(telegram_user_id) and not s.get('manual_entry')]
        if date_from:
            rows = [s for s in rows if (s.get('date') or '') >= date_from]
        if date_to:
            rows = [s for s in rows if (s.get('date') or '') <= date_to]
        shifts = [_build_shift_response(s, live=deps.is_active_photo_checkin_session(s)) for s in rows]
        return {"shifts": shifts}

    handlers = SimpleNamespace(
        bridge_daily_plan=bridge_daily_plan,
        bridge_accept_plan=bridge_accept_plan,
        bridge_accept_amendment=bridge_accept_amendment,
        bridge_shift_active=bridge_shift_active,
        bridge_shift_history=bridge_shift_history,
        build_plan_response=_build_plan_response,
        build_shift_response=_build_shift_response,
    )
    return router, handlers
