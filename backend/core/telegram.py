"""Minimal Telegram Bot API sender — Phase A extraction from main.py.

The module intentionally exposes only the current GrandMont bot token. Retired
bot secrets are not loaded or accepted anywhere in the authentication path.
"""
import json
import os
import urllib.request as _urlreq

BOT_TOKEN = os.environ['BOT_TOKEN']


def send_telegram_message(chat_id, text):
    """Send a Telegram Bot API ``sendMessage`` request with the current bot."""
    body = json.dumps({'chat_id': chat_id, 'text': text}).encode()
    req = _urlreq.Request(
        f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage',
        data=body,
        method='POST',
        headers={'Content-Type': 'application/json'},
    )
    _urlreq.urlopen(req, timeout=10)
