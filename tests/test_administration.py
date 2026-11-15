import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot import Administration, Game


class AdministrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'game.db'
        self.game = Game(self.path)
        self.addCleanup(self.game.db.close)
        self.admin = Administration(self.game, 123)
        self.api = Mock()

    def message(self, text, user=1, chat=-10, kind='supergroup'):
        return {'message': {'text': text, 'chat': {'id': chat, 'type': kind, 'title': 'Test'},
                            'from': {'id': user, 'first_name': 'Test'}}}

    def handle(self, update):
        self.admin.handle(update, self.api, 'testbot')

    def test_pending_blocks_game_and_notifies_owner_once(self):
        self.handle(self.message('/grow'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM players').fetchone()[0], 0)
        self.assertEqual(self.api.call.call_count, 2)
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], -10)
        self.assertIn('верификация', self.api.call.call_args.kwargs['text'])
        self.handle(self.message('/roll'))
        notifications = [c for c in self.api.call.call_args_list if c.kwargs.get('chat_id') == 123]
        self.assertEqual(len(notifications), 1)
        self.assertIn('Новая заявка', notifications[0].kwargs['text'])
        self.assertIn('/approve -10', notifications[0].kwargs['text'])

    def test_join_announces_verification_and_notifies_bot_owner_only(self):
        self.handle({'my_chat_member': {
            'chat': {'id': -10, 'type': 'group', 'title': 'Group'},
            'from': {'id': 456},
            'new_chat_member': {'status': 'member'}}})
        self.assertEqual(self.api.call.call_count, 2)
        self.assertEqual([c.kwargs['chat_id'] for c in self.api.call.call_args_list], [123, -10])
        self.assertIn('верификация', self.api.call.call_args.kwargs['text'])

    def test_owner_approval_and_revocation(self):
        self.handle(self.message('/grow'))
        self.handle(self.message('/approve -10', 123, 123, 'private'))
        self.handle(self.message('/grow'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM players').fetchone()[0], 1)
        self.handle(self.message('/deny -10', 123, 123, 'private'))
        version = self.game.db.execute('SELECT version FROM chats WHERE id=-10').fetchone()[0]
        self.handle({'callback_query': {'id':'confirm', 'from':{'id':123},
                     'message':{'chat':{'id':123,'type':'private'}}, 'data':f'deny:-10:{version}'}})
        self.api.reset_mock()
        self.handle(self.message('/roll'))
        self.assertIn('запретил', self.api.call.call_args.kwargs['text'])

    def test_other_users_and_group_owner_cannot_administer(self):
        self.handle(self.message('/grow'))
        for user, chat, kind in [(1, 1, 'private'), (123, -10, 'supergroup')]:
            self.handle(self.message('/approve -10', user, chat, kind))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'pending')

    def test_callback_authorization(self):
        self.handle(self.message('/grow'))
        for user, kind in [(1, 'private'), (123, 'supergroup'), (123, 'private')]:
            self.handle({'callback_query': {'id': 'q', 'from': {'id': user},
                         'message': {'chat': {'id': 123, 'type': kind}}, 'data': 'approve:-10:1'}})
            expected = 'approved' if (user, kind) == (123, 'private') else 'pending'
            self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], expected)

    def test_join_and_rejoin_keep_denial(self):
        update = {'my_chat_member': {'chat': {'id': -10, 'type': 'group', 'title': 'Group'},
                                    'new_chat_member': {'status': 'member'}}}
        self.handle(update)
        self.admin.decide('deny', -10)
        self.handle(update)
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'denied')

    def test_decisions_survive_restart(self):
        self.handle(self.message('/grow'))
        self.admin.decide('approve', -10)
        other = Game(self.path)
        self.addCleanup(other.db.close)
        Administration(other, 123)
        self.assertEqual(other.db.execute('SELECT status FROM chats').fetchone()[0], 'approved')

    def test_request_available_in_owner_panel(self):
        self.handle(self.message('/grow'))
        self.api.reset_mock()
        self.handle(self.message('/pending', 123, 123, 'private'))
        self.assertIn('-10', self.api.call.call_args.kwargs['text'])

    def test_admin_can_reapply_once_after_denial(self):
        self.handle(self.message('/grow'))
        self.admin.decide('deny', -10)
        self.api.call.return_value = {'status': 'administrator'}
        self.handle(self.message('/verify@testbot'))
        self.assertIn('подана', self.api.call.call_args.kwargs['text'])
        self.handle(self.message('/verify'))
        self.assertIn('Повторная заявка не создана', self.api.call.call_args.kwargs['text'])
        self.assertEqual(self.game.db.execute(
            "SELECT count(*) FROM chats WHERE id=-10 AND status='pending'").fetchone()[0], 1)
        notices = [c for c in self.api.call.call_args_list if c.kwargs.get('chat_id') == 123]
        self.assertEqual(len(notices), 2)
        self.assertIn('Повторная заявка', notices[-1].kwargs['text'])

    def test_creator_can_reapply_but_members_cannot(self):
        self.handle(self.message('/grow'))
        self.admin.decide('deny', -10)
        self.api.call.return_value = {'status': 'member'}
        self.handle(self.message('/verify'))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'denied')
        self.api.call.return_value = {'status': 'creator'}
        self.handle(self.message('/verify'))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'pending')

    def test_verify_does_not_reopen_approved_chat(self):
        self.handle(self.message('/grow'))
        self.admin.decide('approve', -10)
        self.api.call.return_value = {'status': 'administrator'}
        self.handle(self.message('/verify'))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'approved')
        self.assertIn('уже верифицирована', self.api.call.call_args.kwargs['text'])

    def test_failed_membership_check_does_not_reopen_request(self):
        self.handle(self.message('/grow'))
        self.admin.decide('deny', -10)
        def call(method, **params):
            if method == 'getChatMember':
                raise RuntimeError('Unavailable')
        self.api.call.side_effect = call
        self.handle(self.message('/verify'))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'denied')
        self.assertIn('Не удалось проверить', self.api.call.call_args.kwargs['text'])

    def test_anonymous_admin_cannot_reapply(self):
        self.handle(self.message('/grow'))
        self.admin.decide('deny', -10)
        update = self.message('/verify')
        update['message']['sender_chat'] = {'id': -10}
        self.api.reset_mock()
        self.handle(update)
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'denied')
        self.assertEqual(self.api.call.call_count, 1)

    def test_upgrade_preserves_existing_player_data(self):
        path = Path(self.temp.name) / 'legacy.db'
        with sqlite3.connect(path) as legacy:
            legacy.execute('CREATE TABLE players (chat INTEGER, user INTEGER, name TEXT NOT NULL, '
                           'length INTEGER NOT NULL DEFAULT 0, day TEXT, PRIMARY KEY(chat,user))')
            legacy.executemany('INSERT INTO players VALUES(?,?,?,?,?)', [
                (-10, 1, 'Первый игрок', 42, '2026-11-15'),
                (-10, 2, 'Другой игрок', -7, '2026-11-14'),
                (-20, 1, 'Первый игрок', 100, '2026-11-13')])
            expected = legacy.execute('SELECT * FROM players ORDER BY chat,user').fetchall()
        upgraded = Game(path)
        self.addCleanup(upgraded.db.close)
        admin = Administration(upgraded, 123)
        admin.register({'id': -10, 'title': 'Existing group'})
        admin.decide('approve', -10)
        self.assertEqual(upgraded.db.execute('SELECT * FROM players ORDER BY chat,user').fetchall(), expected)
        self.assertEqual(upgraded.db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_failed_owner_notification_preserves_request_and_group_reply(self):
        def call(method, **params):
            if params.get('chat_id') == 123:
                raise RuntimeError('Forbidden')
        self.api.call.side_effect = call
        with self.assertLogs('bot', level='WARNING'):
            self.handle(self.message('/grow'))
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0], 'pending')
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], -10)
        self.api.call.side_effect = None
        self.handle(self.message('/pending', 123, 123, 'private'))
        self.assertIn('-10', self.api.call.call_args.kwargs['text'])

    def test_pending_request_does_not_notify_again_after_restart(self):
        self.handle(self.message('/grow'))
        other = Game(self.path)
        self.addCleanup(other.db.close)
        admin = Administration(other, 123)
        self.api.reset_mock()
        admin.handle(self.message('/grow'), self.api, 'testbot')
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], -10)

    def test_verify_creates_new_request_and_notifies_owner(self):
        self.api.call.return_value = {'status': 'administrator'}
        self.handle(self.message('/verify'))
        notices = [c for c in self.api.call.call_args_list if c.kwargs.get('chat_id') == 123]
        self.assertEqual(len(notices), 1)
        self.assertIn('Новая заявка', notices[0].kwargs['text'])

    def test_malformed_command_and_unknown_chat(self):
        for text in ['/approve', '/deny abc', '/approve -999']:
            self.handle(self.message(text, 123, 123, 'private'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM chats').fetchone()[0], 0)
