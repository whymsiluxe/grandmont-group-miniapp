"""Bot token migration regression tests (2026-10 Telegram bot cutover).

validate_init_data() must accept initData signed by EITHER the old bot
(BOT_TOKEN) or the new bot (BOT_TOKEN_NEW) while BOT_TOKEN_NEW is set in env
-- see backend/core/permissions.py's validate_init_data docstring. Same
genuine-HMAC-signing approach as tests/test_access_control.py, not mocked.

Run:
    cd miniapp-repo && python3 -m unittest tests.test_bot_token_migration -v
(same environment requirements as test_access_control.py: BOT_TOKEN in env,
plus BOT_TOKEN_NEW for these migration-specific tests, run with the miniapp
.venv's python3.)
"""
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
    """Same construction as test_access_control.py's _build_init_data, but the
    signer's bot_token is explicit so a test can sign with OLD or NEW on demand."""
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


class BotTokenMigrationTests(unittest.TestCase):
    """Dual-token verification window: OLD bot keeps working, NEW bot starts
    working, a fake/third token is still rejected either way."""

    @classmethod
    def setUpClass(cls):
        # Real env BOT_TOKEN (required for the whole test module to import
        # main.py at all) is the "OLD" token for these tests. A separate
        # fixed "NEW" token is injected directly into every module-level
        # name that holds a copy of it -- `from telegram import BOT_TOKEN_NEW`
        # in permissions.py binds its OWN module-global at import time, so
        # patching telegram_module.BOT_TOKEN_NEW alone does not propagate
        # (confirmed: that was this test file's own first-draft bug, not a
        # production bug -- production reads env once at process start,
        # same as BOT_TOKEN always has). Patch both bindings explicitly.
        cls._old_token = telegram_module.BOT_TOKEN
        cls._new_token = 'NEW-TEST-TOKEN-0000000000000000000000'
        cls._prior_new_token_telegram = telegram_module.BOT_TOKEN_NEW
        cls._prior_new_token_permissions = permissions_module.BOT_TOKEN_NEW
        telegram_module.BOT_TOKEN_NEW = cls._new_token
        permissions_module.BOT_TOKEN_NEW = cls._new_token

    @classmethod
    def tearDownClass(cls):
        telegram_module.BOT_TOKEN_NEW = cls._prior_new_token_telegram
        permissions_module.BOT_TOKEN_NEW = cls._prior_new_token_permissions

    def test_old_bot_init_data_accepted_during_migration(self):
        init_data = _build_init_data_signed_with(self._old_token, user_id=42)
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)

    def test_new_bot_init_data_accepted_during_migration(self):
        init_data = _build_init_data_signed_with(self._new_token, user_id=42)
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)

    def test_same_user_id_resolves_identically_regardless_of_signer(self):
        # Canonical identity (section 2 of the migration task) -- the SAME
        # telegram_user_id must come out of validate_init_data no matter
        # which of the two bots signed the initData. Core worker-identity
        # resolution (grandmont_core_client.resolve_worker_identity) keys
        # off this value, so if this test passes, identity resolution is
        # unaffected by which bot the request came through.
        old_signed = _build_init_data_signed_with(self._old_token, user_id=5298622655)
        new_signed = _build_init_data_signed_with(self._new_token, user_id=5298622655)
        user_via_old = backend.validate_init_data(old_signed)
        user_via_new = backend.validate_init_data(new_signed)
        self.assertEqual(user_via_old['id'], user_via_new['id'])

    def test_third_party_token_still_rejected(self):
        # Neither OLD nor NEW -- must not be accepted just because dual-token
        # mode is on. Dual-token means "two specific tokens", not "any token".
        fake_token = 'SOME-OTHER-UNRELATED-TOKEN-123456789'
        init_data = _build_init_data_signed_with(fake_token)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_tampered_new_bot_signature_still_rejected(self):
        init_data = _build_init_data_signed_with(self._new_token, tamper=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_new_bot_token_rejected_once_migration_window_closed(self):
        # Simulates post-cutover cleanup: BOT_TOKEN_NEW unset -> NEW-signed
        # initData must stop working (old behavior fully restored), while
        # OLD-signed initData (now effectively the retired bot, if cutover
        # flipped which token is "primary") still validates against
        # whatever BOT_TOKEN currently is.
        telegram_module.BOT_TOKEN_NEW = ''
        permissions_module.BOT_TOKEN_NEW = ''
        try:
            init_data = _build_init_data_signed_with(self._new_token)
            with self.assertRaises(HTTPException) as ctx:
                backend.validate_init_data(init_data)
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            telegram_module.BOT_TOKEN_NEW = self._new_token
            permissions_module.BOT_TOKEN_NEW = self._new_token


if __name__ == '__main__':
    unittest.main()
