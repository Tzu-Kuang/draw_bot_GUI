"""本機 mock：商品詳情購入入口與選盒監控銜接；不操作真實網站。"""
import queue
import unittest
from unittest.mock import patch

import kuji_gui
from account_store import Account
from auth_flow import classify_auth_page
from product_flow import is_product_detail, is_box_selection, is_product_purchase_label
from kuji_gui import AccountWorker, SiteConfig

BOX_URL = 'https://on-line.1kuji.com/Form/Product/SelectBox.aspx?pid=sap_0000008292_s1&pno=1'
PRODUCT_URL = 'https://on-line.1kuji.com/Form/Product/ProductDetail.aspx?shop=0&pid=sap_0000008292_s1&bid=IP00002619'
BOX_WITH_PNO = 'https://on-line.1kuji.com/Form/Product/SelectBox.aspx?pid=sap_0000008292_s1&pno=2'

class FakeBy:
    CSS_SELECTOR = 'css'
    XPATH = 'xpath'

class Cta:
    def __init__(self, driver, label='購入する', enabled=True):
        self.driver = driver
        self.label = label
        self.enabled = enabled
        self.clicks = 0

    def is_displayed(self):
        return True

    def is_enabled(self):
        return self.enabled

    @property
    def text(self):
        return self.label

    def get_attribute(self, name):
        return ''

    def click(self):
        self.clicks += 1
        self.driver.current_url = BOX_WITH_PNO

class Box:
    def is_displayed(self):
        return True

class FakeDriver:
    def __init__(self, url=PRODUCT_URL, label='購入する'):
        self.current_url = url
        self.cta = Cta(self, label)
        self.window_handles = ['main']
        self.switch_to = type('Switch', (), {'window': lambda s, _handle: None})()

    def execute_script(self, script):
        if self.current_url.startswith('https://on-line.1kuji.com/Form/Product/SelectBox.aspx'):
            return [{'id': 'BOX-TEST1', 'button': None, 'preview': 'BOX-TEST1 販売終了'}] if self.find_elements('css', '.box-card') else []
        return []

    def find_elements(self, by, query):
        if query == 'a, button, input[type="button"], input[type="submit"], [role="button"]' and self.current_url == PRODUCT_URL:
            return [self.cta]
        if query == '.box-card' and self.current_url.startswith('https://on-line.1kuji.com/Form/Product/SelectBox.aspx'):
            return [Box()]
        return []

class ProductEntryTests(unittest.TestCase):
    def setup_worker(self, current=PRODUCT_URL, label='購入する'):
        worker = AccountWorker(Account.new('test'), queue.Queue())
        driver = FakeDriver(current, label)
        worker.driver = driver
        worker._open = lambda cfg, navigate: None
        worker._start_count = 0
        worker._start = lambda cfg: setattr(worker, '_start_count', worker._start_count + 1)
        return worker, driver

    def test_route_matching_same_pid_only(self):
        self.assertTrue(is_product_detail(PRODUCT_URL, BOX_URL))
        self.assertTrue(is_box_selection(BOX_WITH_PNO, BOX_URL))
        self.assertFalse(is_product_detail(PRODUCT_URL.replace('8292', '8293'), BOX_URL))
        self.assertFalse(is_product_detail(PRODUCT_URL.replace('on-line.1kuji.com', 'evil.example'), BOX_URL))
        self.assertFalse(is_product_detail(PRODUCT_URL.replace('https:', 'http:'), BOX_URL))
        self.assertEqual(classify_auth_page(PRODUCT_URL, BOX_URL), 'product_detail')
        self.assertEqual(classify_auth_page(BOX_WITH_PNO, BOX_URL), 'target')
        self.assertEqual(classify_auth_page(BOX_URL, BOX_URL), 'target')

    def test_purchase_label_strict(self):
        self.assertTrue(is_product_purchase_label(' 購入する\n'))
        for value in ['購入を確定する', '支払う', '購入手続きを完了する', '購入する（予約）', 'このくじ箱にする']:
            self.assertFalse(is_product_purchase_label(value))

    def test_click_once_after_login_then_start(self):
        worker, driver = self.setup_worker()
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            worker._begin_login(SiteConfig(url=BOX_URL), '', auto_start=True)
            self.assertEqual(driver.cta.clicks, 1)
            self.assertEqual(driver.current_url, BOX_WITH_PNO)
            self.assertEqual(worker._start_count, 0)
            worker._advance_login()
            self.assertEqual(worker._start_count, 1)
            self.assertFalse(worker.login_waiting)
            worker._advance_login()
            self.assertEqual(driver.cta.clicks, 1)

    def test_never_click_on_checkout_page(self):
        checkout = 'https://on-line.1kuji.com/Form/Order/Confirm.aspx?pid=sap_0000008292_s1'
        worker, driver = self.setup_worker(checkout)
        worker.cfg = SiteConfig(url=BOX_URL)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            self.assertFalse(worker._click_product_purchase())
        self.assertEqual(driver.cta.clicks, 0)

    def test_never_click_unexpected_label(self):
        worker, driver = self.setup_worker(label='注文を確定する')
        worker.cfg = SiteConfig(url=BOX_URL)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            self.assertFalse(worker._click_product_purchase())
        self.assertEqual(driver.cta.clicks, 0)

    def test_purchase_entry_can_be_disabled(self):
        worker, driver = self.setup_worker()
        worker.cfg = SiteConfig(url=BOX_URL, enter_purchase=False)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            self.assertFalse(worker._click_product_purchase())
        self.assertEqual(driver.cta.clicks, 0)

    def test_mismatched_product_never_clicked(self):
        worker, driver = self.setup_worker()
        worker.cfg = SiteConfig(url=BOX_URL.replace('8292', '8293'))
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            self.assertFalse(worker._click_product_purchase())
        self.assertEqual(driver.cta.clicks, 0)

    def test_two_accounts_separate_purchase_state(self):
        first, a = self.setup_worker()
        second, b = self.setup_worker()
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            first._begin_login(SiteConfig(url=BOX_URL), '', True)
            second._begin_login(SiteConfig(url=BOX_URL), '', True)
        self.assertEqual(a.cta.clicks, 1)
        self.assertEqual(b.cta.clicks, 1)
        self.assertIsNot(first.driver, second.driver)

    def test_does_not_enter_box_monitor_without_visible_box_css(self):
        worker, driver = self.setup_worker()
        driver.find_elements = lambda by, query: [driver.cta] if 'input[type="button"]' in query and driver.current_url == PRODUCT_URL else []
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            worker._begin_login(SiteConfig(url=BOX_URL), '', True)
            worker._advance_login()
        self.assertEqual(worker._start_count, 0)
        self.assertTrue(worker.login_waiting)

if __name__ == '__main__':
    unittest.main()
