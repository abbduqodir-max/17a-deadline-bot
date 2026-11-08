"""Offline self-tests. python3 -m unittest discover -s tests -v"""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bot import Store, Bot, UZ_TZ, parse_due, remaining, pending_reminders, display_due, notification_line


class FakeTelegram:
    def __init__(self):
        self.messages=[]
    def send(self,chat_id,text):
        self.messages.append((chat_id,text))
    def call(self,method,**kwargs):
        if method=='getChatMember':return {'status':'administrator' if kwargs['user_id']==77 else 'member'}
        raise AssertionError('Unexpected API: '+method)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(str(Path(self.tmp.name)/'test.sqlite3'))
        self.api=FakeTelegram()
        self.bot=Bot(self.api,self.store)
    def tearDown(self):
        self.store.conn.close();self.tmp.cleanup()
    def send_cmd(self,cid,text,kind='private',user=77):
        self.bot.handle_message({'chat':{'id':cid,'type':kind,'title':'UWED group'},
                                 'from':{'id':user},'text':text})

    def test_seed_dates_and_count(self):
        self.assertEqual(len(self.store.seed),32)
        self.assertEqual(len(self.store.undated),1)
        self.assertEqual(len(self.store.list_events(100,False)),14)
        self.assertEqual(display_due('2026-11-14 00:00'),'14.11.2026, 00:00')
        self.assertEqual(parse_due('2026-11-14 00:00').utcoffset(),timedelta(hours=5))

    def test_private_subscribe_by_level_and_unknown(self):
        self.send_cmd(100,'/start')
        self.assertTrue(self.store.enabled(100))
        self.assertEqual(len(self.store.list_events(100,False)),14)
        self.send_cmd(100,'/track eng_ui on')
        self.assertEqual(len(self.store.list_events(100,False)),32)
        self.send_cmd(100,'/unknown')
        self.assertIn('1.2',self.api.messages[-1][1])
        self.send_cmd(100,'/track eng_ui off')
        self.assertEqual(len(self.store.list_events(100,False)),14)

    def test_group_requires_admin_to_enable_or_add(self):
        self.send_cmd(-200,'/enable','supergroup',user=15)
        self.assertFalse(self.store.enabled(-200))
        self.assertIn('administrator',self.api.messages[-1][1])
        self.send_cmd(-200,'/enable','supergroup',user=77)
        self.assertTrue(self.store.enabled(-200))
        self.assertEqual(self.store.subscribed(-200),{'common'})
        self.send_cmd(-200,'/track eng_ui on','supergroup',user=15)
        self.assertNotIn('eng_ui',self.store.subscribed(-200))
        self.send_cmd(-200,'/track eng_ui on','supergroup',user=77)
        self.assertIn('eng_ui',self.store.subscribed(-200))

    def test_reminder_before_deadline_and_idempotent(self):
        self.store.register(-200,'supergroup','Class',enable=True)
        # Economics ON1 at Nov 15 23:00 => 24h reminder Nov 14 23:00.
        now=datetime(2026,11,14,23,0,tzinfo=UZ_TZ)
        pending=pending_reminders(self.store,-200,now)
        self.assertIn(24,pending)
        self.assertEqual([x['id'] for x in pending[24]],['U002'])
        self.assertEqual(self.bot.dispatch_due(now),1)
        self.assertEqual(self.bot.dispatch_due(now),0)
        text='\n'.join(x[1] for x in self.api.messages)
        self.assertIn('Iqtisodiyot nazariyasi',text)
        self.assertIn('24 SOAT',text)
        self.assertIn('15.11.2026, 23:00',text)

    def test_scope_custom_english_russian_and_hides(self):
        self.store.register(-200,'supergroup','Class',enable=True)
        self.store.register(-201,'supergroup','Other',enable=True)
        event_id=self.store.add_event(-200,'2026-12-04 23:59','Rus tili','Oraliq','6 padej','ru_a2','-')
        self.assertNotIn(event_id,{x['id'] for x in self.store.list_events(-200,False)})
        self.store.track_change(-200,'ru_a2',True)
        self.assertIn(event_id,{x['id'] for x in self.store.list_events(-200,False)})
        self.assertNotIn(event_id,{x['id'] for x in self.store.list_events(-201,False)})
        self.store.override(-200,'U002',hidden=True)
        self.assertNotIn('U002',{x['id'] for x in self.store.list_events(-200,False)})
        self.assertIn('U002',{x['id'] for x in self.store.list_events(-201,False)})
        self.store.override(-200,'U002',hidden=False)
        self.assertIn('U002',{x['id'] for x in self.store.list_events(-200,False)})

    def test_group_override_not_global_and_no_fake_topic(self):
        self.store.override(-200,'U002',deadline='2026-11-16 22:00',topic='Teacher verified topic')
        self.assertEqual(self.store.get_event(-200,'U002')['deadline'],'2026-11-16 22:00')
        self.assertEqual(self.store.get_event(-200,'U002')['topic'],'Teacher verified topic')
        self.assertIn('ko‘rsatilmagan',self.store.get_event(-201,'U002')['topic'])

    def test_sanitizes_user_content(self):
        e={'id':'C001','subject':'<b>bad</b>', 'kind':'<fake>', 'topic':'A & B',
           'prep':'read', 'deadline':'2026-12-04 23:59','track':'common', 'url':'-'}
        txt=notification_line(e,datetime(2026,12,3,tzinfo=UZ_TZ))
        self.assertNotIn('<fake>',txt)
        self.assertIn('&lt;fake&gt;',txt)
        self.assertIn('A &amp; B',txt)

    def test_add_group_and_update_for_only_one_group(self):
        self.send_cmd(-200,'/enable','supergroup')
        self.send_cmd(-200,'/add 2027-01-15 21:30 | Rus tili | JN | Cases | ru_a2 | https://moodle.uwed.uz/abc','supergroup')
        self.assertEqual(len(self.store.conn.execute('SELECT * FROM custom_events').fetchall()),1)
        e=self.store.conn.execute('SELECT * FROM custom_events').fetchone()
        self.send_cmd(-200,f'/setdate {e["id"]} 2027-01-16 23:59','supergroup')
        self.assertEqual(self.store.get_event(-200,e['id'])['deadline'],'2027-01-16 23:59')
        self.assertFalse(self.store.has_event(-201,e['id']))
        self.send_cmd(-200,f'/remove {e["id"]}','supergroup')
        self.assertFalse(self.store.has_event(-200,e['id']))


    def test_multiple_simultaneous_deadlines_one_group_message(self):
        self.store.register(-200,'supergroup','Class',enable=True)
        now=datetime(2026,11,29,19,0,tzinfo=UZ_TZ)
        pending=pending_reminders(self.store,-200,now)
        self.assertEqual({e['id'] for e in pending[24]}, {'U004','U005','U006','U007'})
        self.assertEqual(self.bot.dispatch_due(now),4)
        self.assertEqual(len(self.api.messages),1)
        msg=self.api.messages[0][1]
        self.assertIn('JN-1',msg)
        self.assertIn('2-oraliq nazorat',msg)

    def test_sent_reminder_survives_db_restart(self):
        self.store.register(-200,'supergroup','Class',enable=True)
        now=datetime(2026,11,14,23,0,tzinfo=UZ_TZ)
        self.assertEqual(self.bot.dispatch_due(now),1)
        p=self.store.conn.execute('PRAGMA database_list').fetchone()['file']
        self.store.conn.close()
        self.store=Store(p)
        self.bot=Bot(self.api,self.store)
        self.assertEqual(self.bot.dispatch_due(now),0)


if __name__=='__main__':unittest.main()
