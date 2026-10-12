#!/usr/bin/env python3
"""One-shot branch-local transform for Phase 1 tools extraction.

This script is intentionally temporary. It replaces the legacy tools block in
backend/main.py with router wiring and compatibility aliases, then is removed by
the branch-only workflow that executes it.
"""
from pathlib import Path

path = Path("backend/main.py")
source = path.read_text(encoding="utf-8")
start_marker = '\n@app.get("/api/tools")\ndef list_tools('
end_marker = '\n# ---------- Angebot generator ----------'

start = source.find(start_marker)
if start < 0:
    raise SystemExit("tools block start marker not found")
end = source.find(end_marker, start)
if end < 0:
    raise SystemExit("tools block end marker not found")

replacement = r'''
# ---------- Tools domain ----------
# The tools HTTP/business flow lives in routes/tools.py. Dependencies are injected
# here so the route module does not import main.py and therefore cannot form a
# circular dependency. Public names are temporarily re-exported for existing tests
# and callers while the monolith is decomposed incrementally.
if __package__:
    from .routes.tools import (
        CheckoutBody,
        NewToolBody,
        ToolBookingBody,
        ToolUpdateBody,
        ToolsRouteDeps,
        create_tools_router,
        holder_name_from_user as _holder_name_from_user,
        lock_for_tool as _lock_for_tool,
        parse_tool_booking_date as _parse_tool_booking_date,
        tool_booking_default_store as _tool_booking_default_store,
        tool_booking_overlaps as _tool_booking_overlaps,
    )
else:
    from routes.tools import (  # noqa: E402
        CheckoutBody,
        NewToolBody,
        ToolBookingBody,
        ToolUpdateBody,
        ToolsRouteDeps,
        create_tools_router,
        holder_name_from_user as _holder_name_from_user,
        lock_for_tool as _lock_for_tool,
        parse_tool_booking_date as _parse_tool_booking_date,
        tool_booking_default_store as _tool_booking_default_store,
        tool_booking_overlaps as _tool_booking_overlaps,
    )

_tools_router, _tools_handlers = create_tools_router(
    ToolsRouteDeps(
        get_current_user=get_current_user,
        get_role=get_role,
        require_owner=require_owner,
        # Keep these lookups dynamic: tests intentionally monkey-patch the loader
        # and TOOL_BOOKINGS_FILE, and production may override data paths at runtime.
        load_tools_lib=lambda: _load_repo_tools_lib(),
        load_worker_profiles=lambda: _load_worker_profiles(),
        sanitize_display_name=lambda value, fallback: _sanitize_display_name(value, fallback),
        business_today=lambda: business_today(),
        business_today_str=lambda: business_today_str(),
        safe_load_json=lambda path, default: _safe_load_json(path, default),
        update_json_transaction=lambda path, default_factory, mutator: update_json_transaction(
            path, default_factory, mutator
        ),
        tool_bookings_file=lambda: TOOL_BOOKINGS_FILE,
    )
)
app.include_router(_tools_router)

# Temporary compatibility surface for direct-call tests and any internal callers.
# There is no duplicated implementation here; every alias points at routes/tools.py.
list_tools = _tools_handlers.list_tools
tool_history = _tools_handlers.tool_history
list_tool_bookings = _tools_handlers.list_tool_bookings
create_tool_booking = _tools_handlers.create_tool_booking
cancel_tool_booking = _tools_handlers.cancel_tool_booking
checkout_tool = _tools_handlers.checkout_tool
return_tool = _tools_handlers.return_tool
update_tool = _tools_handlers.update_tool
create_tool = _tools_handlers.create_tool
_active_tool_booking_conflict = _tools_handlers.active_tool_booking_conflict

'''

updated = source[:start] + "\n" + replacement + source[end:]
if updated == source:
    raise SystemExit("transform produced no change")
path.write_text(updated, encoding="utf-8")
print(f"backend/main.py: removed {source.count(chr(10)) - updated.count(chr(10))} lines net")
