"""Раунд 6: profile-completion (имя/дата рождения), приватность birthday, CSV-имя,
пересылка комментариев и in-app activity alerts.

Покрывает недостающие за раунды 1-5 требования:
- §3/§4: _is_meaningful_name, _validate_birthday, _profile_completion_status, needs_completion;
- §2.4: имя CSV-файла с реальным именем работника (RFC 5987 filename*);
- §5: forward news/photo comment, валидация цели, права удаления, activity alerts
  (релевантным, автор без self-alert, идемпотентность, deep-link, read сбрасывает).

Run:
    BOT_TOKEN='ci-dummy-token-not-a-real-secret' MINIAPP_DATA_ROOT=$(mktemp -d) \
      /home/promonta/agent/miniapp/.venv/bin/python3 -m pytest tests/test_round6_completion.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import main as backend  # noqa: E402
import core.permissions as permissions  # noqa: E402
from fastapi import HTTPException  # noqa: E402

OWNER = {'id': 1, 'first_name': 'Boss'}
W1 = {'id': 10, 'first_name': 'Ivan'}
W2 = {'id': 20, 'first_name': 'Petr'}
ROLES = {'1': 'owner', '10': 'worker', '20': 'worker'}


# ─────────────────────────── §4.1: определение отсутствующего имени ───────────────────────────
class MeaningfulNameTests(unittest.TestCase):
    def test_empty_and_whitespace_rejected(self):
        self.assertFalse(backend._is_meaningful_name('', '10'))
        self.assertFalse(backend._is_meaningful_name('   ', '10'))
        self.assertFalse(backend._is_meaningful_name(None, '10'))

    def test_digits_only_rejected(self):
        self.assertFalse(backend._is_meaningful_name('12345', '999'))

    def test_equals_user_id_rejected(self):
        self.assertFalse(backend._is_meaningful_name('10', '10'))

    def test_too_short_rejected(self):
        self.assertFalse(backend._is_meaningful_name('I', '10'))

    def test_real_name_accepted(self):
        self.assertTrue(backend._is_meaningful_name('Иван Петров', '10'))


# ─────────────────────────── §3.1: валидация даты рождения ───────────────────────────
class BirthdayValidationTests(unittest.TestCase):
    def test_valid_birthday_normalized(self):
        self.assertEqual(backend._validate_birthday('1990-05-15'), '1990-05-15')

    def test_empty_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend._validate_birthday('')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_bad_format_rejected(self):
        with self.assertRaises(HTTPException) as ctx:
            backend._validate_birthday('15.05.1990')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_future_rejected(self):
        future = (backend.business_today() + timedelta(days=1)).isoformat()
        with self.assertRaises(HTTPException) as ctx:
            backend._validate_birthday(future)
        self.assertEqual(ctx.exception.status_code, 400)


# ─────────────────────────── §3.2/§4/§6: статус завершённости профиля ───────────────────────────
class ProfileCompletionStatusTests(unittest.TestCase):
    def test_worker_missing_both(self):
        # Нет ни profile.name, ни осмысленного Telegram first_name (== user_id) → имя требуется.
        nameless = {'id': 10, 'first_name': '10'}
        st = backend._profile_completion_status(nameless, {}, 'worker')
        self.assertTrue(st['name_required'])
        self.assertTrue(st['birthday_required'])

    def test_worker_telegram_first_name_counts_as_name(self):
        # Telegram first_name — валидное имя (не сырой ID) → имя НЕ требуется, только дата.
        st = backend._profile_completion_status(W1, {}, 'worker')
        self.assertFalse(st['name_required'])
        self.assertTrue(st['birthday_required'])

    def test_worker_complete(self):
        st = backend._profile_completion_status(W1, {'name': 'Иван Петров', 'birthday': '1990-05-15'}, 'worker')
        self.assertFalse(st['name_required'])
        self.assertFalse(st['birthday_required'])

    def test_owner_birthday_not_required(self):
        st = backend._profile_completion_status(OWNER, {'name': 'Boss'}, 'owner')
        self.assertFalse(st['name_required'])
        self.assertFalse(st['birthday_required'])

    def test_get_my_profile_exposes_needs_completion(self):
        initial = {'10': {'quiz_completed': True}}
        with tempfile.TemporaryDirectory() as tmp:
            profiles_file = os.path.join(tmp, 'worker_profiles.json')
            with open(profiles_file, 'w', encoding='utf-8') as handle:
                json.dump(initial, handle)
            with patch.object(backend, 'WORKER_PROFILES_FILE', profiles_file), \
                 patch.object(backend, '_load_worker_profiles', return_value=initial), \
                 patch.object(backend, '_load_roles', return_value=ROLES):
                prof = backend.get_my_profile(user=W1)
        self.assertIn('needs_completion', prof)
        self.assertTrue(prof['needs_completion']['birthday_required'])


# ─────────────────────────── §3.3: приватность birthday ───────────────────────────
class BirthdayPrivacyTests(unittest.TestCase):
    def setUp(self):
        backend._save_worker_profiles({'10': {'name': 'Иван Петров', 'birthday': '1990-05-15'}})
        backend._save_checkin_meta([])
        backend._save_abwesenheit([])

    def test_owner_sees_birthday(self):
        with patch.object(backend, '_load_roles', return_value=ROLES):
            s = backend.profile_stats(user_id='10', user=OWNER, role='owner')
        self.assertEqual(s['birthday'], '1990-05-15')

    def test_birthday_feed_has_no_birth_year(self):
        # feed отдаёт год ОККУРЕНЦИИ (текущий/следующий), не год рождения → возраст не утекает.
        with patch.object(backend, '_load_roles', return_value=ROLES):
            backend._save_birthday_alerts([])
            res = backend.get_birthday_feed(user=W2)
        for b in res['birthdays']:
            self.assertNotIn('1990', str(b.get('year', '')))


# ─────────────────────────── §2.4: имя CSV с реальным именем работника ───────────────────────────
class CsvFilenameTests(unittest.TestCase):
    def setUp(self):
        backend._save_worker_profiles({'100': {'name': 'Иван Петров'}})
        backend._save_checkin_meta([
            {'user_id': '100', 'date': '2026-08-04', 'manual_entry': True,
             'start_time': '08:00', 'end_time': '16:00', 'pause_minutes': 0, 'object_id': 'OBJ-1'},
        ])

    def test_filename_uses_name_via_rfc5987(self):
        resp = backend.export_stundenzettel(
            user_id='100', date_from='2026-08-01', date_to='2026-08-31',
            user=OWNER, role='owner')
        cd = resp.headers['Content-Disposition']
        # RFC 5987 filename* percent-encoded содержит реальное имя (percent-байты кириллицы),
        # ASCII fallback не содержит сырого Telegram ID вместо имени.
        self.assertIn("filename*=UTF-8''", cd)
        self.assertIn('Stundenzettel', cd)
        self.assertIn('2026-08-01_2026-08-31', cd)


# ─────────────────────────── §5: пересылка комментариев + activity alerts ───────────────────────────
class CommentForwardAlertTests(unittest.TestCase):
    def setUp(self):
        backend._save_news_comments({})
        backend._save_activity_alerts([])
        backend._save_chat([])
        backend._save_photo_meta([
            {'id': 'PH1', 'user_id': 30, 'name': 'Sergei', 'object_id': 'OBJ-1',
             'files': ['x.jpg'], 'comments': []},
        ])
        self._patcher = patch.object(backend, '_load_roles', return_value=ROLES)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def _add_news(self, uid, text, user):
        return backend.add_news_comment('POST1', backend.NewsCommentBody(text=text), user=user)

    def test_alert_to_owner_and_prior_not_author(self):
        self._add_news(10, 'first', W1)   # W1 comments -> alert owner only
        self._add_news(20, 'second', W2)  # W2 -> alert owner + W1, not W2
        targets = {a['target_user_id'] for a in backend._load_activity_alerts()}
        self.assertEqual(targets, {'1', '10'})

    def test_author_no_self_alert(self):
        r = self._add_news(10, 'hi', W1)
        cid = r['comments'][-1]['id']
        self_alerts = [a for a in backend._load_activity_alerts()
                       if a['target_user_id'] == '10' and a['comment_id'] == cid]
        self.assertEqual(self_alerts, [])

    def test_alert_idempotent_across_get_alerts(self):
        self._add_news(10, 'hi', W1)
        n1 = len([x for x in backend.get_alerts(user=OWNER, role='owner')['alerts'] if x.get('activity_kind')])
        n2 = len([x for x in backend.get_alerts(user=OWNER, role='owner')['alerts'] if x.get('activity_kind')])
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 1)

    def test_alert_deep_link_fields(self):
        self._add_news(10, 'hi', W1)
        act = next(x for x in backend.get_alerts(user=OWNER, role='owner')['alerts'] if x.get('activity_kind'))
        self.assertEqual(act['activity_kind'], 'news_comment')
        self.assertEqual(act['activity_ref_id'], 'POST1')
        self.assertEqual(act['activity_deep_link'], 'news')

    def test_read_clears_alert(self):
        self._add_news(10, 'hi', W1)
        backend.mark_activity_alerts_read(backend.ActivityReadBody(kind='news_comment', ref_id='POST1'), user=OWNER)
        n = len([x for x in backend.get_alerts(user=OWNER, role='owner')['alerts'] if x.get('activity_kind')])
        self.assertEqual(n, 0)

    def test_photo_comment_alerts_owner_and_author(self):
        backend.add_feed_photo_comment('PH1', backend.PhotoCommentBody(text='nice'), user=W1)
        targets = {a['target_user_id'] for a in backend._load_activity_alerts()}
        self.assertIn('1', targets)    # owner
        self.assertIn('30', targets)   # автор фото
        self.assertNotIn('10', targets)  # автор комментария не получает

    def test_forward_news_builds_server_card(self):
        r = self._add_news(10, 'важный текст', W1)
        cid = r['comments'][-1]['id']
        res = backend.forward_comment(
            backend.CommentForwardBody(source_type='news', source_id='POST1', comment_id=cid),
            user=OWNER, role='owner')
        self.assertIn('Комментарий к новости', res['message']['text'])
        self.assertIn('важный текст', res['message']['text'])

    def test_forward_invalid_comment_404(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.forward_comment(
                backend.CommentForwardBody(source_type='news', source_id='POST1', comment_id='nope'),
                user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 404)

    def test_forward_unknown_source_type_400(self):
        with self.assertRaises(HTTPException) as ctx:
            backend.forward_comment(
                backend.CommentForwardBody(source_type='blog', source_id='X', comment_id='Y'),
                user=OWNER, role='owner')
        self.assertEqual(ctx.exception.status_code, 400)

    def test_other_worker_cannot_delete_comment(self):
        r = self._add_news(10, 'mine', W1)
        cid = r['comments'][-1]['id']
        with self.assertRaises(HTTPException) as ctx:
            backend.delete_news_comment('POST1', cid, user=W2, role='worker')
        self.assertEqual(ctx.exception.status_code, 403)

    def test_owner_can_delete_comment(self):
        r = self._add_news(10, 'mine', W1)
        cid = r['comments'][-1]['id']
        out = backend.delete_news_comment('POST1', cid, user=OWNER, role='owner')
        self.assertFalse(any(c['id'] == cid for c in out['comments']))


if __name__ == '__main__':
    unittest.main()
