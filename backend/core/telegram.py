"""Minimal Telegram Bot API sender — Phase A extraction from main.py.

Extracted specifically to unblock core/permissions.py (Phase A dependency-map
risk R4): get_current_user() -> _notify_owner_new_user() -> send_telegram_message()
was the one upward edge out of the permissions chain back into main.py-local
code. Moving just this leaf function here means core/permissions.py can import
it without creating main.py -> core.permissions -> main.py cycle.

send_pdf_to_chat() (send_telegram_message's sibling) stays in main.py --
nothing in the permissions chain needs it, no reason to move it.
"""
import json
import os
import urllib.request as _urlreq

BOT_TOKEN = os.environ['BOT_TOKEN']

# Bot token migration -- FINAL CUTOVER (2026-10-04): BOT_TOKEN is now the NEW
# bot (@GrandMont_bot) -- it is primary for outbound sends below AND for
# signing new session tokens/initData verification. BOT_TOKEN_OLD is the
# RETIRED bot (@promonta_bot), kept ONLY as a verification fallback so
# sessions/initData signed before cutover keep working for their remaining
# TTL -- never used to sign anything new, never used for outbound. Empty
# string, not unset, when no migration is in progress, so permissions.py's
# `if BOT_TOKEN_OLD` check is a plain falsy check either way.
#
# Migration window: delete BOT_TOKEN_OLD (env var + this fallback code path
# in core/permissions.py) no earlier than 13h after cutover deploy time
# (12h session TTL + 1h clock-skew/deploy-timing buffer) -- see
# docs/DECISIONS.md's final-cutover entry for the exact window start time.
BOT_TOKEN_OLD = os.environ.get('BOT_TOKEN_OLD', '')


def send_telegram_message(chat_id, text):
    """sendMessage через Bot API — тем же стандартно-библиотечным путём, что send_pdf_to_chat."""
    body = json.dumps({'chat_id': chat_id, 'text': text}).encode()
    req = _urlreq.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'}
    )
    _urlreq.urlopen(req, timeout=10)
