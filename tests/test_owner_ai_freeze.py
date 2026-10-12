"""03.08 (ТЗ Задача 2): safe freeze Owner AI (Claude CLI subprocess) в production.
OWNER_AI_ENABLED default false -- subprocess не должен вызываться вообще, пока флаг
выключен. После extraction security controls принадлежат routes.ai_chat, поэтому
patch направлен на канонический AI-модуль, а compatibility call остаётся через main.
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

from fastapi import HTTPException  # noqa: E402
import main as backend  # noqa: E402
import routes.ai_chat as ai_domain  # noqa: E402


class OwnerAiDisabledByDefaultTests(unittest.TestCase):
    def test_subprocess_never_called_when_disabled(self):
        with patch.object(ai_domain, 'OWNER_AI_ENABLED', False), \
             patch.object(ai_domain.subprocess, 'run') as mock_run:
            with self.assertRaises(HTTPException) as ctx:
                backend._call_claude_cli([{'role': 'user', 'content': 'hi'}], 'sonnet')
        mock_run.assert_not_called()
        self.assertEqual(ctx.exception.status_code, 503)

    def test_disabled_message_mentions_glm_fallback(self):
        with patch.object(ai_domain, 'OWNER_AI_ENABLED', False), \
             patch.object(ai_domain.subprocess, 'run') as mock_run:
            with self.assertRaises(HTTPException) as ctx:
                backend._call_claude_cli([{'role': 'user', 'content': 'hi'}], 'opus')
        mock_run.assert_not_called()
        self.assertIn('GLM', ctx.exception.detail)


class OwnerAiEnabledTests(unittest.TestCase):
    def test_subprocess_called_when_explicitly_enabled(self):
        fake_result = MagicMock(returncode=0, stdout='ok reply', stderr='')
        with patch.object(ai_domain, 'OWNER_AI_ENABLED', True), \
             patch.object(ai_domain.subprocess, 'run', return_value=fake_result) as mock_run:
            reply = backend._call_claude_cli([{'role': 'user', 'content': 'hi'}], 'sonnet')
        mock_run.assert_called_once()
        self.assertEqual(reply, 'ok reply')

    def test_dangerously_skip_permissions_flag_removed(self):
        fake_result = MagicMock(returncode=0, stdout='ok', stderr='')
        with patch.object(ai_domain, 'OWNER_AI_ENABLED', True), \
             patch.object(ai_domain.subprocess, 'run', return_value=fake_result) as mock_run:
            backend._call_claude_cli([{'role': 'user', 'content': 'hi'}], 'sonnet')
        called_args = mock_run.call_args[0][0]
        self.assertNotIn('--dangerously-skip-permissions', called_args)

    def test_subprocess_env_is_allowlisted_not_full_environ(self):
        fake_result = MagicMock(returncode=0, stdout='ok', stderr='')
        with patch.object(ai_domain, 'OWNER_AI_ENABLED', True), \
             patch.object(ai_domain.os, 'environ', {'BOT_TOKEN': 'secret', 'PATH': '/usr/bin', 'HOME': '/root'}), \
             patch.object(ai_domain.subprocess, 'run', return_value=fake_result) as mock_run:
            backend._call_claude_cli([{'role': 'user', 'content': 'hi'}], 'sonnet')
        env_used = mock_run.call_args[1]['env']
        self.assertNotIn('BOT_TOKEN', env_used)
        self.assertIn('PATH', env_used)
        self.assertIn('HOME', env_used)


class OwnerAiDefaultFlagValueTests(unittest.TestCase):
    def test_module_loaded_with_disabled_flag_in_this_test_env(self):
        if 'OWNER_AI_ENABLED' not in os.environ:
            self.assertFalse(ai_domain.OWNER_AI_ENABLED)


if __name__ == '__main__':
    unittest.main()
