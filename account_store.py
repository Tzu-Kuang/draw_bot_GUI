"""帳號設定及 Windows DPAPI 密碼保護。非 Windows 系統不落地儲存密碼。"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from ctypes import wintypes

APP_DIR = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.config')) / 'KujiGuiBot'
SETTINGS_FILE = APP_DIR / 'multi_settings.json'
LEGACY_FILE = APP_DIR / 'settings.json'


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(payload: bytes, protect: bool) -> bytes:
    if os.name != 'nt':
        raise RuntimeError('加密儲存密碼僅支援 Windows DPAPI')
    buffer = ctypes.create_string_buffer(payload)
    source = _Blob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = _Blob()
    crypt = ctypes.WinDLL('Crypt32.dll', use_last_error=True)
    kernel = ctypes.WinDLL('Kernel32.dll', use_last_error=True)
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)] if protect else [
        ctypes.POINTER(_Blob), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(_Blob),
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    # 0x01: CRYPTPROTECT_UI_FORBIDDEN（不顯示 OS 對話框）
    ok = fn(ctypes.byref(source), 'Kuji GUI private credential', None, None, None, 0x01,
            ctypes.byref(result)) if protect else fn(ctypes.byref(source), None, None, None, None, 0x01,
                                                   ctypes.byref(result))
    if not ok:
        raise OSError(ctypes.get_last_error(), 'Windows DPAPI 無法存取密碼')
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        kernel.LocalFree(ctypes.cast(result.pbData, ctypes.c_void_p))


def encrypt_password(value: str) -> str:
    return base64.b64encode(_dpapi(value.encode('utf-8'), True)).decode('ascii')


def decrypt_password(value: str) -> str:
    return _dpapi(base64.b64decode(value, validate=True), False).decode('utf-8')


@dataclass
class Account:
    id: str
    alias: str
    username: str = ''
    profile_key: str = ''
    password_dpapi: str = ''  # 只允許儲存 Windows DPAPI 密文
    target_rank: int = 0  # 0 表示跟隨共用設定，其餘為帳號專屬選盒序號

    @classmethod
    def new(cls, alias: str, username: str = '') -> 'Account':
        uid = uuid.uuid4().hex
        return cls(id=uid, alias=alias, username=username, profile_key=uid)

    def profile_dir(self) -> Path:
        safe = re.sub(r'[^A-Za-z0-9_-]', '_', self.profile_key or self.id)[:65]
        return APP_DIR / 'profiles' / (safe or self.id)


def read_all() -> tuple[dict, list[Account], bool]:
    """回傳網站設定、帳號與是否從單帳號舊版匯入。"""
    if SETTINGS_FILE.exists():
        data = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
        accounts = [Account(**{k: d.get(k, 0 if k == 'target_rank' else '') for k in ('id', 'alias', 'username', 'profile_key', 'password_dpapi', 'target_rank')})
                    for d in data.get('accounts', []) if d.get('id') and d.get('alias')]
        return data.get('site', {}), accounts, False
    if LEGACY_FILE.exists():
        data = json.loads(LEGACY_FILE.read_text(encoding='utf-8'))
        profile = str(data.get('profile_name') or 'default')
        safe = re.sub(r'[^A-Za-z0-9_-]', '_', profile)[:60] or 'default'
        return data, [Account(id=uuid.uuid4().hex, alias=profile, profile_key=safe)], True
    return {}, [], False


def save_all(site: dict, accounts: list[Account]):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    data = {'version': 2, 'site': site, 'accounts': [asdict(account) for account in accounts]}
    temp = SETTINGS_FILE.with_suffix('.tmp')
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(SETTINGS_FILE)
    finally:
        if temp.exists():
            temp.unlink()
