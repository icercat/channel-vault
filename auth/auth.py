#!/usr/bin/env python3
"""極簡登入服務:提供網頁登入畫面 + 簽章 cookie,供 nginx auth_request 驗證。僅用標準函式庫。"""
import hashlib, hmac, html, os, time, base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

USER = os.environ.get("WEB_USER", "admin")
PASSWORD = os.environ["WEB_PASSWORD"]
TTL = int(os.environ.get("SESSION_HOURS", "168")) * 3600
SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"
KEY = (os.environ.get("SESSION_SECRET") or hashlib.sha256(("channel-vault-gw|" + USER + "|" + PASSWORD).encode()).hexdigest()).encode()
COOKIE = "channel_vault_session"
UPSTREAM = "Basic " + base64.b64encode((USER + ":" + PASSWORD).encode()).decode()  # 轉給 app 內建 Basic Auth
FAILS = {}  # ip -> (count, first_ts)

def sign(exp: int) -> str:
    msg = str(exp).encode()
    sig = hmac.new(KEY, msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(msg + b"." + sig).decode()

def valid(token: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(token.encode())
        msg, sig = raw.rsplit(b".", 1)
        if not hmac.compare_digest(sig, hmac.new(KEY, msg, hashlib.sha256).digest()):
            return False
        return int(msg) > time.time()
    except Exception:
        return False

PAGE = """<!doctype html><html lang="zh-Hant" data-theme="trans"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Channel Vault 登入</title>
<style>
__THEMES__
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);color:var(--text);font:15px/1.6 ui-rounded,"Nunito","Segoe UI",system-ui,sans-serif;padding:16px;transition:background .2s,color .2s}
form{width:100%;max-width:380px;background:var(--surface);border:1px solid var(--border);border-radius:23px;box-shadow:var(--shadow);overflow:hidden}
.banner{height:96px;background:var(--hero);position:relative}.banner::after{content:'\\2727';color:var(--surface);font-size:40px;position:absolute;right:25px;top:16px;opacity:.75}
.body{padding:0 28px 28px}.logo{width:66px;height:66px;margin:-33px 0 12px;border:4px solid var(--surface);border-radius:50%;background:var(--surface-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:30px;position:relative}
h1{font-size:24px;line-height:1.3;margin:0 0 2px;letter-spacing:-.03em}p{margin:0 0 8px;color:var(--muted);font-size:13px}
label{display:block;font-size:13px;color:var(--muted);margin:14px 0 6px}
input{width:100%;padding:11px;font:inherit;border:1px solid var(--border);border-radius:12px;background:var(--bg);color:var(--text)}
input:focus{outline:2px solid var(--accent);outline-offset:1px;border-color:transparent}
button{width:100%;margin-top:22px;padding:11px 17px;font:inherit;font-weight:600;border:0;border-radius:14px;background:var(--accent);color:var(--accent-text);cursor:pointer;transition:transform .15s}button:hover{transform:translateY(-1px)}
.err{color:var(--live);font-size:13px;margin-top:14px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
<script>
try{var p=JSON.parse(localStorage.getItem("channel-vault-theme")),r=document.documentElement;
if(p&&p.theme){r.dataset.theme=p.theme;
[["accent","--accent"],["background","--bg"],["surface","--surface"]].forEach(function(k){if(p[k[0]])r.style.setProperty(k[1],p[k[0]])});
if(p.accent){var c=p.accent.slice(1).match(/../g).map(function(v){return parseInt(v,16)});
r.style.setProperty("--accent-text",c[0]*.299+c[1]*.587+c[2]*.114>160?"#26313b":"#ffffff")}}}catch(e){}
</script></head><body>
<form method="post" action="/_login"><div class="banner"></div><div class="body">
<div class="logo" aria-hidden="true">\u2601</div>
<h1>Channel Vault</h1><p>把喜歡的時刻，悄悄收藏 \u2661</p>
<label for="u">帳號</label><input id="u" name="username" autocomplete="username" autofocus required>
<label for="p">密碼</label><input id="p" name="password" type="password" autocomplete="current-password" required>
<button type="submit">登入</button>__ERR__
</div></form></body></html>"""
PAGE = PAGE.replace("__THEMES__", r''':root,[data-theme="trans"]{color-scheme:light;--bg:#fff7fb;--surface:#e4f4fc;--surface-soft:#d7ecf7;--accent:#de7eaf;--accent-text:#fff;--text:#44344c;--muted:#8a748f;--border:#efdfeb;--secondary:#69bbd7;--hero:linear-gradient(120deg,#80c8e7,#f4b3cc 47%,#fff5fb 54%,#f4b3cc 65%,#80c8e7);--shadow:0 10px 30px #975f8410;--live:#ae456e;--success:#418e7a}
[data-theme="yt"]{color-scheme:light;--bg:#fff8f8;--surface:#fff;--surface-soft:#ffefef;--accent:#e83e51;--accent-text:#fff;--text:#352a30;--muted:#8c7279;--border:#f1dfe2;--secondary:#fa9394;--hero:linear-gradient(120deg,#ffd5d9,#ff9ea7,#ffe6df);--shadow:0 10px 30px #d4334210;--live:#d42b43;--success:#3a8872}
[data-theme="twitch"]{color-scheme:dark;--bg:#171020;--surface:#231932;--surface-soft:#30203f;--accent:#b294ff;--accent-text:#231238;--text:#f4eaff;--muted:#b7a2c9;--border:#402c52;--secondary:#e29fe8;--hero:linear-gradient(120deg,#5835a0,#935dcb,#482a71);--shadow:0 10px 30px #08020e30;--live:#ffa4dc;--success:#9bd6bd}
[data-theme="dark"]{color-scheme:dark;--bg:#11131b;--surface:#1d202b;--surface-soft:#282c39;--accent:#aab6df;--accent-text:#192037;--text:#f0f1fa;--muted:#a3a8bd;--border:#333747;--secondary:#b8aadf;--hero:linear-gradient(120deg,#353f5a,#64678d,#353c53);--shadow:0 10px 30px #0002;--live:#efa3bd;--success:#94cfb4}
[data-theme="light"]{color-scheme:light;--bg:#f7f8fc;--surface:#fff;--surface-soft:#edf1fa;--accent:#658ed4;--accent-text:#fff;--text:#354056;--muted:#7d8aa4;--border:#e2e7f2;--secondary:#90adc8;--hero:linear-gradient(120deg,#c5d8f8,#bde5ec,#e6e9ff);--shadow:0 10px 30px #4c6cb110;--live:#b65783;--success:#418b77}
[data-theme="neon"]{color-scheme:dark;--bg:#0c121b;--surface:#141f2c;--surface-soft:#1b3038;--accent:#6ee7d0;--accent-text:#0b2925;--text:#edf4fa;--muted:#94a7ba;--border:#253344;--secondary:#69b8d5;--hero:linear-gradient(120deg,#16414a,#37776c,#203142);--shadow:0 10px 30px #0002;--live:#ffadc4;--success:#6ee7d0}
[data-theme="neon-pink"]{color-scheme:dark;--bg:#1b1021;--surface:#2b1931;--surface-soft:#40213f;--accent:#ff8edb;--accent-text:#3f1238;--text:#fff0fb;--muted:#c19bbb;--border:#52314f;--secondary:#c09aff;--hero:linear-gradient(120deg,#783672,#ca63a9,#613a83);--shadow:0 10px 30px #0002;--live:#ffb0d7;--success:#b5dfc5}''')

class H(BaseHTTPRequestHandler):
    server_version = "gw"
    def log_message(self, *a): pass
    def ip(self): return self.headers.get("X-Real-IP") or self.client_address[0]
    def cookie(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == COOKIE: return v
        return ""
    def send(self, code, body=b"", headers=()):
        self.send_response(code)
        for k, v in headers: self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def page(self, err="", code=200):
        e = f'<div class="err">{html.escape(err)}</div>' if err else ""
        self.send(code, PAGE.replace("__ERR__", e).encode(), [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
    def do_GET(self):
        if self.path.startswith("/check"):
            if not valid(self.cookie()): return self.send(401)
            return self.send(200, headers=[("X-Upstream-Auth", UPSTREAM)])
        if self.path.startswith("/logout"):
            return self.send(302, headers=[("Location", "/_login"), ("Set-Cookie", f"{COOKIE}=; Path=/; Max-Age=0")])
        if valid(self.cookie()):
            return self.send(302, headers=[("Location", "/")])
        self.page()
    def do_POST(self):
        ip = self.ip(); n, t = FAILS.get(ip, (0, time.time()))
        if time.time() - t > 300: n, t = 0, time.time()
        if n >= 8: return self.page("嘗試次數過多,請 5 分鐘後再試", 429)
        data = parse_qs(self.rfile.read(min(int(self.headers.get("Content-Length", 0)), 4096)).decode())
        u = data.get("username", [""])[0]; p = data.get("password", [""])[0]
        ok = hmac.compare_digest(u.encode(), USER.encode()) & hmac.compare_digest(p.encode(), PASSWORD.encode())
        if not ok:
            FAILS[ip] = (n + 1, t)
            return self.page("帳號或密碼錯誤", 401)
        FAILS.pop(ip, None)
        ck = f"{COOKIE}={sign(int(time.time()) + TTL)}; Path=/; Max-Age={TTL}; HttpOnly; SameSite=Lax" + ("; Secure" if (SECURE or self.headers.get("X-Forwarded-Proto") == "https") else "")
        self.send(302, headers=[("Location", "/"), ("Set-Cookie", ck)])

if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
