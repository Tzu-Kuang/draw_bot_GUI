"""多帳號設定、登入安全與本機測試站整合測試（不需 Selenium）。"""
from __future__ import annotations

import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import demo_site
from account_store import Account
from kuji_gui import SiteConfig, valid_url, validate_site


class ConfigTests(unittest.TestCase):
    def sample(self):
        return {'url': 'https://example.com/boxes', 'login_url': 'https://example.com/login',
                'interval': '5', 'rank': '2', 'box_css': '.box', 'button_css': '.action',
                'username_css': '#username', 'password_css': '#password', 'submit_css': '#login'}

    def test_site_validation_ok(self):
        cfg = validate_site(self.sample())
        self.assertEqual(cfg.rank, 2)
        self.assertEqual(cfg.interval, 5)
        self.assertEqual(cfg.password_css, '#password')

    def test_reject_http_login_for_https(self):
        data = self.sample()
        data['login_url'] = 'http://example.com/login'
        with self.assertRaisesRegex(ValueError, 'HTTPS'):
            validate_site(data)

    def test_reject_credentials_in_url(self):
        self.assertFalse(valid_url('https://alice:abc@example.com/login'))

    def test_bad_interval(self):
        data = self.sample()
        data['interval'] = '1'
        with self.assertRaises(ValueError):
            validate_site(data)

    def test_separate_account_profiles(self):
        a = Account.new('A', 'user1')
        b = Account.new('B', 'user2')
        self.assertNotEqual(a.id, b.id)
        self.assertNotEqual(a.profile_dir(), b.profile_dir())

    def test_account_rank_override(self):
        from dataclasses import replace
        account = Account.new('A')
        account.target_rank = 3
        config = SiteConfig(url='https://example.com', rank=1)
        independent = replace(config, rank=account.target_rank)
        self.assertEqual(independent.rank, 3)
        self.assertEqual(config.rank, 1)

    def test_no_password_plaintext_in_serialized_account(self):
        a = Account.new('Test', 'username')
        self.assertNotIn('password', asdict(a))
        self.assertEqual(a.password_dpapi, '')

    def test_account_store_roundtrip(self):
        from account_store import read_all, save_all
        with tempfile.TemporaryDirectory() as folder:
            import account_store
            with patch.object(account_store, 'SETTINGS_FILE', Path(folder) / 'multi_settings.json'), \
                 patch.object(account_store, 'LEGACY_FILE', Path(folder) / 'settings.json'), \
                 patch.object(account_store, 'APP_DIR', Path(folder)):
                acc = Account.new('A', 'alice')
                save_all({'url': 'https://example.org/boxes'}, [acc])
                cfg, loaded, migrated = read_all()
                self.assertFalse(migrated)
                self.assertEqual(cfg['url'], 'https://example.org/boxes')
                self.assertEqual(loaded[0].profile_key, acc.profile_key)
                self.assertNotIn('"password":', (Path(folder) / 'multi_settings.json').read_text())

    def test_legacy_migration(self):
        import account_store
        from account_store import read_all
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'settings.json').write_text(json.dumps({'url': 'https://example.org/boxes',
                                                                  'profile_name': 'old-profile'}))
            with patch.object(account_store, 'SETTINGS_FILE', Path(folder) / 'multi_settings.json'), \
                 patch.object(account_store, 'LEGACY_FILE', Path(folder) / 'settings.json'):
                site, accounts, migrated = read_all()
                self.assertTrue(migrated)
                self.assertEqual(accounts[0].profile_key, 'old-profile')
                self.assertEqual(site['url'], 'https://example.org/boxes')


class DemoSiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), demo_site.DemoHandler)
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_address[1])
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def client(self, user):
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        data = urllib.parse.urlencode({'username': user, 'password': 'demo123'}).encode()
        resp = opener.open(self.url + '/login', data=data, timeout=3)
        self.assertEqual(resp.url, self.url + '/')
        return opener

    def test_two_accounts_have_independent_cookies_and_clicks(self):
        one = self.client('demo1')
        two = self.client('demo2')
        self.assertIn('demo1', one.open(self.url, timeout=3).read().decode())
        self.assertIn('demo2', two.open(self.url, timeout=3).read().decode())
        for client in (one, two):
            client.open(self.url + '/click?id=BOX-A002', data=b'', timeout=3).read()
        status = json.load(urllib.request.urlopen(self.url + '/status', timeout=3))
        self.assertTrue(any(item['account'] == 'demo1' for item in status['clicked']))
        self.assertTrue(any(item['account'] == 'demo2' for item in status['clicked']))

    def test_missing_login_redirect(self):
        response = urllib.request.urlopen(self.url + '/', timeout=3)
        self.assertEqual(response.url, self.url + '/login')


if __name__ == '__main__':
    unittest.main()
