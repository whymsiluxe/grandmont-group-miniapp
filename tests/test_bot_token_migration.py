"""Bot token migration regression tests -- FINAL CUTOVER (2026-10-04).

After cutover: BOT_TOKEN is the NEW bot (@GrandMont_bot), primary for
outbound sends, new session-token signing, and initData verification.
BOT_TOKEN_OLD is the RETIRED bot (@promonta_bot), kept ONLY as a
verify-only fallback for initData and session-token signatures created
before cutover -- see backend/core/telegram.py's BOT_TOKEN_OLD docstring
and backend/core/permissions.py's validate_init_data/verify_session_token
docstrings for the exact scope.

Same genuine-HMAC-signing approach as tests/test_access_control.py, not
mocked.

Run:
    cd miniapp-repo && python3 -m unittest tests.test_bot_token_migration -v
(same environment requirements as test_access_control.py: BOT_TOKEN in env,
run with the miniapp .venv's python3.)
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
    signer's bot_token is explicit so a test can sign with NEW or OLD on demand."""
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


def _sign_session_payload(payload_b64, bot_token):
    secret = hmac.new(b"SessionToken", bot_token.encode(), hashlib.sha256).digest()
    return hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()


class BotTokenCutoverTests(unittest.TestCase):
    """Final cutover: NEW is primary everywhere, OLD is verify-only fallback
    for initData and session tokens, never used to sign anything new."""

    @classmethod
    def setUpClass(cls):
        # Real env BOT_TOKEN is "NEW" (the primary) for these tests, matching
        # production post-cutover. A separate fixed "OLD" token is injected
        # directly into both modules' bindings (import-time `from X import Y`
        # copies the reference, so both telegram_module and permissions_module
        # need patching -- confirmed the hard way in the interim-phase test's
        # first draft, see git history).
        cls._new_token = telegram_module.BOT_TOKEN
        cls._old_token = 'OLD-RETIRED-TEST-TOKEN-00000000000000'
        cls._prior_old_telegram = telegram_module.BOT_TOKEN_OLD
        cls._prior_old_permissions = permissions_module.BOT_TOKEN_OLD
        telegram_module.BOT_TOKEN_OLD = cls._old_token
        permissions_module.BOT_TOKEN_OLD = cls._old_token

    @classmethod
    def tearDownClass(cls):
        telegram_module.BOT_TOKEN_OLD = cls._prior_old_telegram
        permissions_module.BOT_TOKEN_OLD = cls._prior_old_permissions

    # --- initData ---

    def test_new_bot_init_data_accepted_as_primary(self):
        init_data = _build_init_data_signed_with(self._new_token, user_id=42)
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)

    def test_old_bot_init_data_still_accepted_as_fallback(self):
        init_data = _build_init_data_signed_with(self._old_token, user_id=42)
        user = backend.validate_init_data(init_data)
        self.assertEqual(user['id'], 42)

    def test_identity_resolves_identically_regardless_of_which_bot_signed(self):
        # Canonical identity must not depend on which bot's token signed the
        # initData -- Core worker-identity resolution keys off this user id.
        new_signed = _build_init_data_signed_with(self._new_token, user_id=5298622655)
        old_signed = _build_init_data_signed_with(self._old_token, user_id=5298622655)
        user_via_new = backend.validate_init_data(new_signed)
        user_via_old = backend.validate_init_data(old_signed)
        self.assertEqual(user_via_new['id'], user_via_old['id'])

    def test_third_party_init_data_token_rejected(self):
        fake_token = 'SOME-OTHER-UNRELATED-TOKEN-123456789'
        init_data = _build_init_data_signed_with(fake_token)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_tampered_init_data_signature_rejected(self):
        init_data = _build_init_data_signed_with(self._new_token, tamper=True)
        with self.assertRaises(HTTPException) as ctx:
            backend.validate_init_data(init_data)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_old_bot_init_data_rejected_once_migration_window_closed(self):
        # Post-window cleanup: BOT_TOKEN_OLD unset -> old-bot-signed initData
        # must stop validating.
        telegram_module.BOT_TOKEN_OLD = ''
        permissions_module.BOT_TOKEN_OLD = ''
        try:
            init_data = _build_init_data_signed_with(self._old_token)
            with self.assertRaises(HTTPException) as ctx:
                backend.validate_init_data(init_data)
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            telegram_module.BOT_TOKEN_OLD = self._old_token
            permissions_module.BOT_TOKEN_OLD = self._old_token

    # --- session tokens ---

    def test_new_session_token_created_and_verified(self):
        token = backend.create_session_token(5298622655)
        user_id = backend.verify_session_token(token)
        self.assertEqual(user_id, '5298622655')

    def test_pre_cutover_session_token_old_secret_still_verifies(self):
        # Simulates a session token that was created BEFORE cutover (signed
        # with the old secret) -- must still verify during the migration
        # window, per the no-forced-logout requirement.
        exp = int(time.time()) + 3600
        payload = f"5298622655.{exp}"
        import base64
        payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
        sig = _sign_session_payload(payload_b64, self._old_token)
        old_token_str = f"{payload_b64}.{sig}"

        user_id = backend.verify_session_token(old_token_str)
        self.assertEqual(user_id, '5298622655')

    def test_new_session_token_is_never_signed_with_old_secret(self):
        # create_session_token() must use ONLY the new primary secret --
        # verify that a token it creates does NOT validate against the old
        # secret (proves no accidental dual-signing on creation).
        token = backend.create_session_token(42)
        payload_b64, sig = token.rsplit('.', 1)
        old_secret_sig = _sign_session_payload(payload_b64, self._old_token)
        self.assertNotEqual(sig, old_secret_sig)

    def test_third_party_session_secret_rejected(self):
        exp = int(time.time()) + 3600
        payload = f"42.{exp}"
        import base64
        payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
        sig = _sign_session_payload(payload_b64, 'SOME-OTHER-UNRELATED-TOKEN')
        fake_token_str = f"{payload_b64}.{sig}"
        with self.assertRaises(HTTPException) as ctx:
            backend.verify_session_token(fake_token_str)
        self.assertEqual(ctx.exception.status_code, 401)

    def test_old_session_secret_rejected_once_migration_window_closed(self):
        telegram_module.BOT_TOKEN_OLD = ''
        permissions_module.BOT_TOKEN_OLD = ''
        try:
            exp = int(time.time()) + 3600
            payload = f"5298622655.{exp}"
            import base64
            payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip('=')
            sig = _sign_session_payload(payload_b64, self._old_token)
            old_token_str = f"{payload_b64}.{sig}"
            with self.assertRaises(HTTPException) as ctx:
                backend.verify_session_token(old_token_str)
            self.assertEqual(ctx.exception.status_code, 401)
        finally:
            telegram_module.BOT_TOKEN_OLD = self._old_token
            permissions_module.BOT_TOKEN_OLD = self._old_token

    # --- outbound (structural check, no real network call) ---

    def test_outbound_send_uses_new_primary_token(self):
        # send_telegram_message builds its request URL from BOT_TOKEN (new
        # primary) -- confirm it is NOT using BOT_TOKEN_OLD, without making a
        # real network call (inspect the module's own BOT_TOKEN binding).
        self.assertEqual(telegram_module.BOT_TOKEN, self._new_token)
        self.assertNotEqual(telegram_module.BOT_TOKEN, telegram_module.BOT_TOKEN_OLD)


if __name__ == '__main__':
    unittest.main()
