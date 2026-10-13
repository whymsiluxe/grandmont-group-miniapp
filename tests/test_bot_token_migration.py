"""Bot token final-cutover regression tests.

The migration window is closed. BOT_TOKEN is the only accepted Telegram bot
secret for outbound sends, initData verification and backend session tokens.
Retired bot secrets must not exist as runtime fallbacks.
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from urllib.parse import quote

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
from core import telegram as telegram_module  # noqa: E402
from core import permissions as permissions_module  # noqa: E402


def _build_init_data_signed_with(bot_token, user_id=111, first_name='Test', auth_date=None, tamper=False):
    if auth_date is None:
        auth_date = int(time.time())
    user_json = json.dumps({'id': user_id, 'first_name': first_name}, separators=(',', ':'))
    params = {'auth_date': str(auth_date), 'user': user_json, 'query_id': 'AAAtest'}
    data_check_string = '\n'.join(f'{k}={v}' for k, v in sorted(params.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if tamper:
        computed_hash = ('0' if computed_hash[0] != '0' else '1') + computed_hash[1:]

    parts = [f'{k}={quote(v, safe="")}' for k, v in params.items()]
    parts.append(f'hash={computed_hash}')
    return '&'.join(parts)


def _session_token_signed_with(bot_token, user_id='5298622655', ttl=3600):
    exp = int(time.time()) + ttl
    payload = f"{user_id}.{exp}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
    secret = hmac.new(b"SessionToken", bot_token.encode(), hashlib.sha256).digest()
    sig = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


class BotTokenCutoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._current_token = telegram_module.BOT_TOKEN
        cls._retired_token = 'OLD-RETIRED-TEST-TOKEN-00000000000000'

    def test_retired_fallback_symbol_is_removed(self):
        self.assertFalse(hasattr(telegram_module, 'BOT_TOKEN_OLD'))
        self.assertFalse(hasattr(permissions_module, 'BOT_TOKEN_OLD'))

    def test_current_bot_init_data_accepted(self):
        init_data = _build_init_data_signed_with(self._current_token, user_id=42)
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)

    def test_retired_bot_init_data_rejected(self):
        init_data = _build_init_data_signed_with(self._retired_token, user_id=42)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_third_party_init_data_token_rejected(self):
        init_data = _build_init_data_signed_with('SOME-OTHER-UNRELATED-TOKEN-123456789')
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_tampered_init_data_signature_rejected(self):
        init_data = _build_init_data_signed_with(self._current_token, tamper=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_current_session_token_created_and_verified(self):
        token = backend.create_session_token(5298622655)
        self.assertEqual(backend.verify_session_token(token), '5298622655')

    def test_retired_session_secret_rejected(self):
        token = _session_token_signed_with(self._retired_token)
        with self.assertRaises(HTTPException) as ctx:
            backend.verify_session_token(token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_third_party_session_secret_rejected(self):
        token = _session_token_signed_with('SOME-OTHER-UNRELATED-TOKEN', user_id='42')
        with self.assertRaises(HTTPException) as ctx:
            backend.verify_session_token(token)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_outbound_send_uses_only_current_primary_token(self):
        self.assertEqual(telegram_module.BOT_TOKEN, self._current_token)
        self.assertFalse(hasattr(telegram_module, 'BOT_TOKEN_OLD'))


if __name__ == '__main__':
    unittest.main()
