import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

import core.permissions as permissions
import core.telegram as telegram


def _signed_init_data(bot_token: str, user_id: int = 123) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(
        secret, data_check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


def _session_signed_with(bot_token: str, user_id: str = "123") -> str:
    payload = f"{user_id}.{int(time.time()) + 300}"
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    secret = hmac.new(b"SessionToken", bot_token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{signature}"


def test_retired_bot_secret_is_not_loaded_or_accepted():
    assert not hasattr(telegram, "BOT_TOKEN_OLD")
    assert not hasattr(permissions, "BOT_TOKEN_OLD")

    assert permissions.validate_init_data(
        _signed_init_data(telegram.BOT_TOKEN)
    )["id"] == 123

    retired = "retired-bot-token-that-must-never-work"
    with pytest.raises(HTTPException) as exc:
        permissions.validate_init_data(_signed_init_data(retired))
    assert exc.value.status_code == 401

    with pytest.raises(HTTPException) as exc:
        permissions.verify_session_token(_session_signed_with(retired))
    assert exc.value.status_code == 401


def test_cleanup_timer_uses_active_data_root():
    import cleanup_old_attachments as cleanup

    expected_root = os.environ["MINIAPP_DATA_ROOT"] + os.sep
    assert cleanup.DATA_ROOT == os.environ["MINIAPP_DATA_ROOT"]
    assert cleanup.DIRS
    assert all(path.startswith(expected_root) for path in cleanup.DIRS)
