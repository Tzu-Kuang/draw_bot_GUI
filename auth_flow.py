"""Official SSO login page classification and credential sending safeguards.

Never send a password to an arbitrary redirect target: only exact HTTPS hosts.
"""
from __future__ import annotations
from urllib.parse import urlsplit
from product_flow import is_product_detail, is_box_selection

KUJI_HOST = 'on-line.1kuji.com'
BNID_LOGIN_HOST = 'account.bandainamcoid.com'
BNID_RELAY_HOST = 'www.bandainamcoid.com'


def is_kuji_site(url: str) -> bool:
    return (urlsplit(url).hostname or '').lower() == KUJI_HOST


def classify_auth_page(url: str, target_url: str) -> str:
    """Page types: target / kuji_login / bnid_login / relay / blocked / other."""
    try:
        current = urlsplit(url)
        target = urlsplit(target_url)
        host = (current.hostname or '').lower()
        path = current.path.casefold()
        if host == KUJI_HOST:
            if path == '/form/login.aspx':
                return 'kuji_login'
            if path in ('/access.html', '/form/error.aspx'):
                return 'blocked'
        if host == KUJI_HOST and is_product_detail(url, target_url):
            return 'product_detail'
        # Preserve the original target status for a configured SelectBox URL.
        if (current.scheme, host, current.port, path) == (
            target.scheme, (target.hostname or '').lower(), target.port, target.path.casefold()
        ) and (host != KUJI_HOST or not path.endswith('/selectbox.aspx')
               or is_box_selection(url, target_url)):
            return 'target'
        if host == KUJI_HOST and is_box_selection(url, target_url):
            return 'box_selection'
        if host == BNID_LOGIN_HOST:
            return 'bnid_login'
        if host == BNID_RELAY_HOST:
            return 'relay'
        if (current.scheme, host, current.port, path) == (
            target.scheme, (target.hostname or '').lower(), target.port, target.path.casefold()
        ):
            return 'target'
        return 'other'
    except ValueError:
        return 'other'


def allowed_credential_host(current_url: str, monitor_url: str, login_url: str = '') -> bool:
    """Bandai login: exact account.* HTTPS. Generic sites: configured same host/protocol only."""
    try:
        current = urlsplit(current_url)
        if current.scheme != 'https':
            # Loopback HTTP allowed only in the included local test server.
            if (current.hostname or '') not in ('127.0.0.1', 'localhost', '::1'):
                return False
        if is_kuji_site(monitor_url):
            return current.scheme == 'https' and current.hostname == BNID_LOGIN_HOST
        intended = urlsplit(login_url or monitor_url)
        return ((current.scheme, current.hostname, current.port) ==
                (intended.scheme, intended.hostname, intended.port))
    except ValueError:
        return False


def login_button_xpath() -> str:
    return ('//*[self::a or self::button or self::input or self::div[@role="button"]]'
            '[contains(normalize-space(.), "バンダイナムコIDでログイン")'
            ' or contains(@value, "バンダイナムコIDでログイン")'
            ' or contains(@aria-label, "バンダイナムコIDでログイン")]')


# Only a primary login button, never passkey/social signup/account linking.
def is_login_button_text(value: str) -> bool:
    from re import sub
    value = sub(r"[\s\u3000]+", "", value or "").casefold()
    return value in {"ログイン", "登入", "登录", "login", "signin", "logon"}


def suspicious_password_css(value: str) -> bool:
    """Catch common accidental plain-text password entry, never echo it."""
    value = (value or '').strip()
    return bool(value) and (value.startswith('@') or
                            (not any(c in value for c in '#.[>: ') and
                             not value.startswith(('input', 'textarea', '*'))))
