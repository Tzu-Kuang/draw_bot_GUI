"""SSO page classification + offline fake browser tests for login handoff.
Does not contact the real Bandai website or bypass OTP/CAPTCHA.
"""
from __future__ import annotations
import queue
import types
import unittest
from unittest.mock import patch

import kuji_gui
from account_store import Account
from auth_flow import (allowed_credential_host, classify_auth_page, is_kuji_site,
                       is_login_button_text, suspicious_password_css)
from kuji_gui import AccountWorker, SiteConfig

TARGET = 'https://on-line.1kuji.com/Form/Product/SelectBox.aspx?pid=abc'
LOGIN = 'https://on-line.1kuji.com/Form/Login.aspx?nurl=some-target'
SSO = 'https://account.bandainamcoid.com/login.html?client_id=test'


class AuthFlowPureTests(unittest.TestCase):
    def test_kuji_login_versus_target(self):
        self.assertEqual(classify_auth_page(LOGIN, TARGET), 'kuji_login')
        self.assertEqual(classify_auth_page(TARGET, TARGET), 'target')
        self.assertEqual(classify_auth_page(SSO, TARGET), 'bnid_login')
        self.assertEqual(classify_auth_page('https://on-line.1kuji.com/Access.html', TARGET), 'blocked')
        self.assertTrue(is_kuji_site(TARGET))

    def test_exact_sso_host_only(self):
        self.assertTrue(allowed_credential_host(SSO, TARGET))
        self.assertFalse(allowed_credential_host(LOGIN, TARGET))
        self.assertFalse(allowed_credential_host('https://account.bandainamcoid.com.evil.test/login', TARGET))
        self.assertFalse(allowed_credential_host('http://account.bandainamcoid.com/login', TARGET))
        self.assertFalse(allowed_credential_host('https://www.bandainamcoid.com/v2/oauth2/auth', TARGET))

    def test_local_demo_login_form_host(self):
        self.assertTrue(allowed_credential_host('http://127.0.0.1:8765/login',
                                                'http://127.0.0.1:8765/',
                                                'http://127.0.0.1:8765/login'))
        self.assertFalse(allowed_credential_host('http://127.0.0.1:9999/login',
                                                'http://127.0.0.1:8765/',
                                                'http://127.0.0.1:8765/login'))


class FakeElement:
    def __init__(self, driver, role):
        self.driver = driver
        self.role = role
        self.entered = ''

    def is_displayed(self):
        return True

    def is_enabled(self):
        if self.role in ('submit', 'standalone_login'):
            # Official page initially disables LOGIN until fields are filled.
            return bool(self.driver.values.get('username') and self.driver.values.get('password'))
        return True

    @property
    def text(self):
        return '登入' if self.role in ('submit', 'standalone_login') else ''

    def get_attribute(self, name):
        if name == 'type':
            return 'submit' if self.role == 'submit' else 'button'
        return None

    def clear(self):
        self.entered = ''

    def send_keys(self, value):
        self.entered += value
        self.driver.values[self.role] = self.entered

    def click(self):
        if self.role == 'entry':
            self.driver.current_url = SSO
        elif self.role in ('submit', 'standalone_login'):
            self.driver.current_url = TARGET

    def find_element(self, kind, query):
        if self.role == 'password' and 'ancestor::form' in query and not self.driver.no_form:
            return FakeElement(self.driver, 'form')
        raise kuji_gui.WebDriverException('No element')

    def find_elements(self, kind, query):
        return [FakeElement(self.driver, 'submit')] if self.role == 'form' else []


class FakeDriver:
    def __init__(self, url=LOGIN, with_boxes=True, no_form=False):
        self.current_url = url
        self.with_boxes = with_boxes
        self.no_form = no_form
        self.values = {}
        self.refreshes = 0
        self.window_handles = ['main']
        self.switch_to = types.SimpleNamespace(window=lambda handle: None)

    def find_elements(self, by, query):
        if 'バンダイナムコIDでログイン' in query and self.current_url == LOGIN:
            return [FakeElement(self, 'entry')]
        if self.current_url.startswith('https://account.bandainamcoid.com'):
            if query.startswith('input[type="email"]'):
                return [FakeElement(self, 'username')]
            if query == 'input[type="password"]':
                return [FakeElement(self, 'password')]
            if query == 'button, input[type="submit"], [role="button"]' and self.no_form:
                return [FakeElement(self, 'standalone_login'), FakeElement(self, 'passkey')]
        if query == '.box-card' and self.current_url == TARGET and self.with_boxes:
            return [FakeElement(self, 'box')]
        return []

    def execute_script(self, script):
        if self.current_url == TARGET and self.with_boxes:
            return [{'id': 'BOX-TEST1', 'button': None, 'preview': 'BOX-TEST1 販売終了'}]
        return []

    def refresh(self):
        self.refreshes += 1


class FakeBy:
    XPATH = 'xpath'
    CSS_SELECTOR = 'css'


class BandaiWorkerTests(unittest.TestCase):
    def setup_worker(self, initial=LOGIN, boxes=True):
        driver = FakeDriver(initial, with_boxes=boxes)
        account = Account.new('test', 'demo@sample.jp')
        worker = AccountWorker(account, queue.Queue())
        worker.driver = driver
        worker.open_url = TARGET
        worker._open = lambda cfg, navigate: None  # avoid launching Chrome in offline unit tests
        worker._start_count = 0
        worker._start = lambda cfg: setattr(worker, '_start_count', worker._start_count + 1)
        return worker, driver

    def test_clicks_official_entry_then_waits_for_manual_otp(self):
        worker, driver = self.setup_worker()
        cfg = SiteConfig(url=TARGET)
        with patch.object(kuji_gui, 'By', FakeBy, create=True), patch.object(kuji_gui, 'SELENIUM_AVAILABLE', True):
            worker._begin_login(cfg, '', True)
            self.assertEqual(driver.current_url, SSO)
            self.assertTrue(worker.login_waiting)
            self.assertEqual(worker._start_count, 0)
            # Simulate user completing SSO and being sent back to the box selection page.
            driver.current_url = TARGET
            worker._advance_login()
            self.assertEqual(worker._start_count, 1)
            self.assertFalse(worker.login_waiting)

    def test_credentials_submit_only_at_official_host(self):
        worker, driver = self.setup_worker()
        cfg = SiteConfig(url=TARGET)
        with patch.object(kuji_gui, 'By', FakeBy, create=True), patch.object(kuji_gui, 'SELENIUM_AVAILABLE', True):
            worker._begin_login(cfg, 'StrongDemoPassword', True)
            self.assertEqual(driver.current_url, SSO)
            worker._advance_login()
            self.assertEqual(driver.values['username'], 'demo@sample.jp')
            self.assertEqual(driver.values['password'], 'StrongDemoPassword')
            self.assertTrue(worker.login_credentials_submitted)
            self.assertEqual(driver.current_url, TARGET)
            worker._advance_login()
            self.assertEqual(worker._start_count, 1)
            self.assertEqual(worker.login_password, '')

    def test_outside_form_exact_login_button_selected(self):
        worker, driver = self.setup_worker()
        driver.no_form = True
        with patch.object(kuji_gui, 'By', FakeBy, create=True), patch.object(kuji_gui, 'SELENIUM_AVAILABLE', True):
            worker._begin_login(SiteConfig(url=TARGET), 'secret-example-not-real', True)
            worker._advance_login()
            self.assertTrue(worker.login_credentials_submitted)
            self.assertEqual(driver.current_url, TARGET)
            self.assertEqual(driver.values['username'], 'demo@sample.jp')

    def test_form_disabled_until_fields_filled(self):
        worker, driver = self.setup_worker()
        with patch.object(kuji_gui, 'By', FakeBy, create=True), patch.object(kuji_gui, 'SELENIUM_AVAILABLE', True):
            worker._begin_login(SiteConfig(url=TARGET), 'test-password-not-real', True)
            self.assertFalse(FakeElement(driver, 'submit').is_enabled())
            worker._advance_login()
            self.assertTrue(worker.login_credentials_submitted)
            self.assertTrue(worker.login_credentials_attempted)
            self.assertEqual(driver.values['password'], 'test-password-not-real')
            self.assertEqual(worker.login_password, '')

    def test_safe_button_caption_rejects_social_and_passkey(self):
        self.assertTrue(is_login_button_text('登入'))
        self.assertTrue(is_login_button_text('ログイン'))
        self.assertTrue(is_login_button_text('Sign in'))
        self.assertFalse(is_login_button_text('使用通行密鑰登入'))
        self.assertFalse(is_login_button_text('Google 登入'))
        self.assertFalse(is_login_button_text('新規登録'))

    def test_css_field_not_password(self):
        self.assertTrue(suspicious_password_css('@abcExampleNotASelector'))
        self.assertFalse(suspicious_password_css('input[type="password"]'))

    def test_malicious_redirect_never_gets_password(self):
        worker, driver = self.setup_worker('https://not-bandai.example/login')
        worker.cfg = SiteConfig(url=TARGET)
        worker.login_waiting = True
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            self.assertFalse(worker._attempt_bnid_credentials('example-not-real'))
        self.assertFalse(worker.login_credentials_attempted)
        self.assertEqual(driver.values, {})

    def test_password_attempt_is_not_retried(self):
        worker, driver = self.setup_worker(SSO)
        worker.cfg = SiteConfig(url=TARGET)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            worker._attempt_bnid_credentials('example-not-real')
            self.assertTrue(worker.login_credentials_submitted)
            self.assertFalse(worker._attempt_bnid_credentials('example-not-real'))
        self.assertEqual(driver.values['password'], 'example-not-real')

    def test_password_css_string_rejected(self):
        from dataclasses import asdict
        from kuji_gui import validate_site
        params = asdict(SiteConfig(url=TARGET))
        params['password_css'] = '@example-not-real'
        with self.assertRaisesRegex(ValueError, 'CSS'):
            validate_site(params)

    def test_target_without_box_css_stays_paused(self):
        worker, driver = self.setup_worker(TARGET, boxes=False)
        cfg = SiteConfig(url=TARGET)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            worker._begin_login(cfg, '', True)
            self.assertEqual(worker._start_count, 0)
            self.assertTrue(worker.login_waiting)

    def test_does_not_refresh_login_page_after_session_expiry(self):
        worker, driver = self.setup_worker(LOGIN)
        cfg = SiteConfig(url=TARGET)
        worker.cfg = cfg
        worker.monitoring = True
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            worker._poll()
        self.assertFalse(worker.monitoring)
        self.assertEqual(driver.refreshes, 0)

    def test_separate_workers_do_not_share_login_browser(self):
        w1, d1 = self.setup_worker()
        w2, d2 = self.setup_worker()
        cfg = SiteConfig(url=TARGET)
        with patch.object(kuji_gui, 'By', FakeBy, create=True):
            w1._begin_login(cfg, '', True)
            self.assertEqual(d1.current_url, SSO)
            self.assertEqual(d2.current_url, LOGIN)
            w2._begin_login(cfg, '', False)
            self.assertFalse(w2.login_autostart)
            self.assertTrue(w1.login_autostart)


if __name__ == '__main__':
    unittest.main()
