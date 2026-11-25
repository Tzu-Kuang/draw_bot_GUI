"""v7.1 帳號管理 GUI 驗證：僅在有 DISPLAY 的環境中執行。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from tkinter import Tk, Toplevel, ttk
from unittest.mock import patch

import account_store
import kuji_gui


@unittest.skipUnless(os.environ.get('DISPLAY') or os.name == 'nt', '需桌面顯示環境')
class AccountToolbarGuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.patches = [
            patch.object(account_store, 'APP_DIR', base),
            patch.object(account_store, 'SETTINGS_FILE', base / 'multi_settings.json'),
            patch.object(account_store, 'LEGACY_FILE', base / 'settings.json'),
            patch.object(kuji_gui, 'APP_DIR', base),
        ]
        for p in self.patches:
            p.start()
        self.root = Tk()
        self.app = kuji_gui.KujiApp(self.root)
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def fake_dialog(self, results):
        class Dialog(Toplevel):
            def __init__(self, root, account=None):
                super().__init__(root)
                self.result = results
                self.after_idle(self.destroy)
        return Dialog

    def test_visible_add_account_button_and_empty_hint(self):
        def all_widgets(widget):
            yield widget
            for child in widget.winfo_children():
                yield from all_widgets(child)
        add_buttons = [widget for widget in all_widgets(self.root)
                       if isinstance(widget, ttk.Button) and '＋ 新增帳號' in widget.cget('text')]
        self.assertEqual(len(add_buttons), 1)
        self.assertEqual(add_buttons[0].winfo_manager(), 'pack')
        self.assertEqual(self.app.account_count_var.get(), '目前 0 個帳號')
        self.assertEqual(self.app.empty_accounts_hint.winfo_manager(), 'pack')

    def test_create_two_accounts_immediately_adds_tabs_and_persists(self):
        for alias, username in [('帳號一', 'first@example.net'), ('帳號二', 'second@example.net')]:
            with patch.object(kuji_gui, 'AccountDialog', self.fake_dialog((alias, username, '', False, 0))):
                self.app._add()
        self.root.update()
        self.assertEqual(len(self.app.accounts), 2)
        self.assertEqual(len(self.app.account_tabs.tabs()), 2)
        self.assertEqual(self.app.account_count_var.get(), '目前 2 個帳號')
        self.assertEqual(self.app.empty_accounts_hint.winfo_manager(), '')
        self.assertEqual(self.app.selected_id, self.app.accounts[1].id)
        self.assertEqual(tuple(self.app.account_tree.selection()), (self.app.selected_id,))
        site, loaded, migrated = account_store.read_all()
        self.assertFalse(migrated)
        self.assertEqual([a.alias for a in loaded], ['帳號一', '帳號二'])
        self.assertNotEqual(loaded[0].profile_dir(), loaded[1].profile_dir())

    def test_edit_and_remove_account_updates_tabs(self):
        with patch.object(kuji_gui, 'AccountDialog', self.fake_dialog(('帳號一', 'first@example.net', '', False, 1))):
            self.app._add()
        with patch.object(kuji_gui, 'AccountDialog', self.fake_dialog(('更名帳號', 'first@example.net', '', False, 3))):
            self.app._edit()
        self.assertEqual(self.app.accounts[0].target_rank, 3)
        self.assertEqual(self.app.account_tabs.tab(self.app.tab_pages[self.app.selected_id], 'text'), '更名帳號')
        with patch.object(kuji_gui.messagebox, 'askyesno', return_value=True):
            self.app._remove()
        self.root.update()
        self.assertEqual(len(self.app.account_tabs.tabs()), 0)
        self.assertEqual(self.app.account_count_var.get(), '目前 0 個帳號')
        self.assertEqual(self.app.empty_accounts_hint.winfo_manager(), 'pack')


if __name__ == '__main__':
    unittest.main()
