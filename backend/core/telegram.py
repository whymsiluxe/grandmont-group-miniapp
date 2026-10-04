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

# Bot token migration (2026-10): set once the new bot exists, used ONLY as an
# extra accepted signer for inbound initData verification (core/permissions.py
# validate_init_data) during cutover. Outbound sends below stay on the old
# BOT_TOKEN until the migration is confirmed complete and this file's
# send_telegram_message/send_pdf_to_chat callers are deliberately flipped --
# not automatic just because this var is present. Empty string, not unset,
# when migration isn't in progress, so permissions.py's `if BOT_TOKEN_NEW`
# check is a plain falsy check either way.
BOT_TOKEN_NEW = os.environ.get('BOT_TOKEN_NEW', '')


def send_telegram_message(chat_id, text):
    """sendMessage через Bot API — тем же стандартно-библиотечным путём, что send_pdf_to_chat."""
    body = json.dumps({'chat_id': chat_id, 'text': text}).encode()
    req = _urlreq.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
        data=body, method='POST',
        headers={'Content-Type': 'application/json'}
    )
    _urlreq.urlopen(req, timeout=10)
