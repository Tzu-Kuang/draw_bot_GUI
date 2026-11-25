"""多帳號登入測試站；只監聽本機 http://127.0.0.1:8765。"""
from __future__ import annotations

import html
import json
import threading
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

LOCK = threading.Lock()
BOXES = ['BOX-A002', 'BOX-A001']
CLICKS: list[dict] = []
DISABLED: set[str] = set()
COUNT = 2
DEMO_USERS = {'demo1': 'demo123', 'demo2': 'demo123', 'demo3': 'demo123', 'demo4': 'demo123'}


class DemoHandler(BaseHTTPRequestHandler):
    def account(self) -> str:
        jar = cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
            name = jar['demo_account'].value
            return name if name in DEMO_USERS else ''
        except (KeyError, cookies.CookieError):
            return ''

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/status':
            with LOCK:
                data = {'boxes': BOXES[:], 'disabled': sorted(DISABLED), 'clicked': CLICKS[:]}
            return self.respond(json.dumps(data, ensure_ascii=False), 'application/json; charset=utf-8')
        if path == '/login':
            return self.login_form()
        if path == '/logout':
            return self.redirect('/login', cookie='demo_account=; Path=/; Max-Age=0; HttpOnly')
        if path != '/':
            return self.respond('Not found', 'text/plain', 404)
        user = self.account()
        if not user:
            return self.redirect('/login')
        with LOCK:
            cards = ''.join(
                f'<article class="box-card" data-id="{html.escape(boxid)}">'
                f'<b>{html.escape(boxid)}</b><p>模擬抽獎盒</p>'
                f'<button class="draw-btn" onclick="choose(\'{html.escape(boxid)}\')"'
                f'{" disabled" if boxid in DISABLED else ""}>'
                f'{"已售完" if boxid in DISABLED else "抽獎"}</button></article>'
                for boxid in BOXES
            )
            records = '、'.join(f"{html.escape(i['account'])}:{html.escape(i['box'])}" for i in CLICKS[-6:]) or '尚未點擊'
        page = f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>多帳號測試站</title>
<style>body{{font-family:sans-serif;max-width:960px;margin:40px auto;background:#f5f7fa}}
.box-card{{display:inline-block;background:white;padding:20px;margin:12px;border:1px solid #ccd;border-radius:10px}}
button{{padding:10px 20px;cursor:pointer}}</style>
<h1>多帳號新盒測試站</h1><p>目前登入：<b>{html.escape(user)}</b>｜<a href="/logout">登出</a></p>
<p>每個 Chrome 應使用不同帳號登入：demo1 / demo2 / demo3 / demo4，密碼皆為 demo123（僅測試站）。</p>
<button onclick="fetch('/add',{{method:'POST'}}).then(()=>location.reload())">新增盒子（插在第 1 盒）</button>
<button onclick="fetch('/toggle',{{method:'POST'}}).then(()=>location.reload())">切換第 1 盒可點狀態</button>
<button onclick="fetch('/reset',{{method:'POST'}}).then(()=>location.reload())">重設測試</button>
<div>{cards}</div><h3>各帳號點擊紀錄</h3><p>{records}</p>
<script>function choose(id){{fetch('/click?id='+encodeURIComponent(id),{{method:'POST'}}).then(()=>location.reload())}}</script></html>'''
        return self.respond(page, 'text/html; charset=utf-8')

    def login_form(self, error: str = ''):
        page = f'''<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>測試登入頁</title>
<style>body{{font:18px sans-serif;max-width:500px;margin:70px auto}}input{{display:block;margin:12px 0;padding:10px;width:90%}}
button{{padding:10px 30px}}</style><h1>本機測試站登入</h1>
<p>demo1 / demo2 / demo3 / demo4；密碼：demo123</p><p style="color:red">{html.escape(error)}</p>
<form method="post" action="/login">
<label>帳號<input name="username" autocomplete="username"></label>
<label>密碼<input name="password" type="password" autocomplete="current-password"></label>
<button type="submit">登入</button></form></html>'''
        return self.respond(page, 'text/html; charset=utf-8')

    def do_POST(self):
        global COUNT
        path = urlsplit(self.path).path
        if path == '/login':
            raw_size = int(self.headers.get('Content-Length', '0'))
            if raw_size > 10240:
                return self.respond('Too large', 'text/plain', 413)
            form = parse_qs(self.rfile.read(raw_size).decode('utf-8', 'replace'))
            username = form.get('username', [''])[0]
            password = form.get('password', [''])[0]
            if DEMO_USERS.get(username) != password:
                return self.login_form('登入失敗')
            return self.redirect('/', cookie=f'demo_account={username}; Path=/; SameSite=Lax; HttpOnly')
        user = self.account()
        if not user:
            return self.respond('Please login', 'text/plain', 401)
        with LOCK:
            if path == '/add':
                COUNT += 1
                BOXES.insert(0, f'BOX-A{COUNT:03d}')
            elif path == '/toggle':
                if BOXES[0] in DISABLED:
                    DISABLED.remove(BOXES[0])
                else:
                    DISABLED.add(BOXES[0])
            elif path == '/reset':
                BOXES[:] = ['BOX-A002', 'BOX-A001']
                CLICKS.clear()
                DISABLED.clear()
                COUNT = 2
            elif path == '/click':
                chosen = parse_qs(urlsplit(self.path).query).get('id', [''])[0]
                if chosen in BOXES and chosen not in DISABLED:
                    CLICKS.append({'account': user, 'box': chosen})
            else:
                return self.respond('Not found', 'text/plain', 404)
        return self.respond('OK', 'text/plain')

    def redirect(self, location: str, cookie: str = ''):
        self.send_response(303)
        self.send_header('Location', location)
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def respond(self, body: str, content_type: str, status=200):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_):
        pass


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 8765), DemoHandler)
    print('多帳號測試站：http://127.0.0.1:8765；Ctrl+C 結束', flush=True)
    server.serve_forever()
