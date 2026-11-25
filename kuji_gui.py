"""GUI 多帳號抽獎盒監控：每帳號獨立 Chrome、可選自動登入、同步監控、事件紀錄。

登入表單只在使用者指定的登入網址、相同主機名上填入帳密。
不會繞過 CAPTCHA/2FA/排隊，不進行付款或下單確認。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from tkinter import Tk, Toplevel, BooleanVar, StringVar, Text, Canvas, messagebox, filedialog, ttk
from tkinter import BOTH, END, LEFT, RIGHT, X, Y
from urllib.parse import urlsplit

from account_store import APP_DIR, Account, decrypt_password, encrypt_password, read_all, save_all
from monitor_core import (BoxInfo, NewBoxTracker, MODE_NEW_ID, MODE_AVAILABLE, MODE_ANY_NEW,
                          MODE_LABELS, MODES)
from kuji_box_dom import read_kuji_rows, actionable_kuji_button
from product_flow import is_product_detail, is_box_selection, is_product_purchase_label
from auth_flow import (BNID_LOGIN_HOST, allowed_credential_host,
                       classify_auth_page, is_kuji_site, login_button_xpath,
                       is_login_button_text, suspicious_password_css)

try:
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait
    SELENIUM_AVAILABLE = True
except ImportError:
    SELENIUM_AVAILABLE = False
    # Make non-browser unit tests runnable before Selenium is installed.
    class WebDriverException(Exception):
        pass
    class TimeoutException(WebDriverException):
        pass

BOX_PATTERN = re.compile(r'BOX-[A-Z0-9]+', re.IGNORECASE)


def valid_url(value: str) -> bool:
    try:
        parts = urlsplit(value.strip())
        return (parts.scheme in ('http', 'https') and bool(parts.hostname)
                and (parts.username is None and parts.password is None))
    except ValueError:
        return False


def safe_site(value: str) -> str:
    """事件紀錄只顯示網域與路徑，不輸出可能包含授權資訊的 query。"""
    try:
        p = urlsplit(value)
        return f'{p.scheme}://{p.netloc}{p.path}'
    except ValueError:
        return '(無效網址)'


def normalized(value: str) -> str:
    return ' '.join((value or '').split())


def identity_of(box, preferred_attr: str) -> tuple[str, str]:
    attrs = list(dict.fromkeys(x for x in (preferred_attr, 'data-box-id', 'data-id', 'id') if x))
    for attr in attrs:
        val = (box.get_attribute(attr) or '').strip()
        if val:
            return f'{attr}:{val}', attr
    html = box.get_attribute('outerHTML') or ''
    match = BOX_PATTERN.search(html)
    if match:
        return 'BOX:' + match.group(0).upper(), 'BOX 編號'
    try:
        for link in box.find_elements(By.CSS_SELECTOR, 'a[href]')[:3]:
            href = (link.get_attribute('href') or '').strip()
            if href and not href.startswith('javascript:') and href != '#':
                return 'href:' + href, '內部連結'
    except WebDriverException:
        pass
    txt = normalized(box.text)
    return ('text:' + txt[:180], '文字備援(不穩定)') if txt else ('', '無法識別')


@dataclass(frozen=True)
class SiteConfig:
    url: str
    login_url: str = ''
    interval: float = 5.0
    rank: int = 1
    box_css: str = '.box-card'
    button_css: str = '.draw-btn'
    button_text: str = '抽獎'
    identity_attr: str = 'data-id'
    username_css: str = 'input[name="username"]'
    password_css: str = 'input[type="password"]'
    submit_css: str = 'button[type="submit"]'
    auto_click: bool = True
    stop_after_click: bool = True
    enter_purchase: bool = True  # only '購入する' on matching ProductDetail page
    monitor_mode: str = MODE_NEW_ID
    minimize_browser: bool = False  # 切到背景：獨立 Chrome 仍各占一個程序


def validate_site(raw: dict) -> SiteConfig:
    data = dict(raw)
    data['url'] = str(data.get('url', '')).strip()
    data['login_url'] = str(data.get('login_url', '')).strip()
    data['interval'] = float(data.get('interval', 5))
    data['rank'] = int(data.get('rank', 1))
    if not valid_url(data['url']):
        raise ValueError('請輸入完整監控網址（http:// 或 https://）')
    if data['login_url'] and not valid_url(data['login_url']):
        raise ValueError('登入網址無效；請填入完整網址，或留白使用監控網址')
    if data['login_url'] and urlsplit(data['url']).scheme == 'https' and urlsplit(data['login_url']).scheme != 'https':
        raise ValueError('HTTPS 監控網站不允許自動登入 HTTP 網址')
    if not (2 <= data['interval'] <= 3600):
        raise ValueError('刷新間隔需介於 2～3600 秒，建議至少 5 秒')
    if not (1 <= data['rank'] <= 300):
        raise ValueError('盒子序號需介於 1～300')
    for key in ('box_css', 'button_css', 'username_css', 'password_css', 'submit_css'):
        if not str(data.get(key, '')).strip():
            raise ValueError(f'CSS 欄位 {key} 不能空白')
        data[key] = str(data[key]).strip()
    if suspicious_password_css(data['password_css']):
        raise ValueError('登入密碼 CSS 應填 CSS 選擇器，例如 input[type=\"password\"]；不要填真實密碼')
    if data.get('monitor_mode', MODE_NEW_ID) not in MODES:
        raise ValueError('未知監控模式；請在下拉清單選擇')
    data['button_text'] = str(data.get('button_text', ''))
    data['identity_attr'] = str(data.get('identity_attr', '')).strip()
    return SiteConfig(**{k: data[k] for k in SiteConfig.__dataclass_fields__ if k in data})


class AccountWorker(threading.Thread):
    """每個帳號擁有自己的 WebDriver + thread；不能跨 worker 操作彼此 driver。"""
    def __init__(self, account: Account, events: queue.Queue):
        super().__init__(daemon=True, name='kuji-' + account.id[:8])
        self.account = account
        self.events = events
        self.commands: queue.Queue = queue.Queue()
        self.driver = None
        self.open_url = ''
        self.monitoring = False
        self.closing = False
        self.cfg: SiteConfig | None = None
        self.tracker = NewBoxTracker()
        self.last_total = -1
        self.last_no_boxes_note = 0.0
        self.poll_number = 0
        self.next_poll = 0.0
        self.login_waiting = False
        self.login_autostart = False
        self.login_password = ''
        self.login_next_check = 0.0
        self.login_started_at = 0.0
        self.product_entry_clicked = False
        self.product_entry_clicked_at = 0.0
        self.login_entry_clicked = False
        self.login_credentials_submitted = False
        self.login_credentials_attempted = False
        self.login_note = ''

    def send(self, action: str, cfg: SiteConfig | None = None, password: str = ''):
        self.commands.put((action, cfg, password))

    def emit(self, kind: str, *args):
        self.events.put((kind, self.account.id, *args))

    def log(self, text: str):
        self.emit('log', text)

    def status(self, value: str):
        self.emit('status', value)

    def run(self):
        while not self.closing:
            # 先處理 GUI 命令，避免使用者要求停止時仍多刷新一輪
            try:
                action, cfg, password = self.commands.get_nowait()
            except queue.Empty:
                action = None
            if action:
                try:
                    if action == 'open':
                        self._cancel_login()
                        self.monitoring = False
                        self._open(cfg, navigate=True)
                        self.status('Chrome 已開啟')
                    elif action == 'login':
                        self._begin_login(cfg, password, auto_start=False)
                    elif action == 'scan':
                        self._open(cfg, navigate=False)
                        self._scan(cfg, do_track=False)
                    elif action == 'start':
                        self._cancel_login()
                        if (self.driver is not None and is_kuji_site(cfg.url)
                                and is_product_detail(self.driver.current_url, cfg.url)):
                            self._begin_login(cfg, '', auto_start=True)
                        else:
                            self._start(cfg)
                    elif action == 'resume':
                        # On Kuji, don't navigate away from a post-login ProductDetail.
                        # Continue through the official product entry instead.
                        self._cancel_login()
                        self.monitoring = False
                        self._open(cfg, navigate=False)
                        if is_kuji_site(cfg.url):
                            self._begin_login(cfg, '', auto_start=True)
                        else:
                            self._navigate(cfg.url)
                            self.open_url = cfg.url
                            self._start(cfg)
                    elif action == 'login_start':
                        self._begin_login(cfg, password, auto_start=True)
                    elif action == 'stop':
                        self._cancel_login()
                        self.monitoring = False
                        self.status('已停止')
                        self.log('監控已停止；Chrome 保持開啟')
                    elif action == 'minimize':
                        if self.driver is None:
                            self.log('Chrome 尚未開啟，無法最小化')
                        else:
                            self.driver.minimize_window()
                            self.log('獨立 Chrome 已最小化（瀏覽器仍在背景執行）')
                    elif action == 'show':
                        if self.driver is None:
                            self.log('Chrome 尚未開啟，請先登入')
                        else:
                            self.driver.set_window_size(1150, 800)
                            self.driver.maximize_window()
                            self.log('已顯示此帳號的 Chrome')
                    elif action == 'close':
                        self._cancel_login()
                        self.monitoring = False
                        self._quit()
                        self.status('Chrome 已關閉')
                    elif action == 'shutdown':
                        self._cancel_login()
                        self.monitoring = False
                        self._quit()
                        self.closing = True
                except (WebDriverException, OSError, ValueError, RuntimeError) as exc:
                    self.monitoring = False
                    self.status('錯誤，查看紀錄')
                    # 避免 WebDriver 訊息意外包含憑證值；只顯示類型與通用提示
                    self.log(f'{action} 失敗：{type(exc).__name__}（可檢查 CSS／登入頁／Chrome 狀態）')
                continue
            if self.login_waiting and time.monotonic() >= self.login_next_check:
                self.login_next_check = time.monotonic() + 2.5
                try:
                    self._advance_login()
                except (WebDriverException, OSError, ValueError, RuntimeError) as exc:
                    self._login_note(f'登入檢查暫時失敗：{type(exc).__name__}；保留 Chrome 讓你繼續登入')
                continue
            if self.monitoring and time.monotonic() >= self.next_poll:
                self.next_poll = time.monotonic() + max(2.0, self.cfg.interval)
                try:
                    self._poll()
                except (WebDriverException, OSError, ValueError, RuntimeError) as exc:
                    self.log(f'刷新失敗：{type(exc).__name__}，下一輪再試')
                continue
            time.sleep(0.12)
        self.emit('closed')

    def _quit(self):
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
        self.driver = None
        self.open_url = ''

    def _open(self, cfg: SiteConfig, navigate: bool):
        if not SELENIUM_AVAILABLE:
            raise RuntimeError('缺少 Selenium，請先執行 install.bat')
        if self.driver is None:
            profile_dir = self.account.profile_dir()
            profile_dir.mkdir(parents=True, exist_ok=True)
            options = webdriver.ChromeOptions()
            options.add_argument(f'--user-data-dir={profile_dir}')
            options.add_argument('--disable-notifications')
            self.log('正在啟動獨立 Chrome 設定檔')
            self.driver = webdriver.Chrome(options=options)
            self.driver.set_page_load_timeout(25)
            self.driver.set_window_size(1150, 800)
            navigate = True
        if navigate or (self.open_url and cfg.url != self.open_url):
            self._navigate(cfg.url)
            self.open_url = cfg.url
            self.log('已開啟監控網頁：' + safe_site(cfg.url))

    def _navigate(self, url: str):
        try:
            self.driver.get(url)
        except TimeoutException:
            self.log('網頁載入逾時，使用已載入的內容')
            try:
                self.driver.execute_script('window.stop()')
            except WebDriverException:
                pass

    def _login(self, cfg: SiteConfig, password: str) -> bool:
        """只有在確定停留於使用者指定的登入主機上，才輸入密碼。"""
        self._open(cfg, navigate=False)
        # 若原本 Chrome profile 的登入工作階段仍有效，直接繼續即可。
        try:
            if (urlsplit(self.driver.current_url).hostname == urlsplit(cfg.url).hostname
                    and len(self.driver.find_elements(By.CSS_SELECTOR, cfg.box_css)) > 0):
                self.log('偵測到既有登入工作階段，直接使用，不重複輸入密碼')
                return True
        except WebDriverException:
            pass
        if not self.account.username or not password:
            self.log('未填入完整帳號或密碼；請開啟 Chrome 手動登入，或編輯帳號憑證')
            return False
        login_url = cfg.login_url or cfg.url
        site = urlsplit(login_url)
        if site.scheme != 'https' and site.hostname not in ('localhost', '127.0.0.1', '::1'):
            self.log('為保護帳密，公開網站只允許 HTTPS 自動登入；請修改登入網址')
            return False
        self._navigate(login_url)
        intended = urlsplit(login_url)
        current = urlsplit(self.driver.current_url)
        if (current.scheme, current.hostname, current.port) != (intended.scheme, intended.hostname, intended.port):
            self.log('登入頁轉向其他網域／協定，為保護密碼已停止自動輸入；請手動登入')
            return False
        try:
            username_input = WebDriverWait(self.driver, 8).until(
                lambda d: d.find_element(By.CSS_SELECTOR, cfg.username_css))
            password_input = self.driver.find_element(By.CSS_SELECTOR, cfg.password_css)
            submit_button = self.driver.find_element(By.CSS_SELECTOR, cfg.submit_css)
            if not (username_input.is_displayed() and password_input.is_displayed()
                    and submit_button.is_displayed()):
                raise RuntimeError('登入表單欄位不可見')
        except (TimeoutException, WebDriverException, RuntimeError):
            self.log('未找到登入表單：可能已登入、驗證尚未完成，或 CSS 不符；請人工確認')
            return False
        username_input.clear()
        username_input.send_keys(self.account.username)
        password_input.clear()
        password_input.send_keys(password)
        submit_button.click()
        self.log('已提交登入表單（不代表成功；不會跳過 CAPTCHA 或雙重驗證）')
        try:
            WebDriverWait(self.driver, 12).until(
                lambda d: not any(x.is_displayed() for x in d.find_elements(By.CSS_SELECTOR, cfg.password_css))
            )
        except TimeoutException:
            self.log('登入表單仍存在：可能登入失敗或需要人工驗證，已暫停自動監控')
            return False
        # CAPTCHA / OTP / 雙重驗證不得略過，避免從挑戰頁直接導走。
        try:
            path = urlsplit(self.driver.current_url).path.casefold()
            challenge_selectors = (
                'input[autocomplete="one-time-code"], input[name*="otp" i], '
                'input[name*="code" i], iframe[src*="captcha" i], '
                '[data-sitekey], input[name*="totp" i]'
            )
            challenge_detected = any(token in path for token in (
                'captcha', 'challenge', 'two-factor', 'two_step', '2fa', 'mfa', 'verification', 'verify', '/otp'
            )) or bool(self.driver.find_elements(By.CSS_SELECTOR, challenge_selectors))
            if challenge_detected:
                self.log('可能需要 CAPTCHA／OTP／雙重驗證；保留目前頁面供人工完成')
                return False
        except WebDriverException:
            self.log('無法確認登入驗證狀態，請人工確認')
            return False
        # 進入選盒頁，確認已能取得盒子元素，再允許一鍵開始自動監控。
        self._navigate(cfg.url)
        self.open_url = cfg.url
        try:
            WebDriverWait(self.driver, 7).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, cfg.box_css)) > 0)
        except TimeoutException:
            self.log('登入後仍無法看到盒子；可能需要人工驗證或是網站 CSS 不符，未自動開始')
            return False
        return True

    def _cancel_login(self):
        self.login_waiting = False
        self.login_password = ''
        self.login_autostart = False
        self.login_note = ''

    def _login_note(self, message: str):
        if message != self.login_note:
            self.login_note = message
            self.log(message)
            if self.login_waiting:
                self.status(message[:45])

    def _begin_login(self, cfg: SiteConfig, password: str, auto_start: bool):
        """Start SSO non-blockingly; do not refresh any login/challenge screen."""
        self._cancel_login()
        self.monitoring = False
        self.cfg = cfg
        self.login_waiting = True
        self.login_autostart = auto_start
        self.login_password = password
        self.product_entry_clicked = False
        self.product_entry_clicked_at = 0.0
        self.login_entry_clicked = False
        self.login_credentials_submitted = False
        self.login_credentials_attempted = False
        self.login_started_at = time.monotonic()
        self.login_next_check = time.monotonic() + 2.5
        self._open(cfg, navigate=False)
        if is_kuji_site(cfg.url):
            self.log('偵測為一番くじ ONLINE：先點「バンダイナムコIDでログイン」，再完成官方登入')
        else:
            if self._login(cfg, password):
                self._complete_login(cfg)
                return
            self.log('一般網站登入未確認；請在 Chrome 中完成登入，程式將等待並再次確認')
        self._advance_login()

    def _switch_to_auth_window(self, target_url: str):
        """If a login pop-up exists, switch to it; recover when it closes."""
        try:
            handles = self.driver.window_handles
            if not handles:
                return
            if len(handles) == 1:
                self.driver.switch_to.window(handles[0])
                return
            choices = []
            for handle in handles:
                try:
                    self.driver.switch_to.window(handle)
                    kind = classify_auth_page(self.driver.current_url, target_url)
                    choices.append((handle, kind))
                except WebDriverException:
                    continue
            # After SSO, a pop-up may stay open; prefer an authenticated target
            # tab with visible boxes over an old account/login window.
            for handle, kind in choices:
                if kind == 'target':
                    try:
                        self.driver.switch_to.window(handle)
                        if any(e.is_displayed() for e in
                               self.driver.find_elements(By.CSS_SELECTOR, self.cfg.box_css)):
                            return
                    except WebDriverException:
                        pass
            for wanted in ('product_detail', 'box_selection', 'bnid_login', 'relay', 'target', 'kuji_login'):
                for handle, kind in reversed(choices):
                    if kind == wanted:
                        self.driver.switch_to.window(handle)
                        return
            self.driver.switch_to.window(handles[-1])
        except WebDriverException:
            pass

    def _click_bnid_entry(self) -> bool:
        """Click only the official 'Bandai Namco ID login' entry point, not sign-up."""
        if classify_auth_page(self.driver.current_url, self.cfg.url) != 'kuji_login':
            return False
        try:
            matches = self.driver.find_elements(By.XPATH, login_button_xpath())
            # Some sites put the Japanese label in a span, with click handler
            # on a parent element without role=button.
            if not matches:
                labels = self.driver.find_elements(By.XPATH,
                    '//*[contains(normalize-space(text()), "バンダイナムコIDでログイン")]')
                for label in labels:
                    try:
                        parent = label.find_element(By.XPATH,
                            './ancestor-or-self::*[self::a or self::button or @role="button" or @onclick][1]')
                        matches.append(parent)
                    except WebDriverException:
                        continue
            for item in matches:
                if item.is_displayed() and item.is_enabled():
                    item.click()
                    self.login_entry_clicked = True
                    self._login_note('已點擊「バンダイナムコIDでログイン」；等待官方登入頁')
                    return True
        except WebDriverException:
            pass
        self._login_note('找不到「バンダイナムコIDでログイン」按鈕；請在 Chrome 中手動點擊')
        return False

    @staticmethod
    def _visible_enabled(element) -> bool:
        try:
            return element.is_displayed() and element.is_enabled()
        except WebDriverException:
            return False

    @staticmethod
    def _button_caption(element) -> str:
        """Read only the short accessibility label; do not log user input."""
        try:
            return (element.text or element.get_attribute('value') or
                    element.get_attribute('aria-label') or '').strip()
        except WebDriverException:
            return ''

    def _safe_bnid_submit(self, password_element):
        """Find the *login* action, never social login or passkey buttons.

        Modern login pages may render a disabled submit before inputs are
        populated, and may omit a native <form>.  Selection must therefore
        not depend on is_enabled().
        """
        try:
            form = password_element.find_element(By.XPATH, './ancestor::form[1]')
        except WebDriverException:
            form = None
        selector = 'button, input[type="submit"], [role="button"]'
        try:
            in_form = form.find_elements(By.CSS_SELECTOR, selector) if form else []
        except WebDriverException:
            in_form = []
        matching = []
        for item in in_form:
            try:
                if not item.is_displayed():
                    continue
                caption = self._button_caption(item)
                if is_login_button_text(caption):
                    matching.append(item)
                elif not caption and (item.get_attribute('type') or '').lower() == 'submit':
                    matching.append(item)
            except WebDriverException:
                continue
        if len(matching) == 1:
            return matching[0]
        if len(matching) > 1:
            return None  # ambiguous: never guess a submit target

        # Some SPA pages put the primary login button outside a native form.
        # Only an exact login label (not 'login with Apple/passkey') qualifies.
        try:
            global_matches = [b for b in self.driver.find_elements(By.CSS_SELECTOR, selector)
                              if b.is_displayed() and is_login_button_text(self._button_caption(b))]
        except WebDriverException:
            return None
        return global_matches[0] if len(global_matches) == 1 else None

    def _attempt_bnid_credentials(self, password: str) -> bool:
        """Fill official Bandai credentials once, then click verified login action.

        Does not bypass CAPTCHA/OTP, submit via JavaScript, or retry passwords.
        """
        if self.login_credentials_attempted or not password or not self.account.username:
            return False
        if not allowed_credential_host(self.driver.current_url, self.cfg.url, self.cfg.login_url):
            return False
        if classify_auth_page(self.driver.current_url, self.cfg.url) != 'bnid_login':
            return False
        challenges = self.driver.find_elements(By.CSS_SELECTOR,
            'input[autocomplete="one-time-code"], input[name*="otp" i], '
            'iframe[src*="captcha" i], [data-sitekey]')
        if any(element.is_displayed() for element in challenges):
            self._login_note('偵測到驗證碼／額外驗證，請在 Chrome 人工完成')
            return False
        usernames = self.driver.find_elements(By.CSS_SELECTOR,
            'input[type="email"], input[autocomplete="username"], '
            'input[name="email"], input[name="loginId"], input[name="username"], '
            'input[id*="email" i], input[id*="mail" i]')
        passwords = self.driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]')
        visible_user = next((x for x in usernames if self._visible_enabled(x)), None)
        visible_pass = next((x for x in passwords if self._visible_enabled(x)), None)
        if not visible_user or not visible_pass:
            self._login_note('Bandai 官方登入頁未找到可輸入的帳密欄位；請人工確認')
            return False

        # IMPORTANT v4 fix: find the submit element before input if possible,
        # but never require it to be enabled until AFTER form filling.
        button = self._safe_bnid_submit(visible_pass)
        self.login_credentials_attempted = True
        # Clear secret from worker state as soon as it is no longer needed.
        self.login_password = ''
        try:
            visible_user.clear()
            visible_user.send_keys(self.account.username)
            visible_pass.clear()
            visible_pass.send_keys(password)
        except WebDriverException:
            self._login_note('嘗試自動填寫帳密時發生錯誤；已停止重試，請人工登入')
            return False
        self._login_note('已在官方 Bandai Namco ID 頁面填入帳密；等待登入按鈕啟用')

        # Some JS frameworks replace buttons after updating input state.
        # Resolve again and wait a bounded time; never force-enable controls.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                # Verify the page did not redirect elsewhere during the fill.
                if not allowed_credential_host(self.driver.current_url, self.cfg.url, self.cfg.login_url):
                    self._login_note('填入後頁面已跳轉；不再操作登入按鈕')
                    return False
                button = self._safe_bnid_submit(visible_pass) or button
                if button and self._visible_enabled(button):
                    button.click()
                    self.login_credentials_submitted = True
                    self._login_note('已點擊 Bandai 官方登入按鈕；等待返回選盒頁或人工驗證')
                    return True
            except WebDriverException:
                # React may re-create the input/button DOM; keep waiting briefly.
                pass
            time.sleep(0.18)
        if button is None:
            self._login_note('已填入帳密，但無法安全辨識登入按鈕；請在 Chrome 人工點擊登入')
        else:
            self._login_note('已填入帳密，但登入按鈕仍停用；請檢查欄位及提示訊息，再人工登入')
        return False

    def _click_product_purchase(self) -> bool:
        """One-time click of official entry *on the matching product detail only*.

        NEVER click purchase/payment actions on other pages; don't retry clicks.
        """
        if self.product_entry_clicked or not self.cfg.enter_purchase:
            return False
        current = self.driver.current_url
        if not is_product_detail(current, self.cfg.url):
            return False
        try:
            elements = self.driver.find_elements(By.CSS_SELECTOR,
                'a, button, input[type="button"], input[type="submit"], [role="button"]')
            candidates = []
            for item in elements:
                try:
                    if not (item.is_displayed() and item.is_enabled()):
                        continue
                    labels = [self._button_caption(item)]
                    # Some anchors put the accessible name into aria-label/value.
                    for attribute in ('aria-label', 'value'):
                        labels.append(item.get_attribute(attribute) or '')
                    if any(is_product_purchase_label(label) for label in labels):
                        candidates.append(item)
                except WebDriverException:
                    continue
            if len(candidates) != 1:
                if not candidates:
                    self._login_note('商品詳情頁找不到可點的「購入する」入口；請在 Chrome 手動點擊')
                else:
                    self._login_note(f'商品詳情頁找到 {len(candidates)} 個「購入する」候選，避免誤點；請手動點擊')
                return False
            # Recheck URL after locating element. The UI might have navigated in the meantime.
            if not is_product_detail(self.driver.current_url, self.cfg.url):
                self._login_note('商品頁已切換，取消購入入口點擊')
                return False
            # Set flag BEFORE click: even on page timeout, no duplicate submission.
            self.product_entry_clicked = True
            self.product_entry_clicked_at = time.monotonic()
            candidates[0].click()
            self._login_note('已點擊商品詳情頁「購入する」（進入選盒流程，並非結帳）；等待選盒頁')
            return True
        except WebDriverException as exc:
            self._login_note(f'商品頁購入入口無法點擊：{type(exc).__name__}；不會自動重試，請手動處理')
            return False

    def _advance_login(self):
        if not self.login_waiting or self.driver is None:
            return
        self._switch_to_auth_window(self.cfg.url)
        current_url = self.driver.current_url
        page = classify_auth_page(current_url, self.cfg.url)
        if page == 'blocked':
            self._login_note('網站顯示限制或錯誤頁；已暫停，請人工確認，程式不會繞過限制')
            self._cancel_login()
            self.status('網站限制／錯誤，已暫停')
            return
        if page == 'product_detail':
            if not self.cfg.enter_purchase:
                self._login_note('目前是商品詳情頁；未勾選自動進入選盒，請手動按「購入する」')
            elif not self.product_entry_clicked:
                self._click_product_purchase()
            elif time.monotonic() - self.product_entry_clicked_at > 15:
                self._login_note('購入入口已嘗試點擊，但仍在商品詳情頁；請人工確認是否可購買，不自動重點')
            return
        if page in ('target', 'box_selection'):
            if is_box_selection(current_url, self.cfg.url):
                visible = bool(read_kuji_rows(self.driver))
            else:
                elements = self.driver.find_elements(By.CSS_SELECTOR, self.cfg.box_css)
                visible = any(x.is_displayed() for x in elements)
            if visible:
                self._complete_login(self.cfg)
                return
            self._login_note('已到選盒頁，但暫時未能讀取 BOX 卡片；請確認頁面完全載入，暫不刷新')
            return
        if page == 'kuji_login':
            if not self.login_entry_clicked:
                self._click_bnid_entry()
            else:
                self._login_note('仍在登入入口頁：如沒有跳轉，請手動按「バンダイナムコIDでログイン」')
        elif page == 'bnid_login':
            if self.login_credentials_attempted:
                # Preserve the diagnostic rather than replacing it with a
                # generic waiting message on each two-second check.
                pass
            elif self.login_password and self.account.username:
                self._attempt_bnid_credentials(self.login_password)
            elif not self.account.username or not self.login_password:
                self._login_note('此帳號沒有可用的自動登入帳密；請在「編輯帳號」填入，或在 Chrome 人工登入')
            else:
                self._login_note('請在 Bandai Namco ID 頁面完成登入或驗證；完成後會自動續接監控')
        elif page == 'relay':
            self._login_note('Bandai Namco ID 正在進行登入跳轉；等待返回一番くじ網站')
        else:
            if self.product_entry_clicked:
                self._login_note('購入入口已點擊，現在可能是排隊／中介頁；保留網頁等待，不進行刷新或付款')
            else:
                self._login_note('登入跳轉中；如遇 CAPTCHA／雙重驗證請人工完成，不會強制刷新')
        # Keep browser on its current login/challenge page; do not force navigation.

    def _complete_login(self, cfg: SiteConfig):
        should_start = self.login_autostart
        self._cancel_login()
        self.log('已進入選盒頁且找到盒子元素：開始建立監控基準')
        self.status('登入成功（偵測到盒子）')
        if should_start:
            self._start(cfg)

    def _ready_to_monitor(self, cfg: SiteConfig) -> bool:
        """No auto monitoring if redirected to login or the selector found nothing."""
        try:
            current = self.driver.current_url
            kind = classify_auth_page(current, cfg.url)
            if kind in ('kuji_login', 'bnid_login', 'relay', 'blocked'):
                self.log('目前不是選盒頁而是登入／限制頁；未開始自動刷新')
                self.status('尚未登入／網站限制')
                return False
            if kind not in ('target', 'box_selection'):
                self.log('目前頁面不是指定的選盒網址；未開始監控，避免刷新登入流程')
                self.status('請先完成登入')
                return False
            if is_box_selection(current, cfg.url):
                found = bool(read_kuji_rows(self.driver))
                if not found:
                    self.log('官方選盒頁尚無可辨識 BOX 卡片；停止刷新，請確認頁面已載入')
                    self.status('未辨識到官方 BOX 卡片')
                    return False
            elif not any(x.is_displayed() for x in self.driver.find_elements(By.CSS_SELECTOR, cfg.box_css)):
                self.log('選盒頁上找不到盒子 CSS；未開始監控，請先掃描確認定位')
                self.status('找不到盒子 CSS')
                return False
            return True
        except WebDriverException:
            self.status('無法讀取登入狀態')
            return False

    def _start(self, cfg: SiteConfig):
        self.monitoring = False
        self._open(cfg, navigate=False)
        # A user who presses Start manually should not be navigated away from 2FA.
        if not self._ready_to_monitor(cfg):
            return
        self.cfg = cfg
        self.tracker = NewBoxTracker()
        self.poll_number = 0
        self.last_total = -1
        self.log(f'開始監控：每 {cfg.interval:g} 秒刷新；{MODE_LABELS[cfg.monitor_mode]}' +
                 (f'、第 {cfg.rank} 盒' if cfg.monitor_mode != MODE_ANY_NEW else '、不限盒序') +
                 ('；自動點擊' if cfg.auto_click else '；僅偵測'))
        self.log('第一次掃描只建立基準，不點擊已存在盒子')
        if is_box_selection(self.driver.current_url, cfg.url):
            self.log('官方選盒模式：辨識 BOX-編號；紫／黃／橘「このくじ箱を見る」可點，售罄略過')
        self._scan(cfg, do_track=True)
        self.monitoring = True
        self.status('監控中')
        self.next_poll = time.monotonic() + cfg.interval
        if cfg.minimize_browser:
            try:
                self.driver.minimize_window()
                self.log('Chrome 已最小化，保留獨立帳號；需要時按「顯示 Chrome」')
            except WebDriverException:
                self.log('Chrome 無法最小化，將繼續使用正常視窗')

    def _poll(self):
        if not self._ready_to_monitor(self.cfg):
            self.monitoring = False
            self.log('登入狀態已改變，監控自動暫停（避免刷新登入／驗證頁）')
            return
        self.poll_number += 1
        try:
            self.driver.refresh()
        except TimeoutException:
            self.log('刷新逾時，使用目前頁面')
            try:
                self.driver.execute_script('window.stop()')
            except WebDriverException:
                pass
        if not self._ready_to_monitor(self.cfg):
            self.monitoring = False
            self.log('刷新後不在選盒頁，已暫停；請重新登入')
            return
        self.log(f'第 {self.poll_number} 次刷新')
        self._scan(self.cfg, do_track=True)

    def _scan(self, cfg: SiteConfig, do_track: bool):
        """Kuji 官方頁優先採用卡片 + 對應連結；其他站沿用使用者 CSS。"""
        official = is_box_selection(self.driver.current_url, cfg.url)
        boxes: list[BoxInfo] = []
        click_targets = []
        if official:
            rows = read_kuji_rows(self.driver)
            boxes = [r.info for r in rows]
            click_targets = [r.button for r in rows]
        else:
            try:
                WebDriverWait(self.driver, 5).until(
                    lambda drv: len(drv.find_elements(By.CSS_SELECTOR, cfg.box_css)) > 0)
            except TimeoutException:
                pass
            candidates = self.driver.find_elements(By.CSS_SELECTOR, cfg.box_css)
            for element in candidates[:300]:
                try:
                    if not element.is_displayed():
                        continue
                    identity, source = identity_of(element, cfg.identity_attr)
                    buttons = self._matching_buttons(element, cfg)
                    active = [b for b in buttons if b.is_displayed() and b.is_enabled()]
                    boxes.append(BoxInfo(len(boxes) + 1, identity, normalized(element.text)[:95],
                                         '可點' if active else '無可用按鈕', source))
                    click_targets.append((element, active[0]) if active else (element, None))
                except WebDriverException:
                    boxes.append(BoxInfo(len(boxes) + 1, '', '元素變更，待重試', '待重試', ''))
                    click_targets.append((None, None))
        self.emit('boxes', boxes, cfg.rank)
        if len(boxes) != self.last_total:
            self.log(f'找到 {len(boxes)} 個盒子；' + ('官方 BOX ID 辨識' if official else f'CSS={cfg.box_css}'))
            self.last_total = len(boxes)
        if not boxes:
            if time.monotonic() - self.last_no_boxes_note > 25:
                self.last_no_boxes_note = time.monotonic()
                self.log('無盒子：請檢查登入狀態、頁面 DOM 或 CSS；不會建立基準')
            return
        if not do_track:
            return
        outcome = self.tracker.observe(boxes, cfg.rank, cfg.monitor_mode)
        if outcome.baseline_created:
            self.log(f'建立基準：{len(self.tracker.seen)} 個既有盒子')
            if any(x.identity_source == '文字備援(不穩定)' for x in boxes):
                self.log('警告：盒子 ID 使用文字備援，文字變動可能誤認為新盒')
            return
        if outcome.new_identities:
            self.log(f'發現 {len(outcome.new_identities)} 個新盒：' + ', '.join(outcome.new_identities[:5]))
        if cfg.monitor_mode == MODE_AVAILABLE and outcome.became_actionable:
            self.log(f'偵測到恢復可點：' + ', '.join(outcome.became_actionable[:5]))
        if outcome.duplicate_selected_id:
            self.log('指定盒子 ID 重複：避免誤點，取消點擊')
            return
        if not (outcome.should_click and outcome.selected_identity):
            return
        if not cfg.auto_click:
            self.tracker.clicked(outcome.selected_identity, True)
            reason = '恢復可點' if outcome.trigger == 'became_available' else '新 ID'
            self.log(f'偵測通知：第 {outcome.target_index} 盒 {outcome.selected_identity}（{reason}，不點擊）')
            return
        try:
            idx = (outcome.target_index or cfg.rank) - 1
            if idx < 0 or idx >= len(click_targets):
                raise RuntimeError('目標盒序已不存在')
            if official:
                button = click_targets[idx]
                if not actionable_kuji_button(button):
                    # 販售結束／未上架：不視為成功，不點擊灰色按鈕。
                    raise RuntimeError('指定新盒目前不可選擇（售罄或未顯示選盒連結）')
                # 瀏覽器被最小化時先還原，避免 Selenium 原生 click 被背景視窗攔下。
                if cfg.minimize_browser:
                    self.driver.set_window_size(1150, 800)
                # 再以 ID 檢查最新 DOM，避免視窗變化、刷新或排序造成誤點。
                latest = read_kuji_rows(self.driver)
                if idx >= len(latest) or latest[idx].info.identity != outcome.selected_identity:
                    raise RuntimeError('點擊前卡片順位或 BOX ID 已變更，取消本次點擊')
                button = latest[idx].button
                if not actionable_kuji_button(button):
                    raise RuntimeError('點擊前按鈕已失效')
            else:
                element, button = click_targets[idx]
                if element is None or button is None:
                    raise RuntimeError('指定盒子沒有可用按鈕')
                actual_id, _ = identity_of(element, cfg.identity_attr)
                if actual_id != outcome.selected_identity:
                    raise RuntimeError('按鈕所在盒子 ID 發生變更')
            # 一律用原生 Selenium click，遵循原站正常互動；不注入點擊事件。
            # 非官方站使用背景 Chrome 時也先恢復以避免原生點擊失敗。
            if not official and cfg.minimize_browser:
                self.driver.set_window_size(1150, 800)
            button.click()
            self.tracker.clicked(outcome.selected_identity, True)
            label = '「このくじ箱を見る」' if official else '指定按鈕'
            reason = '恢復可點' if outcome.trigger == 'became_available' else '新 ID'
            self.log(f'已點擊第 {idx + 1} 盒 {outcome.selected_identity} 的 {label}（{reason}）；請在 Chrome 確認')
            self.emit('clicked')
            if cfg.stop_after_click:
                self.monitoring = False
                self.status('已點擊，暫停')
                self.log('依設定停止刷新，保留瀏覽器供人工確認（不會自動付款）')
        except (WebDriverException, RuntimeError) as exc:
            exhausted = self.tracker.clicked(outcome.selected_identity, False)
            detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
            self.log('點擊失敗：' + detail + ('；已達重試上限' if exhausted else '；下輪重試'))

    @staticmethod
    def _matching_buttons(box, cfg: SiteConfig):
        buttons = box.find_elements(By.CSS_SELECTOR, cfg.button_css)
        if not cfg.button_text:
            return buttons
        keyword = cfg.button_text.casefold()
        results = []
        for b in buttons:
            label = ' '.join([b.text or '', b.get_attribute('aria-label') or '', b.get_attribute('value') or ''])
            if keyword in label.casefold():
                results.append(b)
        return results


class AccountDialog(Toplevel):
    """小視窗編輯帳號與密碼，不把密碼寫到 logs。"""
    def __init__(self, root: Tk, account: Account | None = None):
        super().__init__(root)
        self.title('新增帳號' if account is None else '編輯帳號')
        self.geometry('480x375')
        self.resizable(False, False)
        self.transient(root)
        self.grab_set()
        self.result = None
        self.alias = StringVar(value=account.alias if account else '')
        self.username = StringVar(value=account.username if account else '')
        self.password = StringVar()
        self.remember = BooleanVar(value=bool(account and account.password_dpapi))
        self.rank = StringVar(value=str(account.target_rank if account else 0))
        frame = ttk.Frame(self, padding=17)
        frame.pack(fill=BOTH, expand=True)
        for idx, (title, var, masked) in enumerate((('帳號名稱（清單顯示）', self.alias, False),
                                                    ('登入帳號 / Email', self.username, False),
                                                    ('登入密碼（留白表示沿用已存密碼）', self.password, True))):
            ttk.Label(frame, text=title).grid(row=idx * 2, column=0, sticky='w', pady=(2, 3))
            entry = ttk.Entry(frame, textvariable=var, show='*' if masked else '')
            entry.grid(row=idx * 2 + 1, column=0, sticky='ew', pady=(0, 7))
        ttk.Label(frame, text='帳號專屬盒子序號（0 = 使用上方共用設定）').grid(row=6, column=0, sticky='w')
        ttk.Entry(frame, textvariable=self.rank, width=10).grid(row=7, column=0, sticky='w', pady=(0, 5))
        frame.columnconfigure(0, weight=1)
        checkbox = ttk.Checkbutton(frame, text='記住密碼（限 Windows：使用目前使用者 DPAPI 加密）', variable=self.remember)
        checkbox.grid(row=8, column=0, sticky='w')
        if os.name != 'nt':
            checkbox.state(['disabled'])
            self.remember.set(False)
        buttons = ttk.Frame(frame)
        buttons.grid(row=9, column=0, sticky='e', pady=10)
        ttk.Button(buttons, text='取消', command=self.destroy).pack(side=RIGHT)
        ttk.Button(buttons, text='儲存', command=self._save).pack(side=RIGHT, padx=8)
        self.bind('<Return>', lambda e: self._save())
        self.bind('<Escape>', lambda e: self.destroy())

    def _save(self):
        alias = self.alias.get().strip()
        username = self.username.get().strip()
        if not alias:
            messagebox.showerror('必要欄位', '請填入帳號顯示名稱', parent=self)
            return
        try:
            target_rank = int(self.rank.get())
        except ValueError:
            target_rank = -1
        if not (0 <= target_rank <= 300):
            messagebox.showerror('序號不正確', '專屬盒子序號請填 0～300', parent=self)
            return
        self.result = (alias, username, self.password.get(), self.remember.get(), target_rank)
        self.destroy()


class KujiApp:
    def __init__(self, root: Tk):
        self.root = root
        root.title('新盒監控 v7.1 / 三模式・多帳號分頁・新增帳號')
        root.geometry('1230x900')
        root.minsize(850, 560)
        self.events: queue.Queue = queue.Queue()
        self.accounts: list[Account] = []
        self.workers: dict[str, AccountWorker] = {}
        self.runtime_passwords: dict[str, str] = {}
        self.state: dict[str, dict] = {}
        self.selected_id: str | None = None
        self._closing = False
        self._visible_log_entries = 0
        self.vars = {
            'url': StringVar(), 'login_url': StringVar(), 'interval': StringVar(value='5'),
            'rank': StringVar(value='1'), 'box_css': StringVar(value='.box-card'),
            'button_css': StringVar(value='.draw-btn'), 'button_text': StringVar(value='抽獎'),
            'identity_attr': StringVar(value='data-id'),
            'username_css': StringVar(value='input[name="username"]'),
            'password_css': StringVar(value='input[type="password"]'),
            'submit_css': StringVar(value='button[type="submit"]'),
            'auto_click': BooleanVar(value=True), 'stop_after_click': BooleanVar(value=True),
            'enter_purchase': BooleanVar(value=True),
            'monitor_mode': StringVar(value=MODE_NEW_ID),
            'minimize_browser': BooleanVar(value=False),
        }
        self.tab_pages = {}          # 帳號 id -> 分頁 Frame
        self.tab_trees = {}          # 帳號 id -> 盒子 Treeview
        self.tab_logs = {}           # 帳號 id -> 個別事件紀錄
        self.tab_status_vars = {}    # 帳號 id -> 分頁狀態
        self.tree_owners = {}        # Treeview -> 帳號 id
        self.mode_label_var = StringVar(value=MODE_LABELS[MODE_NEW_ID])
        self.account_count_var = StringVar(value='目前 0 個帳號')
        self._build_ui()
        self._load()
        self.mode_label_var.set(MODE_LABELS.get(self.vars['monitor_mode'].get(), MODE_LABELS[MODE_NEW_ID]))
        self._update_accounts()
        self.root.after(130, self._pump)
        self.root.protocol('WM_DELETE_WINDOW', self._close)
        self.root.bind('<Control-n>', lambda _event: self._add())
        self._log('v7.1 已就緒：固定新增帳號按鈕＋三種監控模式＋帳號分頁；底部事件紀錄固定。')
        if suspicious_password_css(self.vars['password_css'].get()):
            self._log('安全提醒：登入密碼 CSS 疑似不是選擇器；請填 input[type="password"]。若填入真實密碼，請立即更換密碼。')

    def _build_ui(self):
        style = ttk.Style(self.root)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('Title.TLabel', font=('Microsoft JhengHei UI', 16, 'bold'))
        # 底部事件紀錄固定高度；上面所有設定使用捲動面板。
        # 舊版的三個 expand=True 會在視窗縮小時把最下方的 logs 壓到不可見。
        app_layout = ttk.Frame(self.root, padding=(12, 10, 12, 10))
        app_layout.pack(fill=BOTH, expand=True)
        app_layout.columnconfigure(0, weight=1)
        # 上：固定帳號分頁；中：可捲動網站與帳號設定；下：固定總事件紀錄。
        # 讓四個分頁不用先捲到設定表格最下方才找得到。
        app_layout.rowconfigure(1, weight=1)
        scroll_host = ttk.Frame(app_layout)
        scroll_host.grid(row=1, column=0, sticky='nsew')
        scroll_host.rowconfigure(0, weight=1)
        scroll_host.columnconfigure(0, weight=1)
        content_canvas = Canvas(scroll_host, highlightthickness=0, borderwidth=0)
        content_scrollbar = ttk.Scrollbar(scroll_host, orient='vertical', command=content_canvas.yview)
        content_canvas.configure(yscrollcommand=content_scrollbar.set)
        content_canvas.grid(row=0, column=0, sticky='nsew')
        content_scrollbar.grid(row=0, column=1, sticky='ns')
        outer = ttk.Frame(content_canvas, padding=(0, 0, 10, 6))
        window_id = content_canvas.create_window((0, 0), window=outer, anchor='nw')
        def _fit_canvas(event):
            content_canvas.itemconfigure(window_id, width=event.width)
        def _scroll_region(_event):
            content_canvas.configure(scrollregion=content_canvas.bbox('all'))
        content_canvas.bind('<Configure>', _fit_canvas)
        outer.bind('<Configure>', _scroll_region)
        def _wheel(event):
            content_canvas.yview_scroll(-int(event.delta / 120) if event.delta else 0, 'units')
        def _wheel_linux_up(_event):
            content_canvas.yview_scroll(-1, 'units')
        def _wheel_linux_down(_event):
            content_canvas.yview_scroll(1, 'units')
        # 只對上方畫布啟用滾輪，避免搶走下方事件紀錄捲動。
        content_canvas.bind('<MouseWheel>', _wheel)
        content_canvas.bind('<Button-4>', _wheel_linux_up)
        content_canvas.bind('<Button-5>', _wheel_linux_down)
        ttk.Label(outer, text='新 BOX 多帳號監控 v7.1 ｜三種模式・帳號分頁', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='GUI 每個帳號各有分頁；Chrome 仍是獨立登入環境（同一 Chrome 視窗的普通分頁無法隔離帳號）。').pack(anchor='w', pady=(1, 9))

        site = ttk.LabelFrame(outer, text='1｜監控網站（所有帳號共用）', padding=8)
        site.pack(fill=X)
        self._wide_field(site, '選盒網址', 'url')
        self._wide_field(site, '登入網址（可留白）', 'login_url')
        controls = ttk.Frame(site)
        controls.pack(fill=X, pady=4)
        for label, key, width in (('刷新間隔(秒)', 'interval', 6), ('指定頁面第幾盒', 'rank', 6)):
            ttk.Label(controls, text=label).pack(side=LEFT)
            ttk.Entry(controls, textvariable=self.vars[key], width=width).pack(side=LEFT, padx=(5, 15))
        ttk.Checkbutton(controls, text='發現新盒自動點擊', variable=self.vars['auto_click']).pack(side=LEFT)
        ttk.Checkbutton(controls, text='點擊後停止刷新', variable=self.vars['stop_after_click']).pack(side=LEFT, padx=15)
        mode_row = ttk.Frame(site)
        mode_row.pack(fill=X, pady=(5, 1))
        ttk.Label(mode_row, text='監控模式').pack(side=LEFT, padx=(0, 8))
        mode_selector = ttk.Combobox(mode_row, state='readonly', width=34,
                                     textvariable=self.mode_label_var, values=list(MODE_LABELS.values()))
        mode_selector.pack(side=LEFT)
        mode_selector.bind('<<ComboboxSelected>>', self._select_mode)
        ttk.Label(mode_row, text='「任意順位」忽略盒序；切換模式後需重新開始監控。').pack(side=LEFT, padx=12)
        ttk.Checkbutton(site, text='開始監控後自動最小化 Chrome（四帳號視窗保留在背景，需要時可顯示）',
                        variable=self.vars['minimize_browser']).pack(anchor='w', pady=(3, 0))
        ttk.Checkbutton(site, text='登入後若停在商品詳情頁，自動按「購入する」進入選盒（不做付款）',
                        variable=self.vars['enter_purchase']).pack(anchor='w', pady=(4, 0))
        advanced = ttk.LabelFrame(outer, text='2｜CSS 定位（官方 SelectBox 盒子改用自動辨識；其他網站沿用下列 CSS）', padding=8)
        advanced.pack(fill=X, pady=(8, 0))
        grid = ttk.Frame(advanced)
        grid.pack(fill=X)
        fields = (('盒子 CSS', 'box_css'), ('選盒按鈕 CSS', 'button_css'),
                  ('按鈕文字包含', 'button_text'), ('盒子 ID 屬性', 'identity_attr'),
                  ('登入帳號 CSS', 'username_css'), ('登入密碼 CSS', 'password_css'),
                  ('登入送出 CSS', 'submit_css'))
        for idx, (caption, key) in enumerate(fields):
            row, col = divmod(idx, 2)
            ttk.Label(grid, text=caption).grid(row=row, column=col * 2, sticky='w', pady=2)
            ttk.Entry(grid, textvariable=self.vars[key], width=37).grid(row=row, column=col * 2 + 1, sticky='ew', padx=(8, 20), pady=2)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)
        ttk.Label(advanced,
            text='⚠ CSS 是網頁元素選擇器，不是帳密！登入密碼 CSS 請填 input[type="password"]；實際帳密請按下方「編輯帳號」填寫。'
        ).pack(anchor='w', pady=(4, 0))

        account_frame = ttk.LabelFrame(outer, text='3｜帳號管理（選取帳號後操作；支援全部同時監控）', padding=8)
        account_frame.pack(fill=X, pady=(8, 0))
        act = ttk.Frame(account_frame)
        act.pack(fill=X, pady=(0, 7))
        action_buttons = (('新增帳號', self._add), ('編輯帳號', self._edit), ('移除帳號', self._remove),
                          ('開啟選取', lambda: self._dispatch('open')),
                          ('登入選取（可人工驗證）', lambda: self._dispatch('login')),
                          ('登入並監控選取', lambda: self._dispatch('login_start')),
                          ('掃描選取', lambda: self._dispatch('scan')),
                          ('開始選取', lambda: self._dispatch('start')),
                          ('停止選取', lambda: self._dispatch('stop')),
                          ('關閉選取', lambda: self._dispatch('close')))
        for n, (label, callback) in enumerate(action_buttons):
            ttk.Button(act, text=label, command=callback).grid(row=n // 5, column=n % 5,
                                                               sticky='ew', padx=3, pady=2)
        for n in range(5):
            act.columnconfigure(n, weight=1)
        allrow = ttk.Frame(account_frame)
        allrow.pack(fill=X, pady=(0, 7))
        for n, (label, action) in enumerate((('全部開啟 Chrome', 'open'), ('全部登入並在完成後監控', 'login_start'),
                                           ('全部開始監控（已登入）', 'start'), ('全部停止', 'stop'),
                                           ('全部顯示 Chrome', 'show'), ('全部最小化 Chrome', 'minimize'),
                                           ('全部關閉 Chrome', 'close'))):
            ttk.Button(allrow, text=label, command=lambda a=action: self._dispatch(a, all_accounts=True)).grid(
                row=n // 4, column=n % 4, sticky='ew', padx=3, pady=2)
            allrow.columnconfigure(n % 4, weight=1)
        resume_line = ttk.Frame(account_frame)
        resume_line.pack(fill=X, pady=(0, 6))
        ttk.Button(resume_line, text='已登入：接續購入入口／選盒監控（選取帳號）',
                   command=lambda: self._dispatch('resume')).pack(side=LEFT)
        ttk.Label(resume_line, text='遇登入後商品詳情頁、OTP 或選盒流程可接續，不強制返回。').pack(side=LEFT, padx=12)
        headings = (('alias', '帳號顯示名稱', 142), ('username', '登入帳號', 170), ('rank', '監控盒序', 82),
                    ('state', '狀態', 182), ('boxes', '盒子數', 76), ('last', '最後回報', 108),
                    ('clicks', '點擊次數', 72), ('profile', '獨立 Chrome', 155))
        self.account_tree = ttk.Treeview(account_frame, columns=[x[0] for x in headings], show='headings', height=5)
        for key, caption, width in headings:
            self.account_tree.heading(key, text=caption)
            self.account_tree.column(key, width=width, minwidth=65, anchor='w')
        self.account_tree.pack(fill=X)
        self.account_tree.bind('<<TreeviewSelect>>', self._pick)

        # 一個 GUI 視窗內依帳號動態建立 Notebook 分頁，彼此擁有獨立的監控資料。
        tabs_area = ttk.LabelFrame(app_layout, text='帳號分頁｜每個帳號獨立監控', padding=7)
        tabs_area.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        tabs_area.configure(height=285)
        tabs_area.grid_propagate(False)
        # 關鍵修正：新增/編輯/移除帳號必須在固定頂部，不能藏在中間捲動設定區。
        account_toolbar = ttk.Frame(tabs_area)
        account_toolbar.pack(fill=X, pady=(0, 6))
        ttk.Label(account_toolbar, text='帳號管理', font=('Microsoft JhengHei UI', 11, 'bold')).pack(side=LEFT)
        ttk.Label(account_toolbar, textvariable=self.account_count_var).pack(side=LEFT, padx=(8, 0))
        ttk.Button(account_toolbar, text='＋ 新增帳號 (Ctrl+N)', command=self._add).pack(side=RIGHT, padx=(7, 0))
        ttk.Button(account_toolbar, text='編輯目前帳號', command=self._edit).pack(side=RIGHT, padx=(7, 0))
        ttk.Button(account_toolbar, text='移除目前帳號', command=self._remove).pack(side=RIGHT)
        self.empty_accounts_hint = ttk.Label(
            tabs_area, text='尚未建立帳號：點右上方「＋ 新增帳號」開始。', foreground='#444444')
        self.empty_accounts_hint.pack(anchor='w', pady=(2, 4))
        self.account_tabs = ttk.Notebook(tabs_area)
        self.account_tabs.pack(fill=BOTH, expand=True)
        self.account_tabs.bind('<<NotebookTabChanged>>', self._on_account_tab)

        logs = ttk.LabelFrame(app_layout, text='5｜事件紀錄（固定顯示・帳號分別標記並自動儲存）', padding=7)
        logs.grid(row=2, column=0, sticky='ew', pady=(6, 0))
        logs.configure(height=205)
        logs.grid_propagate(False)
        top = ttk.Frame(logs)
        top.pack(fill=X)
        ttk.Button(top, text='匯出紀錄', command=self._export_log).pack(side=RIGHT)
        ttk.Button(top, text='清除畫面', command=self._clear_logs).pack(side=RIGHT, padx=7)
        self.log_widget = Text(logs, height=8, font=('Consolas', 10), wrap='word')
        log_scroll = ttk.Scrollbar(logs, orient='vertical')
        log_scroll.pack(side=RIGHT, fill=Y)
        self.log_widget.configure(yscrollcommand=log_scroll.set)
        log_scroll.configure(command=self.log_widget.yview)
        self.log_widget.pack(fill=BOTH, expand=True)

    def _select_mode(self, _event=None):
        for mode, label in MODE_LABELS.items():
            if label == self.mode_label_var.get():
                self.vars['monitor_mode'].set(mode)
                return

    def _new_account_tab(self, acc: Account):
        page = ttk.Frame(self.account_tabs, padding=8)
        self.account_tabs.add(page, text=acc.alias)
        self.tab_pages[acc.id] = page
        st_var = StringVar(value='待命')
        self.tab_status_vars[acc.id] = st_var
        header = ttk.Frame(page)
        header.pack(fill=X, pady=(0, 5))
        ttk.Label(header, textvariable=st_var).pack(side=LEFT)
        ttk.Label(header, text='｜本分頁獨立登入 Chrome；請不要混用另一帳號視窗',
                  foreground='#555555').pack(side=LEFT)
        actions = ttk.Frame(page)
        actions.pack(fill=X, pady=(0, 6))
        for label, action in (('登入並監控', 'login_start'), ('接續監控', 'resume'),
                              ('掃描盒子', 'scan'), ('停止', 'stop'),
                              ('顯示 Chrome', 'show'), ('最小化 Chrome', 'minimize')):
            ttk.Button(actions, text=label,
                       command=lambda a=action, aid=acc.id: self._dispatch(a, account_id=aid)).pack(side=LEFT, padx=(0, 7))
        headings = (('index', '盒序', 54), ('identity', '盒子識別', 230), ('button', '按鈕', 140),
                    ('source', 'ID 來源', 125), ('preview', '盒子內容預覽', 360))
        tree = ttk.Treeview(page, columns=[x[0] for x in headings], show='headings', height=5)
        for key, caption, width in headings:
            tree.heading(key, text=caption)
            tree.column(key, width=width, minwidth=55, anchor='w')
        tree.tag_configure('selected', background='#e3efff')
        tree.pack(fill=X)
        tree.bind('<<TreeviewSelect>>', self._pick_box)
        self.tab_trees[acc.id] = tree
        self.tree_owners[tree] = acc.id
        ttk.Label(page, text='本帳號事件紀錄（完整合併紀錄仍固定在視窗底部）').pack(anchor='w', pady=(6, 1))
        text_area = ttk.Frame(page)
        text_area.pack(fill=BOTH, expand=True)
        scroll = ttk.Scrollbar(text_area, orient='vertical')
        scroll.pack(side=RIGHT, fill=Y)
        log = Text(text_area, height=4, wrap='word', font=('Consolas', 9), yscrollcommand=scroll.set)
        log.pack(fill=BOTH, expand=True)
        scroll.configure(command=log.yview)
        self.tab_logs[acc.id] = log
        self._show_boxes(self.state.get(acc.id, {}).get('boxes', []), acc.id)

    def _sync_account_tabs(self):
        ids = {acc.id for acc in self.accounts}
        for aid in list(self.tab_pages):
            if aid not in ids:
                self.account_tabs.forget(self.tab_pages.pop(aid))
                tree = self.tab_trees.pop(aid)
                self.tree_owners.pop(tree, None)
                self.tab_logs.pop(aid, None)
                self.tab_status_vars.pop(aid, None)
        for acc in self.accounts:
            if acc.id not in self.tab_pages:
                self._new_account_tab(acc)
            page = self.tab_pages[acc.id]
            if self.account_tabs.tab(page, 'text') != acc.alias:
                self.account_tabs.tab(page, text=acc.alias)
            st = self.state.get(acc.id, {})
            self.tab_status_vars[acc.id].set(
                f"狀態：{st.get('status','待命')}　盒數：{len(st.get('boxes',[]))}　"
                f"最後回報：{st.get('last','—')}　點擊：{st.get('clicks',0)}　"
                f"目標盒序：{acc.target_rank or self.vars['rank'].get()}")
        if self.selected_id and self.selected_id in self.tab_pages:
            selected_page = str(self.tab_pages[self.selected_id])
            if self.account_tabs.select() != selected_page:
                self.account_tabs.select(self.tab_pages[self.selected_id])

    def _on_account_tab(self, _event=None):
        current = self.account_tabs.select()
        for aid, page in self.tab_pages.items():
            if str(page) == current and aid != self.selected_id:
                self.selected_id = aid
                if self.account_tree.exists(aid):
                    self.account_tree.selection_set(aid)
                    self.account_tree.see(aid)
                break

    def _wide_field(self, parent, label: str, key: str):
        line = ttk.Frame(parent)
        line.pack(fill=X, pady=2)
        ttk.Label(line, text=label, width=18).pack(side=LEFT)
        ttk.Entry(line, textvariable=self.vars[key]).pack(side=LEFT, fill=X, expand=True)

    def _load(self):
        try:
            site, self.accounts, migrated = read_all()
            for key, value in site.items():
                if key in self.vars:
                    self.vars[key].set(value)
            for acc in self.accounts:
                self.state[acc.id] = {'status': '待命', 'boxes': [], 'last': '—', 'clicks': 0}
            if suspicious_password_css(self.vars['password_css'].get()):
                self.vars['password_css'].set('input[type="password"]')
                self._log('安全修正：已清除設定中誤填的「登入密碼 CSS」，改為 input[type="password"]；如原值是真實密碼請更換')
                self._save()
            if migrated:
                self._log('已匯入舊版設定檔與 Chrome profile，建議確認帳號名稱')
                self._save()
        except (OSError, ValueError, TypeError) as exc:
            self._log(f'設定讀取失敗：{type(exc).__name__}')

    def _save(self):
        try:
            current = {k: var.get() for k, var in self.vars.items()}
            if suspicious_password_css(current.get('password_css', '')):
                current['password_css'] = 'input[type="password"]'
                self.vars['password_css'].set(current['password_css'])
                self._log('安全提醒：已將疑似誤填的「登入密碼 CSS」重設為選擇器；若原值是真實密碼，建議更換密碼')
            save_all(current, self.accounts)
        except (OSError, ValueError, TypeError) as exc:
            self._log(f'儲存設定失敗：{type(exc).__name__}')

    def _update_accounts(self):
        # 原位更新 Treeview，避免每隔 180 ms 重建表格造成點選/捲動失效。
        ids = {acc.id for acc in self.accounts}
        for iid in self.account_tree.get_children():
            if iid not in ids:
                self.account_tree.delete(iid)
        for acc in self.accounts:
            st = self.state.setdefault(acc.id, {'status': '待命', 'boxes': [], 'last': '—', 'clicks': 0})
            values = (acc.alias, acc.username, str(acc.target_rank) if acc.target_rank else '共用', st['status'], len(st['boxes']),
                      st['last'], st['clicks'], acc.profile_key[:18])
            if self.account_tree.exists(acc.id):
                if tuple(map(str, self.account_tree.item(acc.id, 'values'))) != tuple(map(str, values)):
                    self.account_tree.item(acc.id, values=values)
            else:
                self.account_tree.insert('', END, iid=acc.id, values=values)
        if not self.selected_id or self.selected_id not in ids:
            self.selected_id = self.accounts[0].id if self.accounts else None
        if self.selected_id:
            if self.account_tree.selection() != (self.selected_id,):
                self.account_tree.selection_set(self.selected_id)
            self._show_boxes(self.state[self.selected_id]['boxes'], self.selected_id)
        self.account_count_var.set(f'目前 {len(self.accounts)} 個帳號')
        # 首次使用時提供可見提示；新增帳號後立即切換成帳號分頁。
        if self.accounts:
            self.empty_accounts_hint.pack_forget()
        elif not self.empty_accounts_hint.winfo_manager():
            self.empty_accounts_hint.pack(anchor='w', pady=(2, 4), before=self.account_tabs)
        self._sync_account_tabs()

    def _pick(self, _event=None):
        selected = self.account_tree.selection()
        if selected:
            self.selected_id = selected[0]
            self._show_boxes(self.state.get(self.selected_id, {}).get('boxes', []), self.selected_id)
            if self.selected_id in self.tab_pages:
                self.account_tabs.select(self.tab_pages[self.selected_id])

    def _pick_box(self, event=None):
        tree = getattr(event, 'widget', None)
        aid = self.tree_owners.get(tree)
        if not aid:
            return
        selected = tree.selection()
        if not selected:
            return
        values = tree.item(selected[0], 'values')
        acc = next((x for x in self.accounts if x.id == aid), None)
        if not (values and acc):
            return
        try:
            chosen_rank = int(values[0])
        except (ValueError, TypeError):
            return
        acc.target_rank = chosen_rank
        self.selected_id = aid
        self._save()
        self._update_accounts()
        self._log(f'[{acc.alias}] 目標盒序改為第 {chosen_rank} 盒；重新開始監控後套用（任意順位模式不使用盒序）')

    def _show_boxes(self, boxes, aid=None):
        aid = aid or self.selected_id
        tree = self.tab_trees.get(aid)
        if tree is None:
            return
        for iid in tree.get_children():
            tree.delete(iid)
        try:
            acc = next((x for x in self.accounts if x.id == aid), None)
            rank = acc.target_rank if acc and acc.target_rank else int(self.vars['rank'].get())
        except ValueError:
            rank = 1
        for item in boxes:
            tree.insert('', END, values=(item.index, item.identity, item.button_status,
                item.identity_source, item.preview), tags=('selected',) if item.index == rank else ())

    def _add(self):
        dialog = AccountDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        alias, username, pwd, remember, target_rank = dialog.result
        if any(x.alias.casefold() == alias.casefold() for x in self.accounts):
            messagebox.showerror('名稱重複', '請使用不同的帳號顯示名稱')
            return
        acc = Account.new(alias, username)
        acc.target_rank = target_rank
        if pwd:
            self.runtime_passwords[acc.id] = pwd
            if remember:
                try:
                    acc.password_dpapi = encrypt_password(pwd)
                except (OSError, RuntimeError) as exc:
                    messagebox.showwarning('未儲存密碼', '無法加密儲存；密碼只會保留在本次執行記憶體')
        self.accounts.append(acc)
        self.state[acc.id] = {'status': '待命', 'boxes': [], 'last': '—', 'clicks': 0}
        self.selected_id = acc.id
        self._save()
        self._update_accounts()
        self._log(f'已新增帳號：{alias}')

    def _edit(self):
        acc = self._selected_account()
        if not acc:
            return
        if acc.id in self.workers and self.workers[acc.id].is_alive() and self.workers[acc.id].driver is not None:
            messagebox.showwarning('先關閉瀏覽器', '編輯帳號之前，請先按「全部關閉 Chrome」或個別停止並關閉該帳號 Chrome')
            return
        dialog = AccountDialog(self.root, acc)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        alias, username, pwd, remember, target_rank = dialog.result
        if any(x.id != acc.id and x.alias.casefold() == alias.casefold() for x in self.accounts):
            messagebox.showerror('名稱重複', '請使用不同的帳號顯示名稱')
            return
        acc.alias, acc.username, acc.target_rank = alias, username, target_rank
        if pwd:
            self.runtime_passwords[acc.id] = pwd
        if remember:
            if pwd:
                try:
                    acc.password_dpapi = encrypt_password(pwd)
                except (OSError, RuntimeError):
                    acc.password_dpapi = ''
                    messagebox.showwarning('加密失敗', '本次仍可使用密碼，但沒有存到硬碟')
        else:
            acc.password_dpapi = ''
        self._save()
        self._update_accounts()
        self._log(f'已更新帳號設定：{alias}')

    def _remove(self):
        acc = self._selected_account()
        if not acc:
            return
        if not messagebox.askyesno('移除帳號', f'確認將「{acc.alias}」從清單移除？\n不會刪除 Chrome 資料夾。'):
            return
        worker = self.workers.pop(acc.id, None)
        if worker:
            worker.send('shutdown')
        self.runtime_passwords.pop(acc.id, None)
        self.accounts = [x for x in self.accounts if x.id != acc.id]
        self.state.pop(acc.id, None)
        self.selected_id = None
        self._save()
        self._update_accounts()
        self._log(f'已移除帳號：{acc.alias}')

    def _selected_account(self) -> Account | None:
        match = next((acc for acc in self.accounts if acc.id == self.selected_id), None)
        if match is None:
            messagebox.showinfo('請選擇帳號', '請先新增並選取一個帳號')
        return match

    def _get_site(self) -> SiteConfig | None:
        try:
            return validate_site({k: var.get() for k, var in self.vars.items()})
        except (ValueError, TypeError) as exc:
            messagebox.showerror('網站設定有誤', str(exc))
            return None

    def _password(self, acc: Account) -> str:
        pwd = self.runtime_passwords.get(acc.id, '')
        if pwd:
            return pwd
        if acc.password_dpapi:
            try:
                pwd = decrypt_password(acc.password_dpapi)
                self.runtime_passwords[acc.id] = pwd
                return pwd
            except (OSError, ValueError, RuntimeError):
                self._log(f'[{acc.alias}] 加密密碼無法讀取，請重新編輯帳號')
        return ''

    def _dispatch(self, action: str, all_accounts: bool = False, account_id: str | None = None):
        if not self.accounts:
            messagebox.showinfo('尚無帳號', '請先按「新增帳號」')
            return
        if all_accounts:
            accounts = list(self.accounts)
        elif account_id is not None:
            accounts = [a for a in self.accounts if a.id == account_id]
            if not accounts:
                return
        else:
            account = self._selected_account()
            if not account:
                return
            accounts = [account]
        cfg = None
        if action not in ('stop', 'close', 'show', 'minimize'):
            cfg = self._get_site()
            if cfg is None:
                return
            self._save()
        dispatched = 0
        for acc in accounts:
            worker = self.workers.get(acc.id)
            if worker is None or not worker.is_alive():
                if action in ('stop', 'close', 'show', 'minimize'):
                    continue
                worker = AccountWorker(acc, self.events)
                worker.start()
                self.workers[acc.id] = worker
            pwd = self._password(acc) if action in ('login', 'login_start') else ''
            account_cfg = replace(cfg, rank=acc.target_rank) if cfg and acc.target_rank else cfg
            worker.send(action, account_cfg, pwd)
            dispatched += 1
            self.state[acc.id]['status'] = f'排程：{action}'
        self._update_accounts()
        self._log(f'已送出 {action}：{dispatched} 個帳號' + ('；其餘帳號尚未開啟 Chrome' if dispatched < len(accounts) else '（獨立平行執行）'))

    def _pump(self):
        for _ in range(180):
            try:
                kind, aid, *data = self.events.get_nowait()
            except queue.Empty:
                break
            acc = next((a for a in self.accounts if a.id == aid), None)
            if not acc:
                continue
            st = self.state[aid]
            if kind == 'log':
                self._log(f'[{acc.alias}] {data[0]}')
                tab_log = self.tab_logs.get(aid)
                if tab_log:
                    tab_log.insert(END, f'[{datetime.now():%H:%M:%S}] {data[0]}\n')
                    if int(tab_log.index('end-1c').split('.')[0]) > 600:
                        tab_log.delete('1.0', '101.0')
                    tab_log.see(END)
            elif kind == 'status':
                st['status'] = data[0]
            elif kind == 'boxes':
                st['boxes'] = data[0]
                st['last'] = datetime.now().strftime('%H:%M:%S')
                self._show_boxes(st['boxes'], aid)
            elif kind == 'clicked':
                st['clicks'] += 1
            elif kind == 'closed':
                st['status'] = '已關閉'
        if not self._closing:
            self._update_accounts()
            self.root.after(180, self._pump)

    def _log(self, msg: str):
        line = f'[{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}] {msg}\n'
        self.log_widget.insert(END, line)
        self._visible_log_entries += 1
        if self._visible_log_entries > 4000:
            self.log_widget.delete('1.0', '501.0')
            self._visible_log_entries -= 500
        self.log_widget.see(END)
        try:
            directory = APP_DIR / 'logs'
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f'{datetime.now():%Y-%m-%d}.log').open('a', encoding='utf-8') as f:
                f.write(line)
        except OSError:
            pass

    def _clear_logs(self):
        self.log_widget.delete('1.0', END)
        for log in self.tab_logs.values():
            log.delete('1.0', END)
        self._visible_log_entries = 0

    def _export_log(self):
        path = filedialog.asksaveasfilename(title='匯出事件紀錄', defaultextension='.txt', filetypes=[('文字檔', '*.txt')])
        if path:
            try:
                Path(path).write_text(self.log_widget.get('1.0', END), encoding='utf-8')
                self._log('已匯出事件紀錄')
            except OSError:
                messagebox.showerror('匯出失敗', '無法寫入所選位置')

    def _close(self):
        self._closing = True
        self._save()
        for worker in self.workers.values():
            worker.send('shutdown')
        self._wait_count = 0
        self._wait_close()

    def _wait_close(self):
        if all(not w.is_alive() for w in self.workers.values()) or self._wait_count > 50:
            self.root.destroy()
            return
        self._wait_count += 1
        self.root.after(120, self._wait_close)


def main():
    root = Tk()
    KujiApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
