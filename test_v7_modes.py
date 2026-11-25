"""v7 專屬的模式、順位與多帳號行為測試。"""
import unittest
from dataclasses import replace

from monitor_core import (BoxInfo, NewBoxTracker, MODE_NEW_ID,
                          MODE_AVAILABLE, MODE_ANY_NEW)
from kuji_gui import SiteConfig, validate_site, AccountWorker
from account_store import Account
import queue


def b(index, key, ready=True):
    return BoxInfo(index, key, '盒子', '可點：このくじ箱を見る' if ready else '已售完')


class V7ModeTests(unittest.TestCase):
    def test_mode_validated(self):
        src = {'url':'https://example.org/boxes', 'monitor_mode':'any_new_id',
               'box_css':'.box', 'button_css':'.draw-btn',
               'username_css':'input[name=username]', 'password_css':'input[type=password]',
               'submit_css':'button[type=submit]'}
        self.assertEqual(validate_site(src).monitor_mode, MODE_ANY_NEW)
        with self.assertRaisesRegex(ValueError, '未知監控模式'):
            validate_site({**src, 'monitor_mode':'inject'})

    def test_new_id_mode_only_selected_rank(self):
        t = NewBoxTracker()
        t.observe([b(1,'A'),b(2,'B')],1,MODE_NEW_ID)
        x = t.observe([b(1,'A'),b(2,'C'),b(3,'B')],1,MODE_NEW_ID)
        self.assertEqual(x.new_identities,['C'])
        self.assertFalse(x.should_click)

    def test_recovery_on_existing_card(self):
        t = NewBoxTracker()
        self.assertTrue(t.observe([b(1,'A',False)],1,MODE_AVAILABLE).baseline_created)
        self.assertFalse(t.observe([b(1,'A',False)],1,MODE_AVAILABLE).should_click)
        x = t.observe([b(1,'A',True)],1,MODE_AVAILABLE)
        self.assertEqual(x.became_actionable,['A'])
        self.assertTrue(x.should_click)
        self.assertEqual(x.trigger,'became_available')
        self.assertEqual(x.target_index,1)
        t.clicked('A',True)
        self.assertFalse(t.observe([b(1,'A',True)],1,MODE_AVAILABLE).should_click)

    def test_only_actionable_transition_counts(self):
        t = NewBoxTracker()
        t.observe([b(1,'A',True)],1,MODE_AVAILABLE)
        self.assertFalse(t.observe([b(1,'A',True)],1,MODE_AVAILABLE).should_click)
        t.observe([b(1,'A',False)],1,MODE_AVAILABLE)
        self.assertTrue(t.observe([b(1,'A',True)],1,MODE_AVAILABLE).should_click)

    def test_recovery_mode_still_new_id(self):
        t = NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_AVAILABLE)
        x=t.observe([b(1,'B')],1,MODE_AVAILABLE)
        self.assertEqual(x.trigger,'new_id')
        self.assertTrue(x.should_click)

    def test_new_unavailable_waits_then_recovers(self):
        t = NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_AVAILABLE)
        self.assertFalse(t.observe([b(1,'N',False)],1,MODE_AVAILABLE).should_click)
        self.assertEqual(t.pending['N'],0)
        x=t.observe([b(1,'N',True)],1,MODE_AVAILABLE)
        self.assertTrue(x.should_click)
        self.assertEqual(x.retry_number,1)

    def test_any_new_id_on_rank_3(self):
        t=NewBoxTracker()
        t.observe([b(1,'A'),b(2,'B')],1,MODE_ANY_NEW)
        x=t.observe([b(1,'A'),b(2,'B'),b(3,'NEW')],1,MODE_ANY_NEW)
        self.assertTrue(x.should_click)
        self.assertEqual(x.target_index,3)
        self.assertEqual(x.selected_identity,'NEW')

    def test_any_new_tracks_unavailable_until_ready(self):
        t=NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_ANY_NEW)
        self.assertFalse(t.observe([b(1,'A'),b(2,'NEW',False)],1,MODE_ANY_NEW).should_click)
        self.assertTrue(t.observe([b(1,'NEW',True),b(2,'A')],1,MODE_ANY_NEW).should_click)

    def test_any_new_multiple_new_picks_first_clickable(self):
        t=NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_ANY_NEW)
        x=t.observe([b(1,'X',False),b(2,'Y',True),b(3,'A')],1,MODE_ANY_NEW)
        self.assertEqual(x.selected_identity,'Y')
        t.clicked('Y',True)
        x=t.observe([b(1,'X',True),b(2,'Y',True),b(3,'A')],1,MODE_ANY_NEW)
        self.assertEqual(x.selected_identity,'X')

    def test_any_new_prevents_duplicate(self):
        t=NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_ANY_NEW)
        x=t.observe([b(1,'N'),b(2,'N'),b(3,'A')],1,MODE_ANY_NEW)
        self.assertFalse(x.should_click)
        self.assertTrue(x.duplicate_selected_id)

    def test_seen_existing_id_never_new_again(self):
        t=NewBoxTracker()
        t.observe([b(1,'A')],1,MODE_ANY_NEW)
        t.observe([b(1,'B')],1,MODE_ANY_NEW)
        t.clicked('B',True)
        t.observe([b(1,'A')],1,MODE_ANY_NEW)
        x=t.observe([b(1,'B')],1,MODE_ANY_NEW)
        self.assertFalse(x.should_click)

    def test_per_account_workers_independent(self):
        events=queue.Queue()
        a,bacc=Account.new('one'),Account.new('two')
        a.target_rank=1; bacc.target_rank=3
        w1,w2=AccountWorker(a,events),AccountWorker(bacc,events)
        self.assertIsNot(w1.tracker,w2.tracker)
        c=SiteConfig(url='https://example.org/boxes',monitor_mode=MODE_ANY_NEW)
        self.assertEqual(replace(c,rank=a.target_rank).rank,1)
        self.assertEqual(replace(c,rank=bacc.target_rank).rank,3)


if __name__ == '__main__':
    unittest.main()
