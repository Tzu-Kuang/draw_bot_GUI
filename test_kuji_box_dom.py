import queue
import unittest
from dataclasses import replace

from account_store import Account
from kuji_box_dom import ACTION_CSS, READ_BOXES_JS, actionable_kuji_button, read_kuji_rows
from kuji_gui import AccountWorker, SiteConfig

OFFICIAL_URL = 'https://on-line.1kuji.com/Form/Product/SelectBox.aspx?pid=sap_test&pno=1'


class FakeLink:
    def __init__(self, label='このくじ箱を見る', element_id='ctl00_ContentPlaceHolder1_rBoxList_ctl05_lbQueue',
                 classes='openModalButton c-button--s', enabled=True, visible=True, pointer='auto',
                 disabled=False, role=''):
        self.tag_name = 'a'
        self.text = label
        self.element_id = element_id
        self.classes = classes
        self.enabled = enabled
        self.visible = visible
        self.pointer = pointer
        self.disabled = disabled
        self.count = 0
        self.role = role

    def get_attribute(self, key):
        return {'id': self.element_id, 'class': self.classes, 'aria-disabled': 'true' if self.disabled else None,
                'aria-label': '', 'disabled': None, 'role': self.role}.get(key)

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return self.enabled

    def value_of_css_property(self, key):
        return self.pointer

    def click(self):
        self.count += 1


class FakeDriver:
    def __init__(self, rows):
        self.current_url = OFFICIAL_URL
        self.rows = rows
        self.invocations = 0

    def execute_script(self, script):
        self.invocations += 1
        self.last_script = script
        return self.rows


def item(bid, button=None, status='残20枚'):
    return {'id': bid, 'button': button, 'preview': f'{bid} {status}'}


class KujiBoxTests(unittest.TestCase):
    def test_official_action_is_anchor_not_any_role_link(self):
        self.assertTrue(actionable_kuji_button(FakeLink()))
        self.assertFalse(actionable_kuji_button(FakeLink(element_id='not-box', classes='', role='link')))

    def test_various_link_colors_are_accepted(self):
        for cls in ('openModalButton c-button--purple', 'openModalButton c-button--orange',
                    'openModalButton c-button--yellow'):
            with self.subTest(cls=cls):
                self.assertTrue(actionable_kuji_button(FakeLink(classes=cls)))

    def test_disabled_and_sold_out_not_clicked(self):
        self.assertFalse(actionable_kuji_button(FakeLink(label='販売終了')))
        self.assertFalse(actionable_kuji_button(FakeLink(disabled=True)))
        self.assertFalse(actionable_kuji_button(FakeLink(pointer='none')))
        self.assertFalse(actionable_kuji_button(FakeLink(enabled=False)))
        self.assertFalse(actionable_kuji_button(FakeLink(visible=False)))

    def test_does_not_click_unrelated_actions(self):
        self.assertFalse(actionable_kuji_button(FakeLink(label='購入を確定する')))
        self.assertFalse(actionable_kuji_button(FakeLink(label='このくじ箱を見る 他の操作')))
        self.assertFalse(actionable_kuji_button(None))

    def test_includes_soldout_in_rank(self):
        d = FakeDriver([item('BOX-C07C3', FakeLink()), item('BOX-74F26', None, '販売終了'),
                        item('BOX-A5326', FakeLink())])
        rows = read_kuji_rows(d)
        self.assertEqual([r.info.index for r in rows], [1, 2, 3])
        self.assertEqual([r.info.identity for r in rows], ['BOX-C07C3', 'BOX-74F26', 'BOX-A5326'])
        self.assertIsNone(rows[1].button)
        self.assertEqual(rows[1].info.button_status, '已售完')

    def test_script_uses_visible_dom_cards(self):
        self.assertIn('createTreeWalker', READ_BOXES_JS)
        self.assertIn('販売終了', READ_BOXES_JS)
        self.assertIn('rBoxList_', ACTION_CSS)

    def test_first_scan_creates_baseline_then_clicks_new_queue_button(self):
        first = FakeLink(classes='openModalButton c-button--purple')
        fresh = FakeLink(classes='openModalButton c-button--yellow')
        d = FakeDriver([item('BOX-AAAA', first), item('BOX-BBBB', None, '販売終了')])
        ev = queue.Queue()
        worker = AccountWorker(Account.new('unit-test'), ev)
        worker.driver = d
        worker.cfg = SiteConfig(url=OFFICIAL_URL, rank=1)
        worker._scan(worker.cfg, do_track=True)
        self.assertEqual(first.count, 0)
        d.rows = [item('BOX-CCCC', fresh), item('BOX-BBBB', None, '販売終了')]
        worker._scan(worker.cfg, do_track=True)
        self.assertEqual(fresh.count, 1)
        self.assertEqual(first.count, 0)
        self.assertEqual(worker.monitoring, False)

    def test_new_soldout_target_is_never_clicked(self):
        d = FakeDriver([item('BOX-AAAA', FakeLink()), item('BOX-OLD2', FakeLink())])
        ev = queue.Queue()
        worker = AccountWorker(Account.new('unit-test'), ev)
        worker.driver = d
        worker.cfg = SiteConfig(url=OFFICIAL_URL, rank=2)
        worker._scan(worker.cfg, do_track=True)
        d.rows = [item('BOX-AAAA', FakeLink()), item('BOX-NEW2', None, '販売終了')]
        worker._scan(worker.cfg, do_track=True)
        self.assertFalse(any(t[0] == 'clicked' for t in list(ev.queue)))


if __name__ == '__main__':
    unittest.main()


class V7WorkerRoutingTests(unittest.TestCase):
    def _worker(self, rows, **overrides):
        d = FakeDriver(rows)
        worker = AccountWorker(Account.new('v7-unit'), queue.Queue())
        worker.driver = d
        worker.cfg = replace(SiteConfig(url=OFFICIAL_URL), **overrides)
        return worker, d

    def test_clicks_new_id_at_third_rank_in_any_mode(self):
        target = FakeLink(classes='openModalButton c-button--purple')
        worker, d = self._worker([item('BOX-A', FakeLink()), item('BOX-B', FakeLink())],
                                 monitor_mode='any_new_id', rank=1)
        worker._scan(worker.cfg, True)
        d.rows = [item('BOX-A', FakeLink()), item('BOX-B', FakeLink()), item('BOX-C', target)]
        worker._scan(worker.cfg, True)
        self.assertEqual(target.count, 1)
        self.assertTrue(any('第 3 盒' in x[2] and x[0]=='log' for x in list(worker.events.queue)))

    def test_recovered_existing_box_gets_clicked(self):
        target = FakeLink(classes='openModalButton c-button--purple')
        worker, d = self._worker([item('BOX-A', None, '販売終了')], monitor_mode='available_again')
        worker._scan(worker.cfg, True)
        d.rows = [item('BOX-A', target)]
        worker._scan(worker.cfg, True)
        self.assertEqual(target.count, 1)
        self.assertTrue(any('恢復可點' in x[2] for x in list(worker.events.queue) if x[0]=='log'))

    def test_any_new_does_not_click_existing_available(self):
        target = FakeLink()
        worker, d = self._worker([item('BOX-A', target)], monitor_mode='any_new_id')
        worker._scan(worker.cfg, True)
        worker._scan(worker.cfg, True)
        self.assertEqual(target.count, 0)
