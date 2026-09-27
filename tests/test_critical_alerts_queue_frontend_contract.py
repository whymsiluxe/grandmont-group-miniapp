"""22.09 iPhone screenshot audit: critical-alerts.js's poll loop and queue
management, found duplicating alert popups live on a real device.

Root causes fixed:
  - _pollCriticalAlerts() wholesale-replaced the local queue with the raw
    server response every 15s, with no id-based dedup against what was
    already queued/shown -- merges by id now instead.
  - _closeCriticalAlertModal() removed the "current" alert by ARRAY POSITION
    (queue.shift()), not by the id of the alert actually being closed -- if a
    poll had reordered/replaced the queue while a modal was open, shift()
    could remove the wrong entry. Removes by id now.
  - No local "already shown" or "already acked" tracking -- a stale/in-flight
    poll response landing after an ack could resurrect the same alert.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CRITICAL_ALERTS_JS = ROOT / "frontend" / "js" / "critical-alerts.js"


def _source() -> str:
    return CRITICAL_ALERTS_JS.read_text(encoding="utf-8")


def _fn(src: str, signature: str) -> str:
    start = src.index(signature)
    depth = 0
    i = src.index("{", start + len(signature))
    j = i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1


def test_poll_merges_by_id_instead_of_wholesale_replacing_the_queue():
    body = _fn(_source(), "async function _pollCriticalAlerts()")
    assert "_criticalAlertQueue = data.alerts" not in body, (
        "must not wholesale-replace the queue with the raw poll response -- "
        "merge by id instead, or a stale/duplicate server response can "
        "resurrect or duplicate an alert already queued/shown"
    )
    assert "existingIds" in body
    # 27.09: existingIds must now be UPDATED inside the loop too, not just
    # read -- the original one-line form left it a snapshot taken before the
    # loop, so a poll response containing the same id twice (backend
    # aggregation glitch, or a stale+fresh copy) pushed both copies, since
    # neither push updated the set the other was checked against.
    assert "existingIds.add(alert.id)" in body
    assert "_criticalAlertQueue.push(alert)" in body
    add_idx = body.index("existingIds.add(alert.id)")
    push_idx = body.index("_criticalAlertQueue.push(alert)")
    assert add_idx < push_idx, "existingIds must be marked before the push, not after"


def test_poll_filters_out_already_acked_alerts_before_merging():
    body = _fn(_source(), "async function _pollCriticalAlerts()")
    assert "_criticalAlertAckedIds.has(a.id)" in body


def test_poll_reconciles_queue_against_fresh_server_state():
    # Anything the server no longer lists as pending should drop from the
    # local queue too (acked from another device, resolved, etc.) -- except
    # the alert currently on screen, which finishes its own close flow first.
    body = _fn(_source(), "async function _pollCriticalAlerts()")
    assert "freshIds.has(a.id) || a.id === openId" in body


def test_show_next_never_reshows_an_already_shown_alert_id():
    src = _source()
    assert "let _criticalAlertShownIds = new Set();" in src
    body = _fn(src, "function _showNextCriticalAlert()")
    assert "_criticalAlertShownIds.has(a.id)" in body

    show_modal_body = _fn(src, "function _showCriticalAlertModal(alert)")
    assert "_criticalAlertShownIds.add(alert.id);" in show_modal_body


def test_close_removes_by_id_not_array_position():
    src = _source()
    assert "_criticalAlertQueue.shift()" not in src, (
        "must not remove from the queue by array position -- if a poll "
        "reordered/replaced the queue while a modal was open, shift() could "
        "remove the wrong alert entirely"
    )
    body = _fn(src, "function _closeCriticalAlertModal(closedAlertId)")
    assert "_criticalAlertQueue = _criticalAlertQueue.filter(a => a.id !== closedAlertId);" in body
    assert "_showNextCriticalAlert()" in body


def test_ack_and_resolve_both_mark_the_alert_acked_before_closing():
    src = _source()
    ack_body = _fn(src, "async function _ackCriticalAlert(alertId)")
    assert "_criticalAlertAckedIds.add(alertId);" in ack_body
    assert "_closeCriticalAlertModal(alertId);" in ack_body

    resolve_body = _fn(src, "async function _submitCriticalAlertResolution(alertId, resolution, note, files)")
    assert "_criticalAlertAckedIds.add(alertId);" in resolve_body
    assert "_closeCriticalAlertModal(alertId);" in resolve_body
