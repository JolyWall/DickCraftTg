import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from bot import Administration, Game
from reliability import Delivery, TelegramError


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'game.db'
        self.game = Game(self.path)
        self.addCleanup(self.game.db.close)
        self.admin = Administration(self.game,123)
        self.now = 1000
        self.api = Mock()
        self.delivery = Delivery(self.game.db,self.api,lambda:self.now)

    def message(self, identifier, command='/grow', chat=-10, user=1):
        return {'update_id':identifier,'message':{'text':command,'chat':{'id':chat,'type':'supergroup','title':'Group'},
                                                'from':{'id':user,'first_name':'User'}}}

    def callback(self, data):
        return {'callback_query':{'id':'q','from':{'id':123},'message':{'chat':{'id':123,'type':'private'}},'data':data}}

    def test_flood_suppresses_repeated_pending_notice(self):
        for identifier in range(100):
            self.delivery.process(self.message(identifier),self.admin,'testbot')
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM delivery').fetchone()[0],2)
        self.assertEqual(self.delivery.offset,100)
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM chats').fetchone()[0],1)

    def test_duplicate_update_and_restart_do_not_play_twice(self):
        self.admin.register({'id':-10})
        self.admin.decide('approve',-10)
        update=self.message(10,'/roll')
        self.delivery.process(update,self.admin,'testbot')
        other=Game(self.path)
        self.addCleanup(other.db.close)
        restarted=Delivery(other.db,self.api,lambda:self.now)
        restarted.process(update,Administration(other,123),'testbot')
        self.assertEqual(other.db.execute('SELECT count(*) FROM delivery').fetchone()[0],1)
        self.assertEqual(restarted.offset,11)

    def test_enqueue_failure_rolls_back_game_and_offset(self):
        self.admin.register({'id':-10})
        self.admin.decide('approve',-10)
        self.game.db.execute("CREATE TRIGGER fail_delivery BEFORE INSERT ON delivery BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with self.assertRaises(Exception):
            self.delivery.process(self.message(1),self.admin,'testbot')
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM players').fetchone()[0],0)
        self.assertEqual(self.delivery.offset,0)

    def test_transient_failure_persists_then_delivers(self):
        self.delivery.call('sendMessage',chat_id=-10,text='saved')
        self.api.call.side_effect=OSError('offline')
        self.delivery.flush()
        self.assertEqual(self.game.db.execute('SELECT attempts,state FROM delivery').fetchone(),(1,'pending'))
        self.now+=3
        self.api.call.side_effect=None
        self.delivery.flush()
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM delivery').fetchone()[0],0)

    def test_429_obeys_global_retry_after(self):
        for chat in (-1,-2):
            self.delivery.call('sendMessage',chat_id=chat,text='hello')
        self.api.call.side_effect=TelegramError(429,30)
        self.delivery.flush()
        self.assertEqual(self.api.call.call_count,1)
        self.now+=20
        self.delivery.flush()
        self.assertEqual(self.api.call.call_count,1)
        self.now+=11
        self.api.call.side_effect=None
        self.delivery.flush()
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM delivery').fetchone()[0],0)

    def test_permanent_failure_does_not_block_other_chat(self):
        for chat in (-1,-2):
            self.delivery.call('sendMessage',chat_id=chat,text='hello')
        self.api.call.side_effect=[TelegramError(403),True]
        self.delivery.flush()
        self.assertEqual(self.game.db.execute('SELECT state,error FROM delivery').fetchall(),[('failed',403)])

    def test_denial_requires_confirmation_and_stale_button_fails(self):
        self.admin.register({'id':-10,'title':'Group'})
        self.admin.decide('approve',-10)
        version=self.game.db.execute('SELECT version FROM chats').fetchone()[0]
        self.admin.handle(self.callback(f'deny:-10:{version}'),self.api,'testbot')
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0],'approved')
        self.admin.handle(self.callback(f'confirm:-10:{version}'),self.api,'testbot')
        self.admin.handle(self.callback(f'deny:-10:{version}'),self.api,'testbot')
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0],'denied')
        self.admin.handle(self.callback(f'approve:-10:{version}'),self.api,'testbot')
        self.assertEqual(self.game.db.execute('SELECT status FROM chats').fetchone()[0],'denied')
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM decisions').fetchone()[0],2)

    def test_panel_pagination_and_lookup(self):
        for cid in range(-10,-22,-1):
            self.admin.register({'id':cid,'title':f'Group {cid}'})
        self.admin.panel(self.api,'all',1)
        text=self.api.call.call_args.kwargs['text']
        self.assertIn('страница 2',text)
        self.assertEqual(text.count(' | pending'),5)
        self.admin.detail(self.api,-10)
        self.assertIn('ID: -10',self.api.call.call_args.kwargs['text'])
        self.admin.detail(self.api,-999)
        self.assertEqual(self.api.call.call_args.kwargs['text'],'Чат не найден.')

    def test_chat_limit_applies_across_different_users(self):
        self.admin.register({'id':-10})
        self.admin.decide('approve',-10)
        for identifier in range(30):
            self.delivery.process(self.message(identifier,'/roll',user=identifier+1),self.admin,'testbot')
        self.assertEqual(self.game.db.execute('SELECT count(*) FROM delivery').fetchone()[0],20)

    def test_expired_callback_ack_does_not_block_processing(self):
        self.api.call.side_effect=TelegramError(400)
        self.assertIsNone(self.delivery.call('answerCallbackQuery',callback_query_id='old'))

    def test_user_and_chat_limits_expire(self):
        for i in range(3):
            self.assertTrue(self.delivery.allow('user',3,10))
        self.assertFalse(self.delivery.allow('user',3,10))
        self.now+=11
        self.assertTrue(self.delivery.allow('user',3,10))
        self.assertTrue(self.delivery.allow('different',1,10))
