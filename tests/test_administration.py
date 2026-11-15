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

    def test_pending_blocks_game_and_notifies_only_group(self):
        self.handle(self.message('/grow'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM players').fetchone()[0], 0)
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], -10)
        self.assertIn('верификация', self.api.call.call_args.kwargs['text'])
        self.handle(self.message('/roll'))
        self.assertTrue(all(c.kwargs.get('chat_id') == -10
                            for c in self.api.call.call_args_list))

    def test_join_announces_verification_without_private_messages(self):
        self.handle({'my_chat_member': {
            'chat': {'id': -10, 'type': 'group', 'title': 'Group'},
            'new_chat_member': {'status': 'member'}}})
        self.api.call.assert_called_once()
        self.assertEqual(self.api.call.call_args.args, ('sendMessage',))
        self.assertEqual(self.api.call.call_args.kwargs['chat_id'], -10)
        self.assertIn('верификация', self.api.call.call_args.kwargs['text'])

    def test_owner_approval_and_revocation(self):
        self.handle(self.message('/grow'))
        self.handle(self.message('/approve -10', 123, 123, 'private'))
        self.handle(self.message('/grow'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM players').fetchone()[0], 1)
        self.handle(self.message('/deny -10', 123, 123, 'private'))
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
                         'message': {'chat': {'id': 123, 'type': kind}}, 'data': 'approve:-10'}})
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

    def test_request_available_in_owner_panel_without_notification(self):
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
        self.assertTrue(all(c.kwargs.get('chat_id') == -10 for c in self.api.call.call_args_list))

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

    def test_malformed_command_and_unknown_chat(self):
        for text in ['/approve', '/deny abc', '/approve -999']:
            self.handle(self.message(text, 123, 123, 'private'))
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM chats').fetchone()[0], 0)
