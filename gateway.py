import os
import re
import time
import json
import hashlib
import secrets
import sqlite3
import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
from urllib.parse import urlencode
from html import escape
from email.message import EmailMessage
import smtplib
import ssl

import requests
from flask import Flask, request, jsonify, g, Response, redirect, render_template_string, session, has_request_context
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.environ.get('LLM_PLATFORM_HOME', '/opt/llm-platform')
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_PATH = os.path.join(DATA_DIR, 'platform.db')
MODEL_NAME = os.environ.get('MODEL_NAME', 'qwen2.5-coder-14b-ms:latest')
OLLAMA_BASE_URL = os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', 'admin@ypvps.com')
SITE_NAME = os.environ.get('SITE_NAME', 'LLM Platform')
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
EPAY_API_URL = os.environ.get('EPAY_API_URL', '').rstrip('/')
EPAY_PID = os.environ.get('EPAY_PID', '')
EPAY_KEY = os.environ.get('EPAY_KEY', '')
DOMAIN = os.environ.get('DOMAIN', 'newapi.ypvps.com')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', f'https://{DOMAIN}')

PLAN_CONFIG = {
    'free': {'name': '免费版', 'price': 0, 'daily_tokens': 10000, 'rate_limit': 20, 'days': 3650, 'description': '免费体验'},
    'starter': {'name': '入门版', 'price': 1, 'daily_tokens': 200000, 'rate_limit': 60, 'days': 30, 'description': '个人轻量调用'},
    'pro': {'name': '专业版', 'price': 2, 'daily_tokens': 1000000, 'rate_limit': 180, 'days': 30, 'description': '生产项目推荐'},
    'enterprise': {'name': '企业版', 'price': 3, 'daily_tokens': 5000000, 'rate_limit': 600, 'days': 30, 'description': '团队与高并发'},
    'yearly': {'name': '年付专业版', 'price': 9, 'daily_tokens': 1500000, 'rate_limit': 240, 'days': 365, 'description': '年付优惠'},
}

# 套餐默认值可以用环境变量覆盖（容器盘是临时的，每次重新部署都要重建数据库，
# 写在环境变量里就不必每次进后台手工改价格）。
#
#   PLAN_<ID>_<字段>         例：PLAN_FREE_PRICE=0  PLAN_STARTER_PRICE=1  PLAN_PRO_PRICE=2
#   PLAN_<编号>_<字段>       例：PLAN_1_ID=free  PLAN_1_NAME=免费版  PLAN_1_PRICE=0
#
# 可用字段后缀：ID / NAME / PRICE / TOKENS（每日额度）/ RATE（每分钟次数）/ DAYS / DESC
PLAN_ENV_KEYS = {
    'NAME': 'name', 'PRICE': 'price', 'TOKENS': 'daily_tokens', 'DAILY_TOKENS': 'daily_tokens',
    'RATE': 'rate_limit', 'RATE_LIMIT': 'rate_limit', 'DAYS': 'days',
    'DESC': 'description', 'DESCRIPTION': 'description',
}

def _plan_env_suffixes():
    out = {}
    for suffix, key in PLAN_ENV_KEYS.items():
        out.setdefault(key, []).append(suffix)
    return out

def _plan_cast(key, raw):
    raw = (raw or '').strip()
    if raw == '':
        return None
    if key in ('price',):
        try: return float(raw)
        except ValueError: return None
    if key in ('daily_tokens', 'rate_limit', 'days'):
        try: return int(float(raw))
        except ValueError: return None
    return raw

def plan_env_overrides():
    """只收集环境变量里**显式写了**的套餐字段：{plan_id: {列名: 值}}。"""
    ov = {}
    # 1) 按套餐 id：PLAN_FREE_PRICE / PLAN_PRO_NAME ...
    for pid in PLAN_CONFIG:
        prefix = 'PLAN_%s_' % re.sub(r'[^A-Za-z0-9]', '_', pid).upper()
        for key, suffixes in _plan_env_suffixes().items():
            for sfx in suffixes:
                val = _plan_cast(key, os.environ.get(prefix + sfx))
                if val is not None:
                    ov.setdefault(pid, {})[key] = val
                    break
    # 2) 按编号：PLAN_1_ID/NAME/PRICE/...（可定义全新套餐）
    for i in range(1, 21):
        pid = (os.environ.get('PLAN_%d_ID' % i) or '').strip()
        if not pid or not re.match(r'^[a-zA-Z0-9_-]{2,32}$', pid):
            continue
        item = ov.setdefault(pid, {})
        for key, suffixes in _plan_env_suffixes().items():
            for sfx in suffixes:
                val = _plan_cast(key, os.environ.get('PLAN_%d_%s' % (i, sfx)))
                if val is not None:
                    item[key] = val
                    break
    return ov

def plan_config_from_env():
    """内置套餐默认值 + 环境变量覆盖后的完整套餐表（用于首页/下单/播种）。"""
    plans = {pid: dict(cfg) for pid, cfg in PLAN_CONFIG.items()}
    for pid, fields in plan_env_overrides().items():
        cfg = plans.get(pid) or {'name': pid, 'price': 0, 'daily_tokens': 0, 'rate_limit': 60, 'days': 30, 'description': ''}
        cfg = dict(cfg)
        cfg.update(fields)
        plans[pid] = cfg
    return plans

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=False)
CORS(app)

class KeyPathMiddleware:
    """把 /k/<api_key>/v1/... 改写为 /v1/...，并把 key 注入 X-API-Key。

    背景：某些 PaaS / 反代链路（例如 Cloudflare + 平台 ingress 的自定义域名）
    会丢弃或改写 Authorization 头，导致标准 OpenAI 客户端（只会设置
    Authorization: Bearer <key>）永远 401。

    把 key 放进 URL 路径即可绕开：
        https://host/k/sk-xxxx/v1/chat/completions
    对客户端来说只是个普通的 base_url，无需自定义头支持。
    """
    PREFIX = '/k/'
    # 缺少 /v1 前缀时自动补全。客户端（WorkBuddy / OpenAI SDK 等）会把 base 地址
    # 规范成 ".../chat/completions"，所以 base 写 http://host/k/<key> 时路径会变成
    # /k/<key>/chat/completions，需要在这里补回 /v1。
    _V1_ENDPOINTS = ('chat/completions','completions','embeddings','models','responses','messages','moderations','images/','audio/')

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO') or ''
        if path.startswith(self.PREFIX):
            rest = path[len(self.PREFIX):]
            key, sep, tail = rest.partition('/')
            if key:
                environ['HTTP_X_API_KEY'] = key
                if not sep or not tail:
                    environ['PATH_INFO'] = '/k'
                elif tail == 'v1' or tail.startswith('v1/'):
                    environ['PATH_INFO'] = '/' + tail
                elif any(tail.startswith(p) for p in self._V1_ENDPOINTS):
                    environ['PATH_INFO'] = '/v1/' + tail
                else:
                    environ['PATH_INFO'] = '/' + tail
        return self.wsgi_app(environ, start_response)

app.wsgi_app = KeyPathMiddleware(app.wsgi_app)

@app.after_request
def no_cache_dynamic_pages(resp):
    if request.path in ['/', '/login', '/register', '/logout', '/dashboard', '/playground', '/admin'] or request.path.startswith('/ticket/') or request.path.startswith('/pay/'):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{site_name}} · OpenAI 兼容大模型网关</title><style>
:root{--bg:#f4f5f9;--card:#fff;--line:#e9ebf2;--text:#1f2329;--muted:#8b93a5;--primary:#5b5bd6;--primary2:#8257e6;--ok:#12b76a;--bad:#f04438;--warn:#f79009;--radius:14px}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;font-size:14px;line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--primary);text-decoration:none}a:hover{opacity:.85}
h1,h2,h3,h4{margin:0 0 12px;line-height:1.3}h1{font-size:30px}h2{font-size:21px}h3{font-size:16px}h4{font-size:14px}
p{margin:0 0 10px}
/* top nav */
.topbar{position:sticky;top:0;z-index:50;background:rgba(255,255,255,.9);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}
.topbar-inner{max-width:1240px;margin:0 auto;padding:0 20px;height:62px;display:flex;align-items:center;gap:18px}
.brand{display:flex;align-items:center;gap:10px;color:var(--text);font-weight:700;flex:none}
.brand-logo{width:34px;height:34px;border-radius:10px;background:linear-gradient(135deg,var(--primary),var(--primary2));color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;letter-spacing:.5px;box-shadow:0 4px 12px rgba(91,91,214,.35)}
.brand-text{display:flex;flex-direction:column;line-height:1.1}
.brand-text small{color:var(--muted);font-weight:400;font-size:11px}
.menu{display:flex;align-items:center;gap:2px;flex:1;flex-wrap:wrap}
.menu a{color:#4b5563;padding:7px 12px;border-radius:9px;font-size:13.5px}
.menu a:hover{background:#f1f2f8;color:var(--text);opacity:1}
.user-chip{display:flex;align-items:center;gap:10px;font-size:13px;flex:none}
.avatar{width:28px;height:28px;border-radius:50%;background:linear-gradient(135deg,var(--primary2),var(--primary));color:#fff;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700}
.user-chip a{color:var(--muted)}
/* layout */
.wrap{max-width:1240px;margin:0 auto;padding:22px 20px 8px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.two{display:grid;grid-template-columns:1.05fr .95fr;gap:16px;align-items:start}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:20px;box-shadow:0 1px 2px rgba(16,24,40,.04);margin-bottom:16px}
.card>h3:first-child,.card>h2:first-child{margin-top:0}
.muted{color:var(--muted)}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
/* hero */
.hero{position:relative;overflow:hidden;background:linear-gradient(135deg,#4f46e5 0%,#6d5ce7 45%,#8b5cf6 100%);color:#fff;border-radius:20px;padding:40px 36px 32px;box-shadow:0 18px 40px rgba(79,70,229,.28);margin-bottom:20px}
.hero:after{content:"";position:absolute;right:-90px;top:-120px;width:340px;height:340px;border-radius:50%;background:rgba(255,255,255,.14)}
.hero:before{content:"";position:absolute;right:70px;bottom:-150px;width:240px;height:240px;border-radius:50%;background:rgba(255,255,255,.1)}
.hero h1{font-size:32px;margin-bottom:10px;position:relative;z-index:1}
.hero p{opacity:.92;max-width:720px;position:relative;z-index:1}
.hero-tag{display:inline-block;background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.28);border-radius:999px;padding:4px 12px;font-size:12px;margin-bottom:14px;position:relative;z-index:1}
.hero-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;position:relative;z-index:1}
.hero-stats{display:flex;gap:28px;flex-wrap:wrap;margin-top:26px;position:relative;z-index:1}
.hero-stats div{display:flex;flex-direction:column}
.hero-stats b{font-size:23px;font-weight:800}
.hero-stats span{opacity:.85;font-size:12.5px}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:0;border-radius:10px;background:var(--primary);color:#fff;padding:9px 16px;text-decoration:none;cursor:pointer;font-size:13.5px;font-weight:500;transition:.15s}
.btn:hover{filter:brightness(1.06)}
.btn-light{background:#fff;color:#4f46e5;font-weight:600}
.btn-ghost{background:rgba(255,255,255,.14);color:#fff;border:1px solid rgba(255,255,255,.4)}
.btn2{background:#fff;color:#4b5563;border:1px solid var(--line)}
.btn2:hover{border-color:#c9cbd8;background:#fafbff}
.btn-danger{background:#fff;color:var(--bad);border:1px solid #ffd7d3}
.btn-danger:hover{background:#fff5f4}
.btn-sm{padding:6px 12px;font-size:13px}
.input{width:100%;border:1px solid var(--line);border-radius:10px;padding:9px 11px;margin:6px 0 10px;font-size:13.5px;background:#fff;color:var(--text);transition:.15s}
.input:focus{outline:0;border-color:#b9b6f0;box-shadow:0 0 0 3px rgba(91,91,214,.12)}
.mini{width:130px}
select.input{cursor:pointer}
label{font-size:13px;color:#4b5563;font-weight:500}
/* tables */
table{width:100%;border-collapse:separate;border-spacing:0;background:#fff}
th{background:#fafbfd;color:var(--muted);font-size:12px;font-weight:600;text-transform:none;letter-spacing:.02em;padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
td{padding:11px 12px;border-bottom:1px solid #f2f3f8;text-align:left;vertical-align:top}
tr:last-child td{border-bottom:0}
.card table{margin-top:4px}
/* tags */
.pill{display:inline-block;padding:3px 9px;border-radius:999px;background:#eef0ff;color:#4f46e5;font-size:12px;font-weight:500}
.pill-green{background:#e8f8ef;color:#0d9f5f}
.pill-gray{background:#f2f3f8;color:#6b7280}
.price{font-size:30px;font-weight:800;color:#1f2329;letter-spacing:-.5px}
.price small{font-size:15px;font-weight:600;color:var(--muted);margin-right:2px}
.plan-card{position:relative;display:flex;flex-direction:column}
.plan-card.featured{border-color:#c7c4f5;box-shadow:0 10px 26px rgba(91,91,214,.13)}
.plan-card ul{list-style:none;padding:0;margin:12px 0 16px}
.plan-card li{padding:5px 0;color:#4b5563;font-size:13.5px}
.plan-card li:before{content:"✓";color:var(--ok);font-weight:700;margin-right:8px}
.stat-card{display:flex;flex-direction:column;gap:2px}
.stat-card b{font-size:22px;font-weight:800}
.section-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin:26px 0 12px}
.section-head h2{margin:0}
/* code */
pre{background:#0f1222;color:#d7e3ff;border-radius:12px;padding:14px 16px;overflow:auto;font-size:12.5px;line-height:1.7;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{background:#f2f3f8;border-radius:6px;padding:1px 6px;font-size:12.5px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre code{background:none;padding:0}
.key-line{word-break:break-all;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.curl-box{max-height:280px}
/* playground */
.playground{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(360px,.85fr);gap:16px;align-items:start}
.chat-toolbar{display:grid;grid-template-columns:minmax(220px,1fr) auto;gap:10px;align-items:end}
.chat-prompt{min-height:130px;max-height:240px;resize:vertical;margin-top:10px;width:100%;border:1px solid var(--line);border-radius:10px;padding:10px;font-family:inherit}
.chat-result{min-height:220px;max-height:520px}
.result-card{background:#fafbff;border:1px solid var(--line);border-radius:12px;padding:14px;margin-top:10px}
.result-meta{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
.result-meta span{background:#eef0ff;color:#4f46e5;border-radius:999px;padding:3px 9px;font-size:12px}
.assistant-answer{white-space:pre-wrap;line-height:1.75;font-size:14.5px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px;color:#1f2329}
.raw-json summary{cursor:pointer;color:var(--primary);margin-top:12px}
.raw-json pre{max-height:360px}
/* misc */
.inline-form{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin:0}
.inline-form .input{width:auto;margin:0}
.model-search{max-width:320px}
.foot{max-width:1240px;margin:20px auto 40px;padding:18px 20px 0;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
.copy-box{background:#fafbff;border:1px dashed #d8dae8;border-radius:10px;padding:10px 12px;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;word-break:break-all}
/* auth */
.auth{max-width:440px;margin:34px auto 10px}
.auth-head{text-align:center;margin-bottom:16px}
.auth-head h2{font-size:23px;margin-bottom:6px}
.auth-head p{color:var(--muted);font-size:13.5px}
.auth-card{background:#fff;border:1px solid var(--line);border-radius:18px;padding:26px 24px;box-shadow:0 12px 32px rgba(16,24,40,.07)}
.auth-switch{text-align:center;margin-top:14px;color:var(--muted);font-size:13.5px}
.auth-card .btn{width:100%;padding:11px 16px;font-size:14px}
@media(max-width:900px){.two,.playground,.chat-toolbar{grid-template-columns:1fr}.hero{padding:28px 22px}.hero h1{font-size:25px}.menu{gap:0}.brand-text small{display:none}}
</style></head><body>{{nav|safe}}<main class="wrap">{{body|safe}}</main><footer class="foot"><span>{{site_name}} · OpenAI 兼容大模型网关</span><span>Chat Completions / Responses / Anthropic Messages</span></footer></body></html>'''

def h(v):
    return escape('' if v is None else str(v), quote=True)

def has_request_context_safe():
    try: return has_request_context()
    except Exception: return False


def page(body):
    u=current_user() if has_request_context_safe() else None
    brand=('<a class="brand" href="/"><span class="brand-logo">LLM</span>'
           f'<span class="brand-text">{h(SITE_NAME)}<small>OpenAI 兼容大模型网关</small></span></a>')
    if u:
        admin_links='<a href="/admin">管理后台</a>' if u['is_admin'] else ''
        menu=(f'<nav class="menu"><a href="/">首页</a><a href="/dashboard">用户控制台</a><a href="/playground">聊天测试</a>'
              f'{admin_links}<a href="/v1/models">模型列表</a><a href="/health">服务状态</a></nav>')
        chip=(f'<div class="user-chip"><span class="avatar">{h((u["username"] or "U")[:1].upper())}</span>'
              f'<b>{h(u["username"])}</b><a href="/logout">退出</a></div>')
    else:
        menu='<nav class="menu"><a href="/">首页</a><a href="/#pricing">套餐</a><a href="/#models">模型广场</a><a href="/v1/models">模型列表</a><a href="/health">服务状态</a></nav>'
        chip='<div class="user-chip"><a class="btn btn2 btn-sm" href="/login">登录</a><a class="btn btn-sm" href="/register">免费注册</a></div>'
    nav=f'<header class="topbar"><div class="topbar-inner">{brand}{menu}{chip}</div></header>'
    return render_template_string(HTML, body=body, nav=nav, site_name=h(SITE_NAME))

def db():
    if not hasattr(g, 'db'):
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH, timeout=20)
        g.db.row_factory = sqlite3.Row
        try:
            g.db.execute('PRAGMA busy_timeout=20000')
            g.db.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error:
            pass
    return g.db

@app.teardown_appcontext
def close_db(exc):
    con = getattr(g, 'db', None)
    if con: con.close()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute('PRAGMA busy_timeout=30000')
    except sqlite3.Error:
        pass
    c = con.cursor()
    c.executescript('''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT UNIQUE NOT NULL,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,plan TEXT DEFAULT 'free',plan_expires_at TEXT,tokens_used_today INTEGER DEFAULT 0,tokens_reset_date TEXT,balance REAL DEFAULT 0,invite_code TEXT UNIQUE,invited_by INTEGER,is_admin INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,last_login TEXT,daily_token_limit INTEGER,custom_rate_limit INTEGER);
CREATE TABLE IF NOT EXISTS api_keys(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,key_hash TEXT UNIQUE NOT NULL,key_prefix TEXT NOT NULL,name TEXT DEFAULT '',is_active INTEGER DEFAULT 1,rate_limit INTEGER DEFAULT 60,allowed_ips TEXT DEFAULT '',quota_daily INTEGER,created_at TEXT DEFAULT CURRENT_TIMESTAMP,last_used_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,order_no TEXT UNIQUE NOT NULL,user_id INTEGER NOT NULL,plan_id TEXT NOT NULL,amount REAL NOT NULL,payment_method TEXT DEFAULT '',trade_no TEXT DEFAULT '',status TEXT DEFAULT 'pending',paid_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS usage_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,api_key_id INTEGER,tokens_input INTEGER DEFAULT 0,tokens_output INTEGER DEFAULT 0,model TEXT DEFAULT '',endpoint TEXT DEFAULT '',cost REAL DEFAULT 0,duration_ms INTEGER DEFAULT 0,ip_address TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,content TEXT DEFAULT '',category TEXT DEFAULT 'general',priority TEXT DEFAULT 'normal',status TEXT DEFAULT 'open',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT,FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS ticket_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id INTEGER NOT NULL,user_id INTEGER,author_role TEXT DEFAULT 'user',message TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(ticket_id) REFERENCES tickets(id),FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,name TEXT NOT NULL,price REAL DEFAULT 0,daily_tokens INTEGER DEFAULT 0,rate_limit INTEGER DEFAULT 60,days INTEGER DEFAULT 30,is_active INTEGER DEFAULT 1,sort_order INTEGER DEFAULT 100,description TEXT DEFAULT '',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS managed_projects(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,description TEXT DEFAULT '',base_url TEXT DEFAULT '',status TEXT DEFAULT 'active',sort_order INTEGER DEFAULT 100,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS model_providers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,provider_type TEXT DEFAULT 'openai',base_url TEXT DEFAULT '',api_key TEXT DEFAULT '',model_id TEXT NOT NULL,display_name TEXT DEFAULT '',is_default INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1,sort_order INTEGER DEFAULT 100,timeout_seconds INTEGER DEFAULT 300,endpoint_type TEXT DEFAULT 'chat_completions',modalities TEXT DEFAULT '["text"]',supports_stream INTEGER DEFAULT 1,supports_tools INTEGER DEFAULT 0,supports_vision INTEGER DEFAULT 0,supports_video INTEGER DEFAULT 0,max_input_tokens INTEGER,max_output_tokens INTEGER,extra_config TEXT DEFAULT '{}',created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS model_stats(provider_id INTEGER PRIMARY KEY,ok_count INTEGER DEFAULT 0,fail_count INTEGER DEFAULT 0,avg_ms REAL DEFAULT 0,last_ms REAL DEFAULT 0,fail_streak INTEGER DEFAULT 0,last_error TEXT DEFAULT '',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,order_id INTEGER NOT NULL,invoice_no TEXT UNIQUE NOT NULL,company_name TEXT NOT NULL,tax_id TEXT,amount REAL NOT NULL,status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT DEFAULT '',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS email_activations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,email TEXT NOT NULL,token TEXT UNIQUE NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id));
''')
    for ddl in ['ALTER TABLE users ADD COLUMN daily_token_limit INTEGER','ALTER TABLE users ADD COLUMN custom_rate_limit INTEGER','ALTER TABLE users ADD COLUMN max_api_keys INTEGER DEFAULT 5','ALTER TABLE users ADD COLUMN email_verified_at TEXT','ALTER TABLE api_keys ADD COLUMN quota_daily INTEGER','ALTER TABLE api_keys ADD COLUMN key_plain TEXT']:
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    for ddl in [
        "ALTER TABLE model_providers ADD COLUMN endpoint_type TEXT DEFAULT 'chat_completions'",
        "ALTER TABLE model_providers ADD COLUMN modalities TEXT DEFAULT '[\"text\"]'",
        'ALTER TABLE model_providers ADD COLUMN supports_stream INTEGER DEFAULT 1',
        'ALTER TABLE model_providers ADD COLUMN supports_tools INTEGER DEFAULT 0',
        'ALTER TABLE model_providers ADD COLUMN supports_vision INTEGER DEFAULT 0',
        'ALTER TABLE model_providers ADD COLUMN supports_video INTEGER DEFAULT 0',
        'ALTER TABLE model_providers ADD COLUMN max_input_tokens INTEGER',
        'ALTER TABLE model_providers ADD COLUMN max_output_tokens INTEGER',
        "ALTER TABLE model_providers ADD COLUMN extra_config TEXT DEFAULT '{}'"
    ]:
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    for k,v in {'epay_api_url': EPAY_API_URL, 'epay_pid': EPAY_PID, 'epay_key': EPAY_KEY, 'domain': DOMAIN, 'public_base_url': PUBLIC_BASE_URL, 'payment_enabled': '0' if not (EPAY_API_URL and EPAY_PID and EPAY_KEY) else '1', 'smtp_enabled': os.environ.get('SMTP_ENABLED','0'), 'smtp_host': os.environ.get('SMTP_HOST',''), 'smtp_port': os.environ.get('SMTP_PORT','587'), 'smtp_username': os.environ.get('SMTP_USERNAME',''), 'smtp_password': os.environ.get('SMTP_PASSWORD',''), 'smtp_encryption': os.environ.get('SMTP_ENCRYPTION','tls'), 'smtp_from_email': os.environ.get('SMTP_FROM_EMAIL',''), 'smtp_from_name': os.environ.get('SMTP_FROM_NAME','LLM Platform')}.items():
        c.execute('INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)', (k, v or ''))
    _plans=plan_config_from_env(); _pov=plan_env_overrides()
    for idx,(pid,cfg) in enumerate(_plans.items()):
        c.execute('INSERT OR IGNORE INTO plans(id,name,price,daily_tokens,rate_limit,days,is_active,sort_order,description) VALUES(?,?,?,?,?,?,?,?,?)', (pid,cfg['name'],cfg['price'],cfg['daily_tokens'],cfg['rate_limit'],cfg['days'],1,idx*10,cfg.get('description','')))
        _f={k:v for k,v in (_pov.get(pid) or {}).items() if k in ('name','price','daily_tokens','rate_limit','days','description')}
        if _f:
            # 环境变量里显式给出的套餐字段以环境变量为准（覆盖容器内旧库里的值）
            c.execute('UPDATE plans SET %s,updated_at=CURRENT_TIMESTAMP WHERE id=?' % ','.join('%s=?'%k for k in _f), tuple(_f.values())+(pid,))
    c.execute('INSERT OR IGNORE INTO managed_projects(name,slug,description,base_url,status,sort_order) VALUES(?,?,?,?,?,?)', ('默认 LLM 网关','llm-gateway','OpenAI 兼容接口与模型中转平台',PUBLIC_BASE_URL,'active',10))
    c.execute('DELETE FROM model_providers WHERE id NOT IN (SELECT MIN(id) FROM model_providers GROUP BY provider_type, model_id, base_url)')
    if not c.execute('SELECT 1 FROM model_providers WHERE provider_type=? AND model_id=? AND base_url=?',('ollama',MODEL_NAME,OLLAMA_BASE_URL)).fetchone():
        c.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order) VALUES(?,?,?,?,?,?,?,?,?)', ('本地 Ollama','ollama',OLLAMA_BASE_URL,'',MODEL_NAME,MODEL_NAME,1,1,10))
    c.execute('INSERT OR IGNORE INTO users(username,email,password_hash,is_admin,invite_code,tokens_reset_date) VALUES(?,?,?,?,?,?)', (ADMIN_USERNAME,ADMIN_EMAIL,generate_password_hash(ADMIN_PASSWORD),1,secrets.token_hex(6).upper(),dt.date.today().isoformat()))
    try:
        # 数据库中已存在同名 admin 时，把邮箱同步成环境变量里配置的地址
        c.execute('UPDATE users SET email=? WHERE username=? AND email<>?', (ADMIN_EMAIL, ADMIN_USERNAME, ADMIN_EMAIL))
    except sqlite3.IntegrityError:
        pass
    seed_master_api_key(c)
    con.commit(); con.close()
    start_upstream_seed_thread()

def seed_master_api_key(c):
    """把 MASTER_API_KEY 环境变量播种成一把"永远有效"的 API Key（归属 admin）。

    用途：PaaS/沙箱的容器盘是临时的，重新部署会重建 SQLite，后台手工创建的
    用户和 API Key 全部失效。把 Key 写进环境变量后每次启动自动重建同一把，
    客户端（WorkBuddy、脚本、第三方 SDK）配置就不必跟着改。
    不设置该变量时本函数不做任何事。
    """
    raw_keys=[]
    for env_name in ('MASTER_API_KEY','STATIC_API_KEY'):
        v=(os.environ.get(env_name) or '').strip()
        if v: raw_keys.append((v,env_name+' (env)'))
    # EXTRA_API_KEYS=sk-a,sk-b —— 让后台手工创建的 Key 也能在重新部署后自动重建
    for i,part in enumerate(re.split(r'[,\n;]+', os.environ.get('EXTRA_API_KEYS') or ''),1):
        v=part.strip()
        if v: raw_keys.append((v,'EXTRA_API_KEYS #%d (env)'%i))
    if not raw_keys: return
    admin=c.execute('SELECT id FROM users WHERE is_admin=1 ORDER BY id ASC LIMIT 1').fetchone()
    if not admin: return
    uid=admin['id']
    for raw,label in raw_keys:
        key_hash=hashlib.sha256(raw.encode()).hexdigest()
        row=c.execute('SELECT id FROM api_keys WHERE key_hash=?',(key_hash,)).fetchone()
        if row:
            c.execute('UPDATE api_keys SET is_active=1,key_plain=?,user_id=? WHERE id=?',(raw,uid,row['id']))
        else:
            c.execute('INSERT INTO api_keys(user_id,key_hash,key_prefix,key_plain,name,rate_limit) VALUES(?,?,?,?,?,?)',(uid,key_hash,raw[:10],raw,label,100000))
    print('[init] %d env API key(s) seeded as always-valid admin keys' % len(raw_keys))

def _upstream_env_present():
    if (os.environ.get('UPSTREAM_BASE_URL') or '').strip(): return True
    if (os.environ.get('OPENAI_BASE_URL') or '').strip(): return True
    for i in range(1,51):
        if (os.environ.get('UPSTREAM_%d_BASE_URL'%i) or '').strip(): return True
    return False

_SEED_STATE={'started':False,'lock':os.path.join(DATA_DIR,'.upstream-seed.lock')}

def start_upstream_seed_thread():
    """在后台线程里播种上游模型，绝不阻塞启动。

    背景：UPSTREAM_n_FETCH_MODELS=1 时需要对每个上游发一次 HTTP 请求。
    16 个上游串行、每个超时十几秒的话，最坏会拖住启动好几分钟，
    PaaS 的健康检查等不到端口监听就直接判定部署失败（外部 502）。
    所以这里把整个播种过程挪到 daemon 线程：端口先监听，模型慢慢导入。

    幂等 + 跨进程互斥：wsgi.py / main.py 可能各调用一次 init_db()，
    gunicorn 又可能起多个 worker，这里用进程内标志 + 锁文件保证
    同一时间只有一个进程真正去拉上游，避免把上游打爆 / 把 SQLite 锁死。
    """
    if _SEED_STATE['started']:
        return
    _SEED_STATE['started']=True
    if not _upstream_env_present():
        print('[init] no UPSTREAM_* env found, skip seeding')
        return
    def worker():
        lock_path=_SEED_STATE['lock']
        try:
            if os.path.exists(lock_path) and time.time()-os.path.getmtime(lock_path)>600:
                os.remove(lock_path)          # 陈旧锁（上次进程被强杀）自动清理
            fd=os.open(lock_path, os.O_CREAT|os.O_EXCL|os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
        except FileExistsError:
            print('[init] another worker is already seeding upstream models, skip')
            return
        except Exception as e:
            print('[init] seed lock unavailable (%s), proceeding anyway' % e)
        try:
            con=sqlite3.connect(DB_PATH, timeout=30)
            con.row_factory=sqlite3.Row
            try:
                con.execute('PRAGMA busy_timeout=30000')
                seed_upstream_from_env(con); con.commit()
                print('[init] upstream seeding finished in background')
            finally:
                con.close()
            # 重新部署会清空 SQLite（含速度统计），开启后每次启动自动重测一遍，
            # auto 才能立刻按"谁快"排序；不影响端口监听（仍在后台线程里）。
            if (os.environ.get('PROBE_ON_START') or '').strip().lower() in ('1','true','yes','on'):
                _ok,_info=start_probe_models(); print('[init] PROBE_ON_START:', _info)
        except Exception as e:
            print('[init] upstream seeding failed (ignored):', e)
        finally:
            try: os.remove(lock_path)
            except OSError: pass
    threading.Thread(target=worker, name='upstream-seed', daemon=True).start()
    print('[init] upstream seeding started in background thread')

def seed_upstream_from_env(c):
    """按环境变量预置第三方上游模型。

    PaaS/沙箱的容器盘是临时的：每次重新部署都会重建 SQLite，
    后台 /admin 里手工添加的模型供应商会被清空（表现为重新部署后
    所有请求又变成 502）。这里让上游配置也能来自环境变量，
    因为环境变量在平台上是被持久保存的。

    支持两种写法：
    1) 单上游（简写）：UPSTREAM_BASE_URL / UPSTREAM_API_KEY / UPSTREAM_NAME /
       UPSTREAM_MODELS / UPSTREAM_FETCH_MODELS / UPSTREAM_IS_DEFAULT
    2) 多上游（编号）：UPSTREAM_1_BASE_URL、UPSTREAM_2_BASE_URL ... 每个编号
       都支持对应的 _API_KEY / _NAME / _MODELS / _FETCH_MODELS / _IS_DEFAULT 后缀。
       编号从 1 连续编号，遇到缺失的编号即停止。

    UPSTREAM_FETCH_MODELS=1 时启动时自动 GET {base}/models 全量导入
    （等价于后台的"自动发现"），可用 UPSTREAM_FETCH_MODELS_MAX 限制条数（默认 500）。
    兼容 OPENAI_BASE_URL / OPENAI_API_KEY / UPSTREAM_MODEL（单个模型）。
    建库时按 `模型ID + Base URL` 去重，重复部署不会产生重复记录。
    """
    groups=[]; _shorthand=None
    base=(os.environ.get('UPSTREAM_BASE_URL') or os.environ.get('OPENAI_BASE_URL') or '').strip()
    if base:
        _shorthand={'base':base,'key':(os.environ.get('UPSTREAM_API_KEY') or os.environ.get('OPENAI_API_KEY') or '').strip(),
            'name':(os.environ.get('UPSTREAM_NAME') or 'env 上游模型').strip() or 'env 上游模型',
            'models':(os.environ.get('UPSTREAM_MODELS') or os.environ.get('UPSTREAM_MODEL') or '').replace('\n',','),
            'fetch':os.environ.get('UPSTREAM_FETCH_MODELS',''),'default':os.environ.get('UPSTREAM_IS_DEFAULT','1')}
    for i in range(1,51):
        base=(os.environ.get('UPSTREAM_%d_BASE_URL'%i) or '').strip()
        if not base: break
        groups.append({'base':base,'key':(os.environ.get('UPSTREAM_%d_API_KEY'%i) or '').strip(),
            'name':(os.environ.get('UPSTREAM_%d_NAME'%i) or ('env 上游%d'%i)).strip(),
            'models':(os.environ.get('UPSTREAM_%d_MODELS'%i) or '').replace('\n',','),
            'fetch':os.environ.get('UPSTREAM_%d_FETCH_MODELS'%i,''),'default':os.environ.get('UPSTREAM_%d_IS_DEFAULT'%i,'0' if i>1 else '1')})
    # 编号组优先：只有完全没配编号组时才使用简写单上游变量。
    # 否则遗留的 UPSTREAM_BASE_URL（往往指向与某编号组同一家上游）会抢先插入模型，
    # 让那家供应商在后台少一个模型（例如 amd 只剩 4 个，另一个挂在 "env 上游模型" 名下）。
    if groups:
        if _shorthand:
            try:
                _n=c.execute("DELETE FROM model_providers WHERE name='env 上游模型' AND provider_type='openai'").rowcount
                if _n: print('[init] removed %s legacy shorthand row(s) superseded by numbered upstreams' % _n)
            except sqlite3.Error as _e:
                print('[init] legacy shorthand cleanup failed (ignored):', _e)
    elif _shorthand:
        groups.append(_shorthand)
    if not groups: return
    for gi,g in enumerate(groups):
        base=g['base'].rstrip('/')
        if not base.startswith('http'): continue
        key=g['key']
        raw=(g['models'] or '').replace('\n',',')
        models=[m.strip() for m in raw.split(',') if m.strip()]
        ocr_only=set()
        if g['fetch'].strip().lower() in ('1','true','yes','on'):
            fetched=[]; ocr=set()
            for attempt in (1,2,3):
                try:
                    r=requests.get(base+'/models', headers=({'Authorization':'Bearer '+key} if key else {}), timeout=8)
                    for m in (r.json().get('data') or []):
                        mid=m.get('id')
                        if not mid: continue
                        if mid not in fetched: fetched.append(mid)
                        outs=(m.get('output') or (m.get('architecture') or {}).get('output_modalities') or [])
                        outs=[str(x).lower() for x in outs]
                        if 'ocr' in outs and 'text' not in outs: ocr.add(mid)
                    if fetched: break
                    print('[init] upstream %s: /models returned 0 models (attempt %d/3)' % (g['name'], attempt))
                except Exception as e:
                    print('[init] upstream %s: /models attempt %d/3 failed: %s' % (g['name'], attempt, str(e)[:160]))
                time.sleep(1.5)
            if fetched:
                limit=max(1,int(os.environ.get('UPSTREAM_FETCH_MODELS_MAX','500')))
                kept=fetched[:limit]
                for mid in models:
                    if mid not in kept: kept.append(mid)
                models=kept; ocr_only=ocr
                print('[init] upstream %s: fetched %s model ids (%s ocr-only)' % (g['name'], len(fetched), len(ocr)))
            else:
                print('[init] upstream %s: /models fetch failed after 3 attempts, fallback to env list' % g['name'])
        if not models: models=[MODEL_NAME]
        is_default=0 if g['default'].strip().lower() in ('0','false','no','off') else 1
        added=0
        for idx, mid in enumerate(models):
            if c.execute('SELECT 1 FROM model_providers WHERE model_id=? AND base_url=?', (mid, base)).fetchone(): continue
            mods='["ocr"]' if mid in ocr_only else '["text"]'
            ep='ocr' if mid in ocr_only else 'chat_completions'
            c.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order,timeout_seconds,endpoint_type,modalities,supports_stream,supports_tools) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (g['name'],'openai',base,key,mid,mid,is_default if idx==0 else 0,1,(gi+1)*20+idx,'300',ep,mods,0 if mid in ocr_only else 1,0))
            added+=1
        print('[init] upstream %s: %s new row(s), %s model(s) total' % (g['name'], added, len(models)))
    # 真的导入了上游模型时，把本地 Ollama 占位降级为非默认：
    # 否则首页"默认模型"会显示一个平台上根本不存在的本地模型。
    try:
        if c.execute("SELECT 1 FROM model_providers WHERE provider_type='openai' AND is_default=1 AND is_active=1").fetchone():
            c.execute("UPDATE model_providers SET is_default=0 WHERE provider_type='ollama'")
            print('[init] local Ollama placeholder demoted (env upstreams are the default)')
    except sqlite3.Error as e:
        print('[init] demote ollama failed (ignored):', e)

def current_user():
    uid=session.get('uid')
    if not uid: return None
    return db().execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()

def login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if not current_user(): return redirect('/login')
        return fn(*a, **kw)
    return wrapper

def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        u=current_user()
        if not u: return redirect('/login')
        if not u['is_admin']: return page('<div class="card"><h2>无权限</h2></div>'),403
        return fn(*a, **kw)
    return wrapper

def get_settings():
    rows=db().execute('SELECT key,value FROM app_settings').fetchall()
    data={r['key']:r['value'] for r in rows}
    data.setdefault('epay_api_url', EPAY_API_URL); data.setdefault('epay_pid', EPAY_PID); data.setdefault('epay_key', EPAY_KEY)
    data.setdefault('domain', DOMAIN); data.setdefault('public_base_url', PUBLIC_BASE_URL); data.setdefault('payment_enabled','0')
    data.setdefault('smtp_enabled','0'); data.setdefault('smtp_host',''); data.setdefault('smtp_port','587'); data.setdefault('smtp_username',''); data.setdefault('smtp_password',''); data.setdefault('smtp_encryption','tls'); data.setdefault('smtp_from_email',''); data.setdefault('smtp_from_name','LLM Platform')
    return data

def set_setting(key,value):
    db().execute('INSERT INTO app_settings(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=CURRENT_TIMESTAMP',(key,value or ''))

def get_plan_config(include_inactive=False):
    where='' if include_inactive else ' WHERE is_active=1'
    rows=db().execute('SELECT * FROM plans'+where+' ORDER BY sort_order ASC, price ASC, id ASC').fetchall()
    return {r['id']:{'name':r['name'],'price':float(r['price'] or 0),'daily_tokens':int(r['daily_tokens'] or 0),'rate_limit':int(r['rate_limit'] or 60),'days':int(r['days'] or 30),'description':r['description'] or '','is_active':int(r['is_active'] or 0)} for r in rows} or plan_config_from_env()

def public_base_url():
    st=get_settings()
    return (st.get('public_base_url') or f"https://{st.get('domain') or DOMAIN}").rstrip('/')

def infer_model_capabilities(model_id, provider_name='', meta=None):
    """Best-effort capability inference from /models metadata and common model naming.
    It is intentionally conservative for text-only models and marks likely vision/video/audio models
    so the router can pre-filter multimodal requests before calling upstream.
    """
    text=((provider_name or '')+' '+(model_id or '')).lower()
    meta=meta or {}
    modalities={'text'}
    supports_tools=0
    supports_stream=1
    ctx=None; out=None
    # Common metadata fields from OpenAI-compatible aggregators.
    for key in ('modalities','input_modalities','supported_modalities'):
        vals=meta.get(key) if isinstance(meta,dict) else None
        if isinstance(vals,list):
            for v in vals:
                v=str(v).lower()
                if v in ('text','image','video','audio'): modalities.add(v)
                if v in ('vision','vl'): modalities.add('image')
    caps=meta.get('capabilities') if isinstance(meta,dict) else None
    if isinstance(caps,dict):
        for k,v in caps.items():
            kl=str(k).lower()
            if v and kl in ('vision','image','images'): modalities.add('image')
            if v and kl in ('video','videos'): modalities.add('video')
            if v and kl in ('audio','speech'): modalities.add('audio')
            if v and kl in ('tools','tool_calling','function_calling'): supports_tools=1
            if kl=='stream' and v is False: supports_stream=0
        if isinstance(caps.get('modalities'),list):
            for v in caps.get('modalities'):
                v=str(v).lower()
                if v in ('text','image','video','audio'): modalities.add(v)
    for key in ('context_length','max_context_length','max_input_tokens'):
        try:
            if meta.get(key): ctx=int(meta.get(key)); break
        except Exception: pass
    for key in ('max_output_tokens','max_completion_tokens'):
        try:
            if meta.get(key): out=int(meta.get(key)); break
        except Exception: pass
    # Heuristics for popular model naming.
    image_words=['vision','vl','vlm','visual','image','img','fuyu','kosmos','neva','vila','deplot','nvclip','qwen-vl','llava','pixtral','gpt-4o','gemini','claude-3','claude-3.5','claude-3-5','claude-3.7','claude-4','omni']
    video_words=['video','cosmos','sora','veo','wan','kling','hunyuan-video','ai-synthetic-video']
    audio_words=['audio','speech','tts','asr','whisper','realtime','omni']
    if any(w in text for w in image_words): modalities.add('image')
    if any(w in text for w in video_words): modalities.add('video')
    if any(w in text for w in audio_words): modalities.add('audio')
    # Embedding/rerank/safety parser models are usually not chat multimodal even if vendor name contains clues.
    non_chat=['embed','embedding','rerank','bge-','gliner','guard','safety','reward','parse','detector','translate']
    if any(w in text for w in non_chat):
        supports_tools=0
        if not any(w in text for w in ['vl','vision','image','video','audio','omni']): modalities={'text'}
    return {'modalities':sorted(modalities),'supports_stream':supports_stream,'supports_tools':supports_tools,'max_input_tokens':ctx,'max_output_tokens':out}

def fetch_openai_model_specs(base_url, api_key='', timeout=20):
    base=(base_url or '').rstrip('/')
    if not base: raise RuntimeError('Base URL 为空')
    headers={'Accept':'application/json'}
    if api_key: headers['Authorization']='Bearer '+api_key
    r=requests.get(base+'/models',headers=headers,timeout=timeout)
    if not r.ok: raise RuntimeError(f'读取模型失败 HTTP {r.status_code}: {r.text[:500]}')
    obj=r.json(); data=obj.get('data') if isinstance(obj,dict) else obj
    specs=[]
    if isinstance(data,list):
        for item in data:
            if isinstance(item,dict):
                mid=item.get('id')
                if mid: specs.append({'id':str(mid),'meta':item})
            else:
                specs.append({'id':str(item),'meta':{}})
    dedup={x['id']:x for x in specs if x.get('id')}
    return [dedup[k] for k in sorted(dedup)]

def fetch_openai_models(base_url, api_key='', timeout=20):
    return [x['id'] for x in fetch_openai_model_specs(base_url, api_key, timeout)]

def send_mail(to_email, subject, html_body):
    st=get_settings()
    if st.get('smtp_enabled')!='1': raise RuntimeError('SMTP 未启用')
    host=(st.get('smtp_host') or '').strip(); port=int(st.get('smtp_port') or 587); enc=(st.get('smtp_encryption') or 'tls').lower()
    username=st.get('smtp_username') or ''; password=st.get('smtp_password') or ''
    from_email=(st.get('smtp_from_email') or username or '').strip(); from_name=st.get('smtp_from_name') or 'LLM Platform'
    if not host or not from_email: raise RuntimeError('SMTP 主机或发件邮箱为空')
    msg=EmailMessage(); msg['Subject']=subject; msg['From']=f'{from_name} <{from_email}>'; msg['To']=to_email
    msg.set_content(re.sub('<[^<]+?>','',html_body)); msg.add_alternative(html_body, subtype='html')
    # Port 465 is implicit SSL for common providers such as smtp.qq.com.
    # Treat it as SSL even if the admin accidentally selected STARTTLS.
    if enc=='ssl' or port==465:
        with smtplib.SMTP_SSL(host,port,context=ssl.create_default_context(),timeout=20) as s:
            if username: s.login(username,password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host,port,timeout=20) as s:
            s.ehlo()
            if enc=='tls': s.starttls(context=ssl.create_default_context()); s.ehlo()
            if username: s.login(username,password)
            s.send_message(msg)

def send_activation_email(user_id, email):
    token=secrets.token_urlsafe(48); expires=(dt.datetime.now()+dt.timedelta(hours=24)).isoformat(timespec='seconds')
    con=db(); con.execute('INSERT INTO email_activations(user_id,email,token,expires_at) VALUES(?,?,?,?)',(user_id,email,token,expires)); con.commit()
    url=public_base_url()+'/activate?token='+token
    html=f'''<div style="font-family:Arial,sans-serif;max-width:640px;margin:auto;line-height:1.7;color:#111827"><h2>激活你的账号</h2><p>感谢注册 LLM Platform。请点击下面按钮完成邮箱验证并激活账号：</p><p><a href="{h(url)}" style="display:inline-block;background:#2563eb;color:#fff;padding:12px 20px;border-radius:8px;text-decoration:none">激活账号</a></p><p>如果按钮打不开，请复制链接到浏览器：</p><p style="word-break:break-all;color:#2563eb">{h(url)}</p><p style="color:#6b7280">链接 24 小时内有效。如非本人操作，请忽略。</p></div>'''
    send_mail(email,'激活你的 LLM Platform 账号',html)

def content_text(content):
    if isinstance(content, str): return content
    if isinstance(content, list):
        parts=[]
        for part in content:
            if not isinstance(part, dict): continue
            if part.get('type') in ('text','input_text','output_text'): parts.append(str(part.get('text','')))
            elif part.get('type') in ('image_url','input_image'): parts.append('[image]')
            elif part.get('type') in ('video_url','input_video'): parts.append('[video]')
            elif part.get('type') in ('audio_url','input_audio'): parts.append('[audio]')
        return '\n'.join(parts)
    return str(content or '')

def token_count(messages):
    total_chars=0
    for m in messages or []:
        if not isinstance(m, dict): continue
        content=m.get('content', m.get('input', ''))
        if isinstance(content, str): total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str): total_chars += len(part); continue
                if not isinstance(part, dict): continue
                ptype=part.get('type','')
                if ptype in ('text','input_text','output_text'): total_chars += len(part.get('text') or '')
                elif ptype in ('image_url','input_image'): total_chars += 1000
                elif ptype in ('video_url','input_video'): total_chars += 5000
                elif ptype in ('audio_url','input_audio'): total_chars += 3000
        else: total_chars += len(str(content or ''))
    return total_chars//2+1

def _bearer_stripped(value):
    value=(value or '').strip()
    if value[:7].lower()=='bearer ': value=value[7:].strip()
    return value

def api_key_candidates():
    """按优先级收集本次请求可能携带 API Key 的位置。

    线上实测（Cloudflare + 平台 ingress 的自定义域名）：Authorization 头会被
    丢弃或改写 —— 同一把 Key 用 Authorization 100% 401，改用 X-API-Key 立刻 200。
    因此把可能的来源都收集起来逐个比对，任一命中即通过。
    """
    cands=[]
    auth=(request.headers.get('Authorization') or '').strip()
    if auth: cands.append(_bearer_stripped(auth))
    # 自定义头（Authorization 被中间层动过时最先命中这里）
    for name in ('X-API-Key','api-key','x-api-key','x-auth-token','x-api-token','X-Authorization'):
        v=(request.headers.get(name) or '').strip()
        if v: cands.append(_bearer_stripped(v))
    # 查询串：?api_key= / ?key= / ?access_token=
    for q in ('api_key','key','access_token'):
        v=(request.args.get(q) or '').strip()
        if v: cands.append(_bearer_stripped(v))
    seen=set(); out=[]
    for c in cands:
        if c and c not in seen:
            seen.add(c); out.append(c)
    return out

def api_auth():
    for raw in api_key_candidates():
        key_hash=hashlib.sha256(raw.encode()).hexdigest()
        row=db().execute('SELECT k.*,u.plan,u.tokens_used_today,u.tokens_reset_date,u.is_active user_active,u.daily_token_limit,u.custom_rate_limit FROM api_keys k JOIN users u ON u.id=k.user_id WHERE k.key_hash=? AND k.is_active=1',(key_hash,)).fetchone()
        if row and row['user_active']: return row,raw
        if os.environ.get('AUTH_DEBUG'):
            app.logger.warning('AUTH_DEBUG miss: recv_len=%s recv_sha256=%s', len(raw), key_hash)
    return None,None

def epay_sign(params):
    filtered={k:v for k,v in params.items() if k not in ('sign','sign_type') and v not in ('',None)}
    s='&'.join(f'{k}={filtered[k]}' for k in sorted(filtered))+(get_settings().get('epay_key') or '')
    return hashlib.md5(s.encode()).hexdigest()

def active_model_rows():
    return db().execute('SELECT * FROM model_providers WHERE is_active=1 ORDER BY is_default DESC, sort_order ASC, id ASC').fetchall()

# ---------- 模型响应速度统计：auto 模式据此优先挑"反应快"的模型 ----------
AUTO_UNKNOWN_MS = float(os.environ.get('AUTO_UNKNOWN_MS', '2500'))        # 未测过模型的中性分
AUTO_FAIL_PENALTY_MS = float(os.environ.get('AUTO_FAIL_PENALTY_MS', '6000'))  # 失败折算的惩罚毫秒
AUTO_SORT_MODE = (os.environ.get('AUTO_SORT_MODE', 'speed') or 'speed').strip().lower()  # speed | manual
# 专用/非通用对话模型（翻译、安全审查、向量、解析等）虽然很快，但不适合做 auto 首选，降权处理
AUTO_EXCLUDE_PATTERNS = [p.strip().lower() for p in (os.environ.get('AUTO_EXCLUDE_PATTERNS') or 'embed,rerank,guard,safety,translate,detector,moderation,parse').split(',') if p.strip()]
AUTO_EXCLUDE_PENALTY = float(os.environ.get('AUTO_EXCLUDE_PENALTY', '100000'))

def _auto_excluded(model_id):
    mid = (model_id or '').lower()
    return any(p in mid for p in AUTO_EXCLUDE_PATTERNS)

def model_stats_map():
    """{provider_id: stats_row}，只读，供排序用。"""
    try:
        return {int(r['provider_id']): r for r in db().execute('SELECT * FROM model_stats').fetchall()}
    except Exception:
        return {}

def model_auto_score(row, stats):
    """分数越低越优先。没有统计数据的模型给中性分，保证新模型仍会被探索到。"""
    st = stats.get(int(row['id'])) if stats else None
    if not st:
        return AUTO_UNKNOWN_MS
    try:
        ok = int(st['ok_count'] or 0); bad = int(st['fail_count'] or 0)
        streak = int(st['fail_streak'] or 0); avg = float(st['avg_ms'] or 0)
    except Exception:
        return AUTO_UNKNOWN_MS
    if ok == 0 and bad > 0:
        return AUTO_UNKNOWN_MS + AUTO_FAIL_PENALTY_MS          # 从来没成功过，排到最后
    if streak >= 2:
        return AUTO_UNKNOWN_MS + AUTO_FAIL_PENALTY_MS * streak  # 连续失败越多越靠后
    if avg <= 0:
        return AUTO_UNKNOWN_MS
    rate = bad / float(ok + bad) if (ok + bad) else 0.0
    return avg + rate * AUTO_FAIL_PENALTY_MS

def auto_sorted_rows(rows):
    """auto 模式排序：非本地优先 → 实测延迟升序 → 默认模型 → 手工顺序。"""
    if AUTO_SORT_MODE == 'manual':
        return sorted(rows, key=lambda r: (0 if r['provider_type'] != 'ollama' else 1,
                                           0 if r['is_default'] else 1,
                                           int(r['sort_order'] or 100), int(r['id'])))
    stats = model_stats_map()
    return sorted(rows, key=lambda r: (0 if r['provider_type'] != 'ollama' else 1,
                                       model_auto_score(r, stats) + (AUTO_EXCLUDE_PENALTY if _auto_excluded(r['model_id']) else 0),
                                       0 if r['is_default'] else 1,
                                       int(r['sort_order'] or 100), int(r['id'])))

def _stats_conn():
    con = sqlite3.connect(DB_PATH, timeout=20)
    try:
        con.execute('PRAGMA busy_timeout=20000')
        con.execute('PRAGMA journal_mode=WAL')
    except sqlite3.Error:
        pass
    return con

def record_model_result(provider_id, ok, ms=0, error=''):
    """记录一次上游调用结果（EWMA 平均延迟 + 失败计数）。任何异常都不影响主流程。"""
    try:
        con = _stats_conn()
        try:
            row = con.execute('SELECT ok_count,fail_count,avg_ms,fail_streak FROM model_stats WHERE provider_id=?',
                              (int(provider_id),)).fetchone()
            if row:
                ok_c, bad_c, avg, streak = int(row[0] or 0), int(row[1] or 0), float(row[2] or 0), int(row[3] or 0)
            else:
                ok_c, bad_c, avg, streak = 0, 0, 0.0, 0
            err = ''
            if ok:
                avg = float(ms) if ok_c == 0 else avg * 0.7 + float(ms) * 0.3
                ok_c += 1; streak = 0
            else:
                bad_c += 1; streak += 1; err = str(error)[:200]
            if row:
                con.execute('UPDATE model_stats SET ok_count=?,fail_count=?,avg_ms=?,last_ms=?,fail_streak=?,last_error=?,updated_at=CURRENT_TIMESTAMP WHERE provider_id=?',
                            (ok_c, bad_c, avg, float(ms or 0), streak, err, int(provider_id)))
            else:
                con.execute('INSERT INTO model_stats(provider_id,ok_count,fail_count,avg_ms,last_ms,fail_streak,last_error) VALUES(?,?,?,?,?,?,?)',
                            (int(provider_id), ok_c, bad_c, avg, float(ms or 0), streak, err))
            con.commit()
        finally:
            con.close()
    except Exception as e:
        print('[stats] record failed (ignored):', e)

def reset_model_stats():
    try:
        con = _stats_conn()
        try:
            con.execute('DELETE FROM model_stats'); con.commit()
        finally:
            con.close()
        return True
    except Exception as e:
        print('[stats] reset failed:', e); return False

# ---------- 后台「一键测速」：实测每个模型的响应速度 ----------
PROBE_STATE = {'running': False, 'total': 0, 'done': 0, 'ok': 0, 'fail': 0,
               'started_at': '', 'finished_at': '', 'last': []}

def _probe_targets():
    """需要测速的模型行（跳过本地 Ollama 与 OCR 专用端点）。"""
    con = _stats_conn(); con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute('SELECT * FROM model_providers WHERE is_active=1 ORDER BY id').fetchall()]
    finally:
        con.close()
    out = []
    for r in rows:
        if (r.get('provider_type') or '') == 'ollama':
            continue
        if (r.get('endpoint_type') or 'chat_completions') == 'ocr':
            continue
        if not (r.get('base_url') or '').strip():
            continue
        out.append(r)
    return out

def _probe_one(row, timeout=None):
    """对单个模型发一次最小请求，返回 (ok, ms, error)。"""
    base = (row.get('base_url') or '').rstrip('/')
    et = row.get('endpoint_type') or 'chat_completions'
    key = row.get('api_key') or ''
    tmo = int(timeout or min(int(row.get('timeout_seconds') or 30), int(os.environ.get('PROBE_TIMEOUT', '30'))))
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    t0 = time.time()
    try:
        if et == 'anthropic_messages':
            url = base + '/messages'
            body = {'model': row.get('model_id'), 'max_tokens': 1, 'messages': [{'role': 'user', 'content': 'hi'}]}
            try:
                headers['anthropic-version'] = (json.loads(row.get('extra_config') or '{}') or {}).get('anthropic_version', '2023-06-01')
            except Exception:
                headers['anthropic-version'] = '2023-06-01'
            if key:
                headers['x-api-key'] = key
                headers.pop('Authorization', None)
        elif et == 'responses':
            url = base + '/responses'
            body = {'model': row.get('model_id'), 'input': 'hi', 'max_output_tokens': 16}
        else:
            url = base + '/chat/completions'
            # 不传 temperature：部分模型只接受 temperature=1（传 0 会 400），会造成误判
            body = {'model': row.get('model_id'), 'messages': [{'role': 'user', 'content': 'hi'}], 'max_tokens': 8}
        r = requests.post(url, headers=headers, json=body, timeout=tmo)
        ms = round((time.time() - t0) * 1000)
        if r.ok:
            return True, ms, ''
        return False, ms, 'HTTP %s %s' % (r.status_code, r.text[:150].replace(chr(10), ' '))
    except Exception as e:
        return False, round((time.time() - t0) * 1000), str(e)[:150]

def start_probe_models(max_workers=None):
    """后台线程逐个测速并写入 model_stats，返回 (ok, message)。"""
    if PROBE_STATE.get('running'):
        return False, '测速已在进行中（%d/%d），刷新页面可看进度' % (PROBE_STATE['done'], PROBE_STATE['total'])
    targets = _probe_targets()
    if not targets:
        return False, '没有可测速的模型（已跳过本地 Ollama 与 OCR 端点）'
    PROBE_STATE.update({'running': True, 'total': len(targets), 'done': 0, 'ok': 0, 'fail': 0,
                        'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'finished_at': '', 'last': []})

    def worker():
        conc = int(max_workers or os.environ.get('PROBE_CONCURRENCY', '4'))
        try:
            with ThreadPoolExecutor(max_workers=conc) as ex:
                futs = {ex.submit(_probe_one, row): row for row in targets}
                for f in as_completed(futs):
                    row = futs[f]
                    try:
                        ok, ms, err = f.result()
                    except Exception as e:
                        ok, ms, err = False, 0, str(e)[:150]
                    record_model_result(row['id'], ok, ms, err)
                    PROBE_STATE['done'] += 1
                    if ok:
                        PROBE_STATE['ok'] += 1
                    else:
                        PROBE_STATE['fail'] += 1
                    PROBE_STATE['last'] = (PROBE_STATE['last'] + [{'model': row.get('model_id'), 'upstream': row.get('name'), 'ok': ok, 'ms': ms}])[-8:]
        except Exception as e:
            print('[probe] worker crashed:', e)
        finally:
            PROBE_STATE['running'] = False
            PROBE_STATE['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
            print('[probe] finished: %s ok / %s fail' % (PROBE_STATE['ok'], PROBE_STATE['fail']))

    threading.Thread(target=worker, name='model-probe', daemon=True).start()
    return True, '已在后台开始测速 %d 个模型（并发 %s），完成后 auto 会按实测速度排序' % (len(targets), os.environ.get('PROBE_CONCURRENCY', '4'))

def candidate_model_rows(requested):
    rows=list(active_model_rows())
    if not rows: return []
    req=(requested or '').strip()
    if not req or req.lower() in ('auto','auto:fallback','fallback'):
        return auto_sorted_rows(rows)   # auto：按实测响应速度排序
    rows=sorted(rows, key=lambda r: (0 if r['provider_type']!='ollama' else 1, 0 if r['is_default'] else 1, int(r['sort_order'] or 100), int(r['id'])))
    exact=[]; rest=[]
    for r in rows:
        if req in (r['model_id'], r['display_name'], r['name']): exact.append(r)
        else: rest.append(r)
    return exact+rest if exact else rows

def select_model(requested):
    rows=candidate_model_rows(requested)
    return rows[0] if rows else None

def get_provider_endpoint_type(provider):
    try: return provider['endpoint_type'] or 'chat_completions'
    except Exception: return 'chat_completions'

def get_provider_modalities(provider):
    try:
        mods=json.loads(provider['modalities'] or '["text"]')
        return set(str(x).lower() for x in mods) if isinstance(mods, list) else {'text'}
    except Exception:
        return {'text'}

def get_provider_extra(provider):
    try:
        extra=json.loads(provider['extra_config'] or '{}')
        return extra if isinstance(extra, dict) else {}
    except Exception:
        return {}

def openai_compat_docs_html(public_base=None):
    base=h(public_base or public_base_url())
    return f"""<div class="card"><h3>后台使用说明 / OpenAI 兼容接口</h3>
<p class="muted">平台对外保持 OpenAI 兼容，用户只需要使用本站 API Key 和 Base URL。后台模型配置负责把不同第三方协议统一中转。</p>
<div class="grid"><div><h4>对外接口</h4><ul>
<li><code>GET /v1/models</code>：模型列表，含 <code>endpoint_type</code> 和 <code>capabilities</code></li>
<li><code>POST /v1/chat/completions</code>：OpenAI Chat Completions 兼容</li>
<li><code>POST /v1/responses</code>：OpenAI Responses 兼容</li>
<li><code>POST /v1/messages</code>：Anthropic Messages 兼容入口</li>
</ul></div><div><h4>模型配置字段</h4><ul>
<li><b>协议类型</b>：OpenAI Chat / OpenAI Responses / Anthropic Messages</li>
<li><b>模态能力</b>：文本、图片、视频、音频</li>
<li><b>stream/tools</b>：按上游真实能力勾选</li>
<li><b>extra_config</b>：JSON 扩展，例如 <code>{{"anthropic_version":"2023-06-01"}}</code></li>
</ul></div></div>
<h4>curl 示例</h4><pre class="curl-box">curl {base}/v1/models

curl {base}/v1/chat/completions \\
  -H 'Authorization: Bearer sk-你的Key' \\
  -H 'Content-Type: application/json' \\
  -d '{{"model":"auto","messages":[{{"role":"user","content":"Say OK"}}]}}'

curl {base}/v1/responses \\
  -H 'Authorization: Bearer sk-你的Key' \\
  -H 'Content-Type: application/json' \\
  -d '{{"model":"auto","input":"Say OK"}}'

curl {base}/v1/chat/completions \\
  -H 'Authorization: Bearer sk-你的Key' \\
  -H 'Content-Type: application/json' \\
  -d '{{"model":"auto","messages":[{{"role":"user","content":[{{"type":"text","text":"描述图片"}},{{"type":"image_url","image_url":{{"url":"https://example.com/a.jpg"}}}}]}}]}}'</pre>
<p class="muted">建议普通用户使用 <code>model:"auto"</code>，网关会按模型能力、排序和可用性自动选择；<code>stream=true</code> 不做中途失败切换。</p></div>"""

def detect_modalities_from_payload(payload):
    mods={'text'}
    raw=json.dumps(payload.get('messages') or payload.get('input') or '', ensure_ascii=False)
    if 'image_url' in raw or 'input_image' in raw or 'data:image/' in raw: mods.add('image')
    if 'video_url' in raw or 'input_video' in raw or 'data:video/' in raw: mods.add('video')
    if 'audio_url' in raw or 'input_audio' in raw or 'data:audio/' in raw: mods.add('audio')
    return mods

def filter_candidates_by_capability(candidates, payload, endpoint_types=None):
    required=detect_modalities_from_payload(payload)
    filtered=[]
    for p in candidates:
        et=get_provider_endpoint_type(p)
        if endpoint_types and et not in endpoint_types:
            continue
        if not required.issubset(get_provider_modalities(p)):
            continue
        if payload.get('stream') and not int(p['supports_stream'] if 'supports_stream' in p.keys() else 1):
            continue
        if payload.get('tools') and not int(p['supports_tools'] if 'supports_tools' in p.keys() else 0):
            continue
        filtered.append(p)
    return filtered

def chat_messages_to_responses_input(messages):
    result=[]
    for msg in messages or []:
        if not isinstance(msg, dict): continue
        role=msg.get('role','user')
        content=msg.get('content','')
        if isinstance(content, str):
            parts=[{'type':'input_text','text':content}]
        elif isinstance(content, list):
            parts=[]
            for part in content:
                if isinstance(part, str): parts.append({'type':'input_text','text':part}); continue
                if not isinstance(part, dict): continue
                ptype=part.get('type')
                if ptype in ('text','input_text'):
                    parts.append({'type':'input_text','text':part.get('text','')})
                elif ptype in ('image_url','input_image'):
                    image_url=part.get('image_url') or part.get('url') or part.get('image') or ''
                    if isinstance(image_url, dict): image_url=image_url.get('url','')
                    parts.append({'type':'input_image','image_url':image_url})
                elif ptype in ('video_url','input_video'):
                    video_url=part.get('video_url') or part.get('url') or ''
                    if isinstance(video_url, dict): video_url=video_url.get('url','')
                    parts.append({'type':'input_video','video_url':video_url})
                elif ptype in ('audio_url','input_audio'):
                    audio_url=part.get('audio_url') or part.get('url') or ''
                    if isinstance(audio_url, dict): audio_url=audio_url.get('url','')
                    parts.append({'type':'input_audio','audio_url':audio_url})
        else:
            parts=[{'type':'input_text','text':str(content or '')}]
        result.append({'role':role,'content':parts})
    return result

def responses_input_to_chat_messages(input_data):
    if isinstance(input_data, str): return [{'role':'user','content':input_data}]
    messages=[]
    for item in input_data or []:
        if isinstance(item, str): messages.append({'role':'user','content':item}); continue
        if not isinstance(item, dict): continue
        role=item.get('role','user'); content=item.get('content', item.get('input',''))
        if isinstance(content, str): messages.append({'role':role,'content':content}); continue
        parts=[]
        for part in content or []:
            if isinstance(part, str): parts.append({'type':'text','text':part}); continue
            if not isinstance(part, dict): continue
            ptype=part.get('type')
            if ptype in ('input_text','text'):
                parts.append({'type':'text','text':part.get('text','')})
            elif ptype in ('input_image','image_url'):
                url=part.get('image_url') or part.get('url') or ''
                parts.append({'type':'image_url','image_url':{'url':url}})
            elif ptype in ('input_video','video_url'):
                url=part.get('video_url') or part.get('url') or ''
                parts.append({'type':'video_url','video_url':url})
        messages.append({'role':role,'content':parts if parts else ''})
    return messages

def extract_responses_text(data):
    if not isinstance(data, dict): return ''
    if data.get('output_text'): return data.get('output_text') or ''
    text=''
    for item in data.get('output',[]) or []:
        if not isinstance(item, dict): continue
        for part in item.get('content',[]) or []:
            if isinstance(part, dict) and part.get('type') in ('output_text','text'):
                text += part.get('text','') or ''
    return text

def responses_to_chat_completion(data, model):
    text=extract_responses_text(data)
    usage=data.get('usage') or {} if isinstance(data,dict) else {}
    if 'input_tokens' in usage or 'output_tokens' in usage:
        usage={'prompt_tokens':usage.get('prompt_tokens',usage.get('input_tokens',0)),'completion_tokens':usage.get('completion_tokens',usage.get('output_tokens',0)),'total_tokens':usage.get('total_tokens',(usage.get('input_tokens') or 0)+(usage.get('output_tokens') or 0))}
    return {'id':data.get('id','chatcmpl-proxy') if isinstance(data,dict) else 'chatcmpl-proxy','object':'chat.completion','created':int(time.time()),'model':model,'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}],'usage':usage}

def chat_completion_to_response(data, model):
    content=''
    try: content=data.get('choices',[{}])[0].get('message',{}).get('content','') or ''
    except Exception: pass
    usage=data.get('usage') or {}
    return {'id':data.get('id','resp-proxy'),'object':'response','created_at':time.time(),'model':data.get('model') or model,'output_text':content,'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':content}]}],'usage':{'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'total_tokens':usage.get('total_tokens',0)}}

def check_quota_and_reset(key, input_tokens):
    today=dt.date.today().isoformat(); con=db()
    if key['tokens_reset_date']!=today:
        con.execute('UPDATE users SET tokens_used_today=0,tokens_reset_date=? WHERE id=?',(today,key['user_id'])); con.commit(); used=0
    else: used=key['tokens_used_today']
    plan_limit=get_plan_config(True).get(key['plan'],PLAN_CONFIG['free'])['daily_tokens']; limits=[plan_limit]
    if key['daily_token_limit']: limits.append(int(key['daily_token_limit']))
    if key['quota_daily']: limits.append(int(key['quota_daily']))
    if used+input_tokens>min(limits): return jsonify({'error':{'message':'Daily token quota exceeded','type':'quota_error'}}),429
    return None

def record_usage(key, input_tokens, output_tokens, model, endpoint, start):
    con=db(); total=int(input_tokens or 0)+int(output_tokens or 0); dur=int((time.time()-start)*1000)
    con.execute('UPDATE users SET tokens_used_today=tokens_used_today+? WHERE id=?',(total,key['user_id']))
    con.execute('UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?',(key['id'],))
    con.execute('INSERT INTO usage_logs(user_id,api_key_id,tokens_input,tokens_output,model,endpoint,duration_ms,ip_address) VALUES(?,?,?,?,?,?,?,?)',(key['user_id'],key['id'],input_tokens,output_tokens,model,endpoint,dur,request.remote_addr))
    con.commit()

def proxy_chat_completions_provider(provider, payload, key, input_tokens, start, target_api='chat_completions'):
    url=(provider['base_url'] or '').rstrip('/')
    if not url: raise RuntimeError('第三方模型 Base URL 为空')
    headers={'Content-Type':'application/json'}
    if provider['api_key']: headers['Authorization']='Bearer '+provider['api_key']
    out=dict(payload); out['model']=provider['model_id']
    if 'messages' not in out and 'input' in out:
        out['messages']=responses_input_to_chat_messages(out.get('input'))
    out.pop('input', None)
    r=requests.post(url+'/chat/completions',headers=headers,json=out,timeout=int(provider['timeout_seconds'] or 300),stream=bool(out.get('stream')))
    if not r.ok: raise RuntimeError(f"{provider['name']} upstream HTTP {r.status_code}: {r.text[:500]}")
    if out.get('stream'):
        def gen():
            for chunk in r.iter_content(chunk_size=None):
                if chunk: yield chunk
        return Response(gen(), mimetype=r.headers.get('content-type','text/event-stream'))
    data=r.json()
    if 'model' not in data: data['model']=provider['model_id']
    response_data=chat_completion_to_response(data,provider['model_id']) if target_api=='responses' else data
    content=''
    try: content=data.get('choices',[{}])[0].get('message',{}).get('content','') or ''
    except Exception: pass
    usage=data.get('usage') or {}; out_tokens=int(usage.get('completion_tokens') or max(1,len(content)//2))
    record_usage(key,input_tokens,out_tokens,provider['model_id'],'/v1/chat/completions',start)
    return jsonify(response_data)

def proxy_responses_stream(r, provider, target_api='responses'):
    if target_api=='responses':
        def passthrough():
            for chunk in r.iter_content(chunk_size=None):
                if chunk: yield chunk
        return Response(passthrough(), mimetype=r.headers.get('content-type','text/event-stream'))
    def gen():
        for line in r.iter_lines():
            if not line: continue
            text=line.decode(errors='ignore')
            if not text.startswith('data:'):
                continue
            data=text[5:].strip()
            if data=='[DONE]':
                yield 'data: [DONE]\n\n'; continue
            try: obj=json.loads(data)
            except Exception: continue
            delta=''
            if obj.get('type') in ('response.output_text.delta','response.refusal.delta'): delta=obj.get('delta','') or ''
            if delta:
                yield 'data: '+json.dumps({'id':obj.get('response_id','chatcmpl-proxy'),'object':'chat.completion.chunk','created':int(time.time()),'model':provider['model_id'],'choices':[{'index':0,'delta':{'content':delta},'finish_reason':None}]},ensure_ascii=False)+'\n\n'
        yield 'data: [DONE]\n\n'
    return Response(gen(),mimetype='text/event-stream')

def proxy_responses_provider(provider, payload, key, input_tokens, start, target_api='responses'):
    url=(provider['base_url'] or '').rstrip('/')
    if not url: raise RuntimeError('第三方模型 Base URL 为空')
    headers={'Content-Type':'application/json'}
    if provider['api_key']: headers['Authorization']='Bearer '+provider['api_key']
    out=dict(payload); out['model']=provider['model_id']
    if 'input' not in out and 'messages' in out:
        out['input']=chat_messages_to_responses_input(out.get('messages') or [])
    out.pop('messages',None)
    r=requests.post(url+'/responses',headers=headers,json=out,timeout=int(provider['timeout_seconds'] or 300),stream=bool(out.get('stream')))
    if not r.ok: raise RuntimeError(f"{provider['name']} upstream HTTP {r.status_code}: {r.text[:500]}")
    if out.get('stream'):
        return proxy_responses_stream(r,provider,target_api)
    data=r.json()
    response_data=responses_to_chat_completion(data,provider['model_id']) if target_api=='chat_completions' else data
    usage=response_data.get('usage') or data.get('usage') or {}
    out_tokens=int(usage.get('completion_tokens') or usage.get('output_tokens') or max(1,len(extract_responses_text(data))//2) or 1)
    record_usage(key,input_tokens,out_tokens,provider['model_id'],'/v1/responses',start)
    return jsonify(response_data)

def anthropic_to_chat_completion(data, model):
    text=''
    for part in data.get('content',[]) or []:
        if isinstance(part,dict) and part.get('type')=='text': text += part.get('text','') or ''
    usage=data.get('usage') or {}
    return {'id':data.get('id','chatcmpl-anthropic-proxy'),'object':'chat.completion','created':int(time.time()),'model':model,'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':data.get('stop_reason') or 'stop'}],'usage':{'prompt_tokens':usage.get('input_tokens',0),'completion_tokens':usage.get('output_tokens',0),'total_tokens':(usage.get('input_tokens') or 0)+(usage.get('output_tokens') or 0)}}

def proxy_anthropic_messages_provider(provider, payload, key, input_tokens, start, target_api='messages'):
    url=(provider['base_url'] or '').rstrip('/')
    if not url: raise RuntimeError('第三方模型 Base URL 为空')
    extra=get_provider_extra(provider); auth_type=extra.get('auth_type','x-api-key')
    headers={'Content-Type':'application/json','anthropic-version':extra.get('anthropic_version','2023-06-01')}
    if provider['api_key']:
        if auth_type=='bearer': headers['Authorization']='Bearer '+provider['api_key']
        else: headers['x-api-key']=provider['api_key']
    out=dict(payload); out['model']=provider['model_id']
    if 'messages' not in out and 'input' in out:
        out['messages']=responses_input_to_chat_messages(out.get('input'))
    if 'max_tokens' not in out: out['max_tokens']=int(provider['max_output_tokens'] or 1024) if 'max_output_tokens' in provider.keys() else 1024
    r=requests.post(url+'/messages',headers=headers,json=out,timeout=int(provider['timeout_seconds'] or 300),stream=bool(out.get('stream')))
    if not r.ok: raise RuntimeError(f"{provider['name']} upstream HTTP {r.status_code}: {r.text[:500]}")
    if out.get('stream'):
        def gen():
            for chunk in r.iter_content(chunk_size=None):
                if chunk: yield chunk
        return Response(gen(),mimetype=r.headers.get('content-type','text/event-stream'))
    data=r.json(); text=''
    for part in data.get('content',[]) or []:
        if isinstance(part,dict) and part.get('type')=='text': text += part.get('text','') or ''
    usage=data.get('usage') or {}; out_tokens=int(usage.get('output_tokens') or max(1,len(text)//2))
    record_usage(key,input_tokens,out_tokens,provider['model_id'],'/v1/messages',start)
    if target_api=='chat_completions': return jsonify(anthropic_to_chat_completion(data,provider['model_id']))
    return jsonify(data)


def proxy_ollama_as_response(provider, payload, key, input_tokens, start):
    payload2=dict(payload)
    if 'messages' not in payload2:
        payload2['messages']=responses_input_to_chat_messages(payload2.get('input',''))
    payload2['stream']=False
    base=(provider['base_url'] or OLLAMA_BASE_URL).rstrip('/'); model=provider['model_id'] or MODEL_NAME
    model_latest=model if model.endswith(':latest') or ':' in model else model+':latest'
    prompt='\n'.join([str(m.get('role','user'))+': '+content_text(m.get('content','')) for m in payload2.get('messages') or []])
    r=requests.post(base+'/api/generate',json={'model':model_latest,'prompt':prompt,'stream':False,'options':{'temperature':payload.get('temperature',0.7)}},timeout=int(provider['timeout_seconds'] or 300))
    if not r.ok: raise RuntimeError(r.text)
    obj=r.json(); content=obj.get('response',''); out_tokens=max(1,len(content)//2)
    record_usage(key,input_tokens,out_tokens,model,'/v1/responses',start)
    return jsonify({'id':'resp-local','object':'response','created_at':time.time(),'model':model,'output_text':content,'output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':content}]}],'usage':{'input_tokens':input_tokens,'output_tokens':out_tokens,'total_tokens':input_tokens+out_tokens}})

def proxy_provider(provider, payload, key, input_tokens, start, target_api='chat_completions'):
    if provider['provider_type']=='ollama':
        if target_api=='responses':
            return proxy_ollama_as_response(provider,payload,key,input_tokens,start)
        return proxy_ollama(provider,payload,key,input_tokens,start)
    endpoint_type=get_provider_endpoint_type(provider)
    if endpoint_type=='responses': return proxy_responses_provider(provider,payload,key,input_tokens,start,target_api)
    if endpoint_type=='anthropic_messages': return proxy_anthropic_messages_provider(provider,payload,key,input_tokens,start,target_api)
    return proxy_chat_completions_provider(provider,payload,key,input_tokens,start,target_api)

def proxy_ollama(provider, payload, key, input_tokens, start):
    con=db(); messages=payload.get('messages') or []
    base=(provider['base_url'] or OLLAMA_BASE_URL).rstrip('/'); model=provider['model_id'] or MODEL_NAME
    if not model.endswith(':latest') and ':' not in model: model_latest=model+':latest'
    else: model_latest=model
    stream=bool(payload.get('stream',False)); timeout=int(provider['timeout_seconds'] or 300)
    prompt='\n'.join([str(m.get('role','user'))+': '+str(m.get('content','')) for m in messages]) or str(payload.get('prompt',''))
    # Ollama 的部分 GGUF 模型只标记 completion，/api/generate 比 /api/chat 更稳更快；统一用 generate 再包装成 OpenAI 响应。
    r=requests.post(base+'/api/generate',json={'model':model_latest,'prompt':prompt,'stream':stream,'options':{'temperature':payload.get('temperature',0.7)}},timeout=timeout,stream=stream)
    if not r.ok: raise RuntimeError(r.text)
    if stream:
        def gen():
            for line in r.iter_lines():
                if line:
                    obj=json.loads(line.decode()); content=obj.get('response','')
                    yield 'data: '+json.dumps({'id':'chatcmpl-local','object':'chat.completion.chunk','created':int(time.time()),'model':model,'choices':[{'index':0,'delta':{'content':content},'finish_reason':'stop' if obj.get('done') else None}]},ensure_ascii=False)+'\n\n'
            yield 'data: [DONE]\n\n'
        return Response(gen(),mimetype='text/event-stream')
    obj=r.json(); content=obj.get('response',''); out_tokens=max(1,len(content)//2); dur=int((time.time()-start)*1000)
    con.execute('UPDATE users SET tokens_used_today=tokens_used_today+? WHERE id=?',(input_tokens+out_tokens,key['user_id']))
    con.execute('UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?',(key['id'],))
    con.execute('INSERT INTO usage_logs(user_id,api_key_id,tokens_input,tokens_output,model,endpoint,duration_ms,ip_address) VALUES(?,?,?,?,?,?,?,?)',(key['user_id'],key['id'],input_tokens,out_tokens,model,'/v1/chat/completions',dur,request.remote_addr))
    con.commit()
    return jsonify({'id':'chatcmpl-local','object':'chat.completion','created':int(time.time()),'model':model,'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':input_tokens,'completion_tokens':out_tokens,'total_tokens':input_tokens+out_tokens}})

@app.route('/healthz')
def healthz():
    """轻量健康检查：不做任何外部网络请求，保证平台探针秒回 200。"""
    return jsonify({'ok': True, 'service': 'llm-platform'})

_OLLAMA_PROBE={'ok':None}
def _ollama_probe_loop():
    """后台低频探测本地 Ollama，请求路径里永远不做这个网络请求。"""
    while True:
        try:
            _OLLAMA_PROBE['ok']=bool(requests.get(f'{OLLAMA_BASE_URL}/api/tags',timeout=5).ok)
        except Exception:
            _OLLAMA_PROBE['ok']=False
        time.sleep(60)
threading.Thread(target=_ollama_probe_loop,name='ollama-probe',daemon=True).start()

def ollama_status_html():
    ok=_OLLAMA_PROBE['ok']
    if ok is None: return '<span class="muted">探测中</span>'
    return '<span class="ok">已连接</span>' if ok else '<span class="bad">未连接</span>'

@app.route('/')
def index():
    public_base=public_base_url(); plans=get_plan_config(); projects=db().execute('SELECT * FROM managed_projects ORDER BY sort_order ASC,id DESC LIMIT 12').fetchall()
    am=active_model_rows(); default_model=(am[0]['model_id'] if am else MODEL_NAME)
    providers=sorted(set((m['name'] or '') for m in am if m['name']))
    rows=''
    for m in am[:120]:
        mods=' '.join('<span class="pill pill-gray">%s</span>'%h(x) for x in sorted(get_provider_modalities(m)))
        key=((m['model_id'] or '')+' '+(m['name'] or '')).lower()
        rows+=f'<tr class="model-row" data-k="{h(key)}"><td><b>{h(m["model_id"])}</b></td><td><span class="pill">{h(m["name"])}</span></td><td>{mods}</td><td class="muted">{h(get_provider_endpoint_type(m))}</td></tr>'
    more=(f'<p class="muted">仅展示前 120 个，完整列表见 <a href="/v1/models">/v1/models</a>（共 {len(am)} 个）。</p>' if len(am)>120 else '')
    cards=''
    for i,(pid,v) in enumerate(plans.items()):
        feat=[f'每日 {v["daily_tokens"]:,} tokens', f'限速 {v["rate_limit"]} 次/分钟', f'有效期 {v["days"]} 天', v.get('description') or '']
        lis=''.join('<li>%s</li>'%h(x) for x in feat if x)
        featured=' featured' if len(plans)>1 and i==1 else ''
        cta=('<a class="btn" href="/register">免费开始</a>' if float(v['price'])<=0 else '<a class="btn" href="/dashboard">立即购买</a>')
        cards+=f'<div class="card plan-card{featured}"><span class="pill">{h(pid)}</span><h3 style="margin-top:10px">{h(v["name"])}</h3><div class="price"><small>¥</small>{v["price"]:g}</div><ul>{lis}</ul>{cta}</div>'
    project_html=''.join([f'<div class="card"><span class="pill">{h(x["status"])}</span><h3 style="margin-top:10px">{h(x["name"])}</h3><p class="muted">{h(x["description"])}</p>'+(f'<a class="btn btn2" href="{h(x["base_url"])}">打开项目</a>' if x['base_url'] else '')+'</div>' for x in projects]) or '<div class="card">暂无项目</div>'
    model_filter_js = (
        '<script>'
        'document.addEventListener("DOMContentLoaded",function(){'
        'var i=document.getElementById("modelSearch"); if(!i) return;'
        'i.addEventListener("input",function(){'
        'var q=i.value.toLowerCase().trim();'
        'document.querySelectorAll(".model-row").forEach(function(r){'
        'var hit=(!q)||((r.getAttribute("data-k")||"").indexOf(q)>=0);'
        'r.style.display=hit?"":"none";});});});'
        '</script>'
    )
    body=f'''<section class="hero">
<span class="hero-tag">OpenAI 兼容 · 多上游聚合 · 统一计费</span>
<h1>一个 Base URL，调用全部大模型</h1>
<p>已接入 {len(providers)} 家上游、{len(am)} 个模型；支持 Chat Completions / Responses / Anthropic Messages 三种协议，按套餐统一限速与计量。</p>
<div class="hero-actions"><a class="btn btn-light" href="/register">免费注册</a><a class="btn btn-ghost" href="/playground">在线试用</a></div>
<div class="hero-stats"><div><b>{len(am)}</b><span>可用模型</span></div><div><b>{len(providers)}</b><span>上游供应商</span></div><div><b>{h(default_model)}</b><span>默认模型</span></div><div><b>{ollama_status_html()}</b><span>本地 Ollama</span></div></div>
</section>
<div class="two"><div class="card"><h3>快速接入</h3><p class="muted">在用户控制台创建 API Key，把 Base URL 指向下面地址即可，任何 OpenAI SDK / 客户端都能直接用。</p><div class="copy-box">Base URL：{h(public_base)}/v1</div><pre>curl {h(public_base)}/v1/chat/completions \\
  -H "Authorization: Bearer sk-你的KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"{h(default_model)}","messages":[{{"role":"user","content":"你好"}}]}}'</pre><p class="muted">若反代链路会丢弃 Authorization 头，可改用路径写法：<code>{h(public_base)}/k/&lt;你的KEY&gt;/v1</code></p></div>
<div class="card"><h3>平台状态</h3><p>Web 网关：<span class="ok">运行中</span></p><p>默认模型：<b>{h(default_model)}</b></p><p>可用模型：<b>{len(am)}</b> 个 · 上游供应商 <b>{len(providers)}</b> 家</p><p class="muted">模型配置由环境变量自动播种，重新部署后自动恢复；登录后可在 <a href="/dashboard">用户控制台</a> 创建 Key、查看用量/订单/工单。</p></div></div>
<div class="section-head" id="models"><h2>模型广场</h2><span class="muted">共 {len(am)} 个可用模型</span></div>
<div class="card"><input class="input model-search" id="modelSearch" placeholder="搜索模型或供应商，例如 deepseek / qwen / amd"><table><tr><th>模型 ID</th><th>上游</th><th>能力</th><th>协议</th></tr>{rows or '<tr><td colspan="4">暂无启用模型</td></tr>'}</table>{more}</div>
<div class="section-head" id="pricing"><h2>套餐价格</h2><span class="muted">价格/额度可在后台或环境变量调整</span></div>
<div class="grid">{cards}</div>
<div class="section-head"><h2>管理项目</h2></div>
<div class="grid">{project_html}</div>
{model_filter_js}'''
    return page(body)

def auth_card(title,subtitle,form_html,msg=''):
    return page(f'<div class="auth"><div class="auth-head"><h2>{h(title)}</h2><p>{h(subtitle)}</p></div><div class="auth-card">{msg}{form_html}</div></div>')

def login_form_html():
    return ('<form method="post"><label>用户名 / 邮箱</label><input class="input" name="username" placeholder="admin 或 admin@ypvps.com" autocomplete="username">'
            '<label>密码</label><input class="input" type="password" name="password" placeholder="请输入密码" autocomplete="current-password">'
            '<button class="btn">登录</button></form><div class="auth-switch">还没有账号？<a href="/register">免费注册</a></div>')

def register_form_html():
    return ('<form method="post"><label>用户名</label><input class="input" name="username" placeholder="3-32 位字母 / 数字 / 下划线">'
            '<label>邮箱</label><input class="input" name="email" placeholder="you@example.com">'
            '<label>密码</label><input class="input" type="password" name="password" placeholder="至少 6 位">'
            '<button class="btn">注册账号</button></form><div class="auth-switch">已有账号？<a href="/login">直接登录</a></div>')

@app.route('/login',methods=['GET','POST'])
def login():
    msg=''
    if request.method=='POST':
        name=request.form.get('username','').strip(); pw=request.form.get('password','')
        u=db().execute('SELECT * FROM users WHERE username=? OR email=?',(name,name)).fetchone()
        if u and check_password_hash(u['password_hash'],pw):
            if not u['is_active']:
                return auth_card('登录','使用 OpenAI 兼容接口，登录后即可创建 API Key','<p class="bad">账号未激活，请先前往邮箱点击激活链接</p>'+login_form_html())
            session['uid']=u['id']; db().execute('UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?',(u['id'],)); db().commit(); return redirect('/dashboard')
        msg='<p class="bad">账号或密码错误</p>'
    return auth_card('登录','使用 OpenAI 兼容接口，登录后即可创建 API Key',login_form_html(),msg)

@app.route('/register',methods=['GET','POST'])
def register():
    msg=''
    if request.method=='POST':
        username=request.form.get('username','').strip(); email=request.form.get('email','').strip(); pw=request.form.get('password','')
        if not re.match(r'^[a-zA-Z0-9_]{3,32}$',username): msg='<p class="bad">用户名 3-32 位字母数字下划线</p>'
        elif not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',email): msg='<p class="bad">请填写有效邮箱</p>'
        elif len(pw)<6: msg='<p class="bad">密码至少 6 位</p>'
        else:
            try:
                con=db(); mail_on=(get_settings().get('smtp_enabled')=='1')
                cur=con.execute('INSERT INTO users(username,email,password_hash,invite_code,tokens_reset_date,is_active) VALUES(?,?,?,?,?,?)',(username,email,generate_password_hash(pw),secrets.token_hex(6).upper(),dt.date.today().isoformat(),1 if not mail_on else 0)); con.commit()
                if mail_on:
                    send_activation_email(cur.lastrowid,email)
                    return auth_card('注册成功','还需一步：完成邮箱验证','<p class="ok">激活邮件已发送，请点击邮件里的链接完成注册。</p><p><a class="btn" href="/login">去登录</a></p>')
                return auth_card('注册成功','当前未启用邮件服务，账号已自动激活','<p class="ok">可以直接登录并创建 API Key 了。</p><p><a class="btn" href="/login">去登录</a></p>')
            except Exception as e: msg=f'<p class="bad">注册失败：{h(e)}</p>'
    return auth_card('创建账号','注册即可使用免费套餐，随后创建 API Key',register_form_html(),msg)

@app.route('/activate')
def activate():
    token=request.args.get('token','').strip(); con=db()
    row=con.execute('SELECT * FROM email_activations WHERE token=? AND used_at IS NULL',(token,)).fetchone()
    if not row: return auth_card('激活失败','激活链接无效','<p class="bad">链接无效或已被使用。</p><p><a class="btn" href="/register">重新注册</a></p>'),400
    if dt.datetime.fromisoformat(row['expires_at']) < dt.datetime.now(): return auth_card('激活失败','激活链接已过期','<p class="bad">请重新注册，或联系管理员手动激活。</p><p><a class="btn" href="/register">重新注册</a></p>'),400
    con.execute('UPDATE users SET is_active=1,email_verified_at=CURRENT_TIMESTAMP WHERE id=?',(row['user_id'],)); con.execute('UPDATE email_activations SET used_at=CURRENT_TIMESTAMP WHERE id=?',(row['id'],)); con.commit()
    return auth_card('账号已激活','邮箱验证成功','<p class="ok">现在可以登录并创建 API Key 了。</p><p><a class="btn" href="/login">去登录</a></p>')

@app.route('/logout')
def logout(): session.clear(); return redirect('/')

def format_playground_result(provider, response_text, elapsed_ms=None):
    provider_name=h(provider['name'] if provider else 'Ollama')
    provider_type=h(provider['provider_type'] if provider else 'ollama')
    configured_model=h(provider['model_id'] if provider else MODEL_NAME)
    raw=response_text or ''
    answer=''; actual_model=configured_model; usage=[]; finish=''
    pretty=raw
    try:
        data=json.loads(raw)
        pretty=json.dumps(data,ensure_ascii=False,indent=2)
        if isinstance(data,dict):
            actual_model=h(str(data.get('model') or configured_model))
            if isinstance(data.get('choices'),list) and data['choices']:
                ch=data['choices'][0] or {}
                msg=ch.get('message') or {}
                answer=msg.get('content') or ch.get('text') or ''
                finish=ch.get('finish_reason') or ''
            elif data.get('output_text'):
                answer=data.get('output_text') or ''
                finish=data.get('status') or ''
            elif isinstance(data.get('content'),list):
                answer=''.join([x.get('text','') for x in data.get('content') if isinstance(x,dict) and x.get('type')=='text'])
                finish=data.get('stop_reason') or ''
            elif 'response' in data:
                answer=data.get('response') or ''
                finish='done' if data.get('done') else ''
            u=data.get('usage') or {}
            if isinstance(u,dict):
                for label,key in [('输入','prompt_tokens'),('输出','completion_tokens'),('总计','total_tokens'),('输入','input_tokens'),('输出','output_tokens')]:
                    if key in u: usage.append(f'{label} {h(str(u[key]))}')
    except Exception:
        answer=raw
    if not answer:
        answer=raw
    meta=[f'供应商：{provider_name}', f'类型：{provider_type}', f'模型：{actual_model}']
    if elapsed_ms is not None: meta.append(f'耗时：{int(elapsed_ms)}ms')
    if finish: meta.append(f'结束：{h(str(finish))}')
    meta.extend(usage)
    meta_html=''.join(f'<span>{m}</span>' for m in meta)
    return '<div class="result-card"><div class="result-meta">'+meta_html+'</div><div class="assistant-answer">'+h(answer)+'</div><details class="raw-json"><summary>查看原始返回 JSON / 文本</summary><pre>'+h(pretty)+'</pre></details></div>'

@app.route('/playground',methods=['GET','POST'])
@login_required
def playground():
    u=current_user(); con=db(); result=''; raw=''
    if request.method=='POST':
        prompt=request.form.get('prompt','你好')
        k=con.execute('SELECT * FROM api_keys WHERE user_id=? AND is_active=1 ORDER BY id DESC LIMIT 1',(u['id'],)).fetchone()
        if not k:
            raw='sk-'+secrets.token_urlsafe(32); con.execute('INSERT INTO api_keys(user_id,key_hash,key_prefix,name,rate_limit) VALUES(?,?,?,?,?)',(u['id'],hashlib.sha256(raw.encode()).hexdigest(),raw[:10],'playground',get_plan_config(True).get(u['plan'],PLAN_CONFIG['free'])['rate_limit'])); con.commit()
        else: raw=k['key_prefix']+'...（已有 Key，完整值只在创建时显示）'
        try:
            errors=[]
            for provider in filter_candidates_by_capability(candidate_model_rows(request.form.get('model')), {'messages':[{'role':'user','content':prompt}]}, None):
                try:
                    start_call=time.time(); endpoint=get_provider_endpoint_type(provider)
                    headers={'Content-Type':'application/json'}
                    if provider['api_key']: headers['Authorization']='Bearer '+provider['api_key']
                    if provider['provider_type']=='ollama':
                        model_id=(provider['model_id'] if provider else MODEL_NAME)
                        if not model_id.endswith(':latest') and ':' not in model_id: model_id=model_id+':latest'
                        rr=requests.post((provider['base_url'] if provider else OLLAMA_BASE_URL).rstrip()+'/api/generate',json={'model':model_id,'prompt':prompt,'stream':False},timeout=min(60,int(provider['timeout_seconds'] if provider else 300)))
                    elif endpoint=='responses':
                        rr=requests.post((provider['base_url'] or '').rstrip()+'/responses',headers=headers,json={'model':provider['model_id'],'input':prompt,'temperature':0.4,'max_output_tokens':256},timeout=min(60,int(provider['timeout_seconds'] or 300)))
                    elif endpoint=='anthropic_messages':
                        extra=get_provider_extra(provider); headers={'Content-Type':'application/json','anthropic-version':extra.get('anthropic_version','2023-06-01')}
                        if provider['api_key']: headers['x-api-key']=provider['api_key']
                        rr=requests.post((provider['base_url'] or '').rstrip()+'/messages',headers=headers,json={'model':provider['model_id'],'messages':[{'role':'user','content':prompt}],'max_tokens':256},timeout=min(60,int(provider['timeout_seconds'] or 300)))
                    else:
                        rr=requests.post((provider['base_url'] or '').rstrip()+'/chat/completions',headers=headers,json={'model':provider['model_id'],'messages':[{'role':'user','content':prompt}],'temperature':0.4,'max_tokens':256},timeout=min(60,int(provider['timeout_seconds'] or 300)))
                    if not rr.ok: raise RuntimeError(f'HTTP {rr.status_code}: {rr.text[:300]}')
                    result=format_playground_result(provider, rr.text, int((time.time()-start_call)*1000)); break
                except Exception as e:
                    errors.append('%s/%s：%s' % (provider['name'],provider['model_id'],str(e)[:200]))
            if not result: result='<div class="result-card"><div class="assistant-answer">'+h('所有候选模型均调用失败：\n'+'\n'.join(errors))+'</div></div>'
        except Exception as e: result='<div class="result-card"><div class="assistant-answer">'+h('调用失败：'+str(e))+'</div></div>'
    models='<option value="auto">自动模式 auto · 第三方优先，本地最后兜底</option>'+''.join([f'<option value="{h(m["model_id"])}">{h(m["display_name"] or m["model_id"])} · {h(m["name"])} · {h(m["provider_type"])}</option>' for m in candidate_model_rows('auto')])
    curl="curl -X POST "+public_base_url()+"/v1/chat/completions \\\n  -H 'Content-Type: application/json' \\\n  -H 'Authorization: Bearer YOUR_API_KEY' \\\n  -d '{\"model\":\"auto\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'"
    return page(f'''<div class="two playground">
<div class="card"><h2>聊天测试</h2><p class="muted">当前登录用户：{h(u["username"])} · ID {u["id"]}</p><form method="post"><div class="chat-toolbar"><div><label class="muted">选择模型</label><select class="input" name="model">{models}</select></div><button class="btn">发送测试</button></div><textarea class="input chat-prompt" name="prompt" rows="5" placeholder="输入问题，支持多行；拖动右下角可调整高度"></textarea></form><h3>输出结果</h3><div class="chat-result">{result or '<div class="result-card"><div class="assistant-answer muted">等待发送测试...</div></div>'}</div></div>
<div class="card"><h3>使用说明</h3><p><b>推荐使用自动模式：</b><code>model: "auto"</code></p><ul><li>优先调用第三方 / OpenAI 兼容模型。</li><li>第三方模型故障、超时或返回错误时，自动尝试下一条启用模型。</li><li>请求包含图片/视频/音频/tools 时，会先按后台能力配置筛选模型。</li><li>所有第三方都不可用时，最后才切到本地 Ollama。</li><li><code>stream=true</code> 暂不做自动切换，避免流式响应中途换模型。</li></ul><h3>OpenAI 兼容接口</h3><ul><li><code>/v1/models</code></li><li><code>/v1/chat/completions</code></li><li><code>/v1/responses</code></li><li><code>/v1/messages</code></li></ul><h3>curl 测试命令</h3><pre class="curl-box">{h(curl)}</pre><p class="muted key-line">当前 API Key：{h(raw or '请先在控制台创建；发送一次测试会自动生成或复用 Key')}</p></div>
</div>''')

def usage_guide_html(sample_key):
    """OpenAI 兼容接入说明，登录用户可见。"""
    base_v1=public_base_url()+'/v1'
    return f'''<div class="card"><h3>使用方法（OpenAI 兼容接口）</h3>
<p>接口地址：<b>{h(base_v1)}</b><br>认证方式：请求头 <code>Authorization: Bearer &lt;你的 API Key&gt;</code></p>
<p class="muted"><b>如果 Authorization 头被中间的 CDN/网关丢弃（表现为一直 401，而 Key 本身没错）</b>，把 Key 放进 URL 路径即可，无需自定义头：<br>
<code>{h(public_base_url())}/k/&lt;你的 API Key&gt;/v1</code> —— 把这个当地址填，Key 随便填。<br>
也支持自定义头 <code>X-API-Key</code> 与查询串 <code>?api_key=&lt;key&gt;</code>。</p>
<table><tr><th>端点</th><th>方法</th><th>说明</th></tr>
<tr><td><code>/v1/models</code></td><td>GET</td><td>列出当前可用模型</td></tr>
<tr><td><code>/v1/chat/completions</code></td><td>POST</td><td>对话补全，支持 <code>stream: true</code> 流式</td></tr>
<tr><td><code>/v1/responses</code></td><td>POST</td><td>OpenAI Responses 兼容</td></tr>
<tr><td><code>/v1/messages</code></td><td>POST</td><td>Anthropic Messages 兼容</td></tr></table>
<p class="muted"><code>model</code> 填 <code>auto</code> 会自动路由（第三方优先、本地兜底），也可以直接填下方模型列表里的模型 ID。</p>
<h4>curl</h4><pre>curl {h(base_v1)}/chat/completions \\
  -H "Authorization: Bearer {h(sample_key)}" \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"auto","messages":[{{"role":"user","content":"你好"}}]}}'</pre>
<h4>curl —— Key 写在路径里（Authorization 被中间层吃掉时用这个）</h4><pre>curl {h(public_base_url())}/k/{h(sample_key)}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -d '{{"model":"auto","messages":[{{"role":"user","content":"你好"}}]}}'</pre>
<h4>Python（openai SDK）</h4><pre>from openai import OpenAI
client = OpenAI(api_key="{h(sample_key)}", base_url="{h(base_v1)}")
r = client.chat.completions.create(model="auto", messages=[{{"role":"user","content":"你好"}}])
print(r.choices[0].message.content)</pre>
<h4>Node.js</h4><pre>import OpenAI from "openai";
const client = new OpenAI({{ apiKey: "{h(sample_key)}", baseURL: "{h(base_v1)}" }});
const r = await client.chat.completions.create({{ model: "auto", messages: [{{ role: "user", content: "你好" }}] }});
console.log(r.choices[0].message.content);</pre>
<p class="muted">也可以直接用站内 <a href="/playground">聊天测试</a> 页验证连通性；报 401 说明 Key 不对，报 502 说明所有上游模型都调用失败（去后台检查供应商配置）。</p></div>'''

def model_list_html():
    """所有启用中的模型，登录用户可见。"""
    rows=active_model_rows()
    if not rows:
        return '<div class="card"><h3>可用模型列表</h3><p class="muted">还没有启用中的模型，请联系管理员在管理后台添加模型供应商。</p></div>'
    trs=[]
    for m in rows:
        try: mods='/'.join(json.loads(m['modalities'] or '["text"]'))
        except Exception: mods='text'
        caps=[]
        if m['supports_stream']: caps.append('流式')
        if m['supports_tools']: caps.append('工具调用')
        if m['supports_vision']: caps.append('视觉')
        if m['supports_video']: caps.append('视频')
        badge=' <span class="pill">默认</span>' if m['is_default'] else ''
        trs.append(f'<tr><td><b>{h(m["model_id"])}</b>{badge}</td><td>{h(m["display_name"] or m["name"])}</td><td>{h(m["provider_type"])}</td><td>{h(mods)}</td><td>{h(" · ".join(caps) or "文本")}</td></tr>')
    return f'''<div class="card"><h3>可用模型列表</h3>
<table><tr><th>模型 ID</th><th>名称</th><th>类型</th><th>模态</th><th>能力</th></tr>{"".join(trs)}</table>
<p class="muted">实时列表：<code>GET {h(public_base_url())}/v1/models</code>（返回 OpenAI 格式，可直接被客户端拉取）。</p></div>'''

@app.route('/dashboard',methods=['GET','POST'])
@login_required
def dashboard():
    u=current_user(); con=db()
    if request.method=='POST':
        act=request.form.get('act')
        if act=='newkey':
            max_keys=int(u['max_api_keys'] or 5); key_count=con.execute('SELECT COUNT(*) c FROM api_keys WHERE user_id=?',(u['id'],)).fetchone()['c']
            if key_count >= max_keys:
                session['new_key']='已达到 API Key 数量上限：%s 个，请删除旧 Key 或联系管理员调整。' % max_keys
            else:
                raw='sk-'+secrets.token_urlsafe(32); con.execute('INSERT INTO api_keys(user_id,key_hash,key_prefix,key_plain,name,rate_limit) VALUES(?,?,?,?,?,?)',(u['id'],hashlib.sha256(raw.encode()).hexdigest(),raw[:10],raw,request.form.get('name','default'),get_plan_config(True).get(u['plan'],PLAN_CONFIG['free'])['rate_limit'])); con.commit(); session['new_key']=raw
        elif act=='rotate_key':
            kid=request.form.get('key_id'); row=con.execute('SELECT * FROM api_keys WHERE id=? AND user_id=?',(kid,u['id'])).fetchone()
            if row:
                raw='sk-'+secrets.token_urlsafe(32); con.execute('UPDATE api_keys SET key_hash=?,key_prefix=?,key_plain=?,last_used_at=NULL WHERE id=? AND user_id=?',(hashlib.sha256(raw.encode()).hexdigest(),raw[:10],raw,kid,u['id'])); con.commit(); session['new_key']=raw
        elif act=='ticket':
            title=request.form.get('title','').strip(); content=request.form.get('content','').strip()
            cur=con.execute('INSERT INTO tickets(user_id,title,content) VALUES(?,?,?)',(u['id'],title,content)); con.execute('INSERT INTO ticket_messages(ticket_id,user_id,author_role,message) VALUES(?,?,?,?)',(cur.lastrowid,u['id'],'user',content)); con.commit()
        elif act=='order':
            plan=request.form.get('plan','starter'); pay_type=request.form.get('payment_method','alipay')
            if pay_type not in ('alipay','wxpay'): pay_type='alipay'
            cfg=get_plan_config().get(plan) or PLAN_CONFIG['starter']; order_no='O'+dt.datetime.now().strftime('%Y%m%d%H%M%S')+secrets.token_hex(3).upper(); con.execute('INSERT INTO orders(order_no,user_id,plan_id,amount,payment_method) VALUES(?,?,?,?,?)',(order_no,u['id'],plan,cfg['price'],pay_type)); con.commit(); return redirect('/pay/'+order_no)
    newkey=session.pop('new_key',None)
    keys=con.execute('SELECT * FROM api_keys WHERE user_id=? ORDER BY id DESC',(u['id'],)).fetchall(); orders=con.execute('SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10',(u['id'],)).fetchall(); tickets=con.execute('SELECT * FROM tickets WHERE user_id=? ORDER BY id DESC LIMIT 10',(u['id'],)).fetchall()
    plan_map=get_plan_config(True)
    current_plan=(plan_map.get(u['plan']) or PLAN_CONFIG.get(u['plan']) or PLAN_CONFIG['free'])
    current_plan_name=current_plan.get('name',u['plan'])
    daily_limit=int(u['daily_token_limit'] or current_plan.get('daily_tokens') or 0)
    rate_limit=int(u['custom_rate_limit'] or current_plan.get('rate_limit') or 0)
    used_today=int(u['tokens_used_today'] or 0)
    remaining=max(0,daily_limit-used_today) if daily_limit else 0
    expire_text=h(u['plan_expires_at'] or '长期/未设置')
    usage_pct=min(100, int(used_today*100/daily_limit)) if daily_limit else 0
    plan_usage=f'<div class="card"><h3>当前套餐</h3><p>套餐：<b>{h(current_plan_name)}</b></p><p>每日额度：<b>{daily_limit:,}</b> tokens</p><p>今日已用：<b>{used_today:,}</b> tokens · 剩余：<b>{remaining:,}</b> tokens</p><div style="height:10px;background:#e5e7eb;border-radius:999px;overflow:hidden"><div style="width:{usage_pct}%;height:10px;background:#2563eb"></div></div><p>限速：<b>{rate_limit}</b> 次/分钟</p><p>到期时间：{expire_text}</p></div>'
    max_keys=int(u['max_api_keys'] or 5); key_count=len(keys)
    key_html=''.join([f'<li><b>{h(k["name"])}</b> · active={k["is_active"]}<br><input class="input" readonly onclick="this.select()" value="{h(k["key_plain"] or (k["key_prefix"]+"...（历史 Key 仅保存前缀，请重新生成后复制）"))}"><form method="post"><input type="hidden" name="act" value="rotate_key"><input type="hidden" name="key_id" value="{k["id"]}"><button class="btn btn2">重新生成并显示</button></form></li>' for k in keys]) or '<li>暂无</li>'
    order_html=''.join([f'<li>{h(o["order_no"])} · {h((plan_map.get(o["plan_id"]) or {}).get("name", o["plan_id"]))} · ¥{o["amount"]} · {h(o["status"])}</li>' for o in orders]) or '<li>暂无</li>'
    ticket_html=''.join([f'<li><a href="/ticket/{t["id"]}"><b>#{t["id"]} {h(t["title"])}</b></a> · {h(t["status"])}<br><span class="muted">{h(t["content"])}</span></li>' for t in tickets]) or '<li>暂无</li>'
    plans=''.join([f'<option value="{h(pid)}">{h(v["name"])} ¥{v["price"]:g}</option>' for pid,v in get_plan_config().items() if pid!='free'])
    reveal=f'<div class="card"><h3>新 API Key，只显示一次</h3><pre>{h(newkey)}</pre></div>' if newkey else ''
    sample_key=newkey or next((dict(k).get('key_plain') for k in keys if dict(k).get('key_plain')), 'sk-你的API Key')
    body=f'<div class="two"><div><div class="card"><h2>用户控制台</h2><p>用户：{h(u["username"])} </p><a href="/logout">退出</a></div>{plan_usage}{reveal}<div class="card"><h3>API Keys</h3><p class="muted">已创建 {key_count} / 允许 {max_keys} 个；点击输入框可全选复制。</p><ul>{key_html}</ul><form method="post"><input type="hidden" name="act" value="newkey"><input class="input" name="name" placeholder="Key 名称"><button class="btn">创建 API Key</button></form></div></div><div><div class="card"><h3>购买套餐</h3><form method="post"><input type="hidden" name="act" value="order"><select class="input" name="plan">{plans}</select><select class="input" name="payment_method"><option value="alipay">支付宝</option><option value="wxpay">微信支付</option></select><button class="btn">创建订单</button></form><h4>我的订单</h4><ul>{order_html}</ul></div><div class="card"><h3>提交工单</h3><form method="post"><input type="hidden" name="act" value="ticket"><input class="input" name="title" placeholder="标题"><textarea class="input" name="content" placeholder="问题描述"></textarea><button class="btn btn2">提交</button></form><h4>我的工单</h4><ul>{ticket_html}</ul></div></div></div>{usage_guide_html(sample_key)}{model_list_html()}'
    return page(body)

@app.route('/ticket/<int:ticket_id>',methods=['GET','POST'])
@login_required
def ticket_detail(ticket_id):
    u=current_user(); con=db(); t=con.execute('SELECT * FROM tickets WHERE id=? AND user_id=?',(ticket_id,u['id'])).fetchone()
    if not t: return page('<div class="card">工单不存在</div>'),404
    if request.method=='POST':
        msg=request.form.get('message','').strip()
        if msg: con.execute('INSERT INTO ticket_messages(ticket_id,user_id,author_role,message) VALUES(?,?,?,?)',(ticket_id,u['id'],'user',msg)); con.execute('UPDATE tickets SET status="open",updated_at=CURRENT_TIMESTAMP WHERE id=?',(ticket_id,)); con.commit(); return redirect('/ticket/'+str(ticket_id))
    msgs=con.execute('SELECT m.*,u.username FROM ticket_messages m LEFT JOIN users u ON u.id=m.user_id WHERE m.ticket_id=? ORDER BY m.id ASC',(ticket_id,)).fetchall()
    mh=''.join([f'<div class="card"><span class="pill">{h(m["author_role"])}</span><p>{h(m["message"])}</p><p class="muted">{h(m["username"] or "管理员")} · {h(m["created_at"])}</p></div>' for m in msgs])
    return page(f'<div class="card"><h2>工单 #{t["id"]} {h(t["title"])}</h2><p>状态：<b>{h(t["status"])}</b></p></div>{mh}<div class="card"><h3>继续补充</h3><form method="post"><textarea class="input" name="message" rows="4"></textarea><button class="btn">提交</button> <a class="btn btn2" href="/dashboard">返回</a></form></div>')

@app.route('/pay/<order_no>')
@login_required
def pay(order_no):
    o=db().execute('SELECT * FROM orders WHERE order_no=? AND user_id=?',(order_no,current_user()['id'])).fetchone()
    if not o: return page('<div class="card">订单不存在</div>'),404
    st=get_settings(); epay_api_url=(st.get('epay_api_url') or '').rstrip('/'); epay_pid=st.get('epay_pid') or ''; epay_key=st.get('epay_key') or ''
    missing=[]
    if st.get('payment_enabled')!='1': missing.append('未启用支付')
    if not epay_api_url: missing.append('易支付网关为空')
    if not epay_pid: missing.append('商户 PID 为空')
    if not epay_key: missing.append('商户 Key 为空')
    if missing: return page(f'<div class="card"><h2>订单已创建</h2><p>订单号：{h(o["order_no"])}</p><p>金额：¥{o["amount"]}</p><p class="bad">当前仍为演示模式：{h("；".join(missing))}</p><p class="muted">后台会显示具体缺失项，保存完整配置后重试。</p></div>')
    pay_type=o['payment_method'] if o['payment_method'] in ('alipay','wxpay') else 'alipay'
    params={'pid':epay_pid,'type':pay_type,'out_trade_no':o['order_no'],'notify_url':public_base_url()+'/payment/notify','return_url':public_base_url()+'/payment/success','name':'LLM套餐-'+o['plan_id'],'money':str(o['amount']),'sitename':'LLM Platform'}
    params['sign']=epay_sign(params); params['sign_type']='MD5'
    return redirect(epay_api_url+'/submit.php?'+urlencode(params))

@app.route('/payment/notify',methods=['GET','POST'])
def payment_notify():
    params=request.args.to_dict() or request.form.to_dict()
    if not (get_settings().get('epay_key') or '') or params.get('sign')!=epay_sign(params): return 'fail',400
    if params.get('trade_status')=='TRADE_SUCCESS':
        con=db(); o=con.execute('SELECT * FROM orders WHERE order_no=?',(params.get('out_trade_no'),)).fetchone()
        if o:
            cfg=get_plan_config(True).get(o['plan_id'],PLAN_CONFIG['free']); exp=(dt.datetime.now()+dt.timedelta(days=cfg['days'])).isoformat(); con.execute('UPDATE orders SET status="paid",trade_no=?,paid_at=CURRENT_TIMESTAMP WHERE id=?',(params.get('trade_no',''),o['id'])); con.execute('UPDATE users SET plan=?,plan_expires_at=? WHERE id=?',(o['plan_id'],exp,o['user_id'])); con.commit()
    return 'success'

@app.route('/payment/success')
def payment_success(): return page('<div class="card"><h2>支付完成</h2><p>如果已到账，套餐会自动生效。</p><a class="btn" href="/dashboard">返回控制台</a></div>')

def sel(cur,val): return 'selected' if str(cur)==str(val) else ''

def admin_table_helpers(con):
    pass

@app.route('/admin',methods=['GET','POST'])
@admin_required
def admin():
    con=db(); msg=''
    if request.method=='POST':
        act=request.form.get('act')
        if act=='save_epay':
            for k in ['epay_api_url','epay_pid','epay_key','domain','public_base_url','payment_enabled']: set_setting(k,request.form.get(k,''))
            con.commit(); msg='易支付配置已保存'
        elif act=='save_smtp':
            for k in ['smtp_enabled','smtp_host','smtp_port','smtp_username','smtp_password','smtp_encryption','smtp_from_email','smtp_from_name']: set_setting(k,request.form.get(k,''))
            con.commit(); msg='SMTP 邮件配置已保存'
        elif act=='test_smtp':
            for k in ['smtp_enabled','smtp_host','smtp_port','smtp_username','smtp_password','smtp_encryption','smtp_from_email','smtp_from_name']: set_setting(k,request.form.get(k,''))
            con.commit()
            try:
                send_mail(request.form.get('test_email','').strip() or (current_user()['email'] or ''),'LLM Platform SMTP 测试邮件','<p>这是一封 SMTP 配置测试邮件。收到说明邮件服务可用。</p>')
                msg='SMTP 测试邮件已发送'
            except Exception as e: msg='SMTP 测试失败：'+str(e)
        elif act=='update_user':
            uid=request.form.get('user_id'); new_password=request.form.get('new_password','')
            con.execute('UPDATE users SET username=?,email=?,plan=?,daily_token_limit=?,custom_rate_limit=?,max_api_keys=?,is_active=?,is_admin=?,plan_expires_at=?,balance=?,tokens_used_today=? WHERE id=?',(request.form.get('username',''),request.form.get('email',''),request.form.get('plan','free'),request.form.get('daily_token_limit') or None,request.form.get('custom_rate_limit') or None,int(request.form.get('max_api_keys') or 5),1 if request.form.get('is_active')=='1' else 0,1 if request.form.get('is_admin')=='1' else 0,request.form.get('plan_expires_at') or None,float(request.form.get('balance') or 0),int(request.form.get('tokens_used_today') or 0),uid))
            if new_password:
                if len(new_password) < 6: msg='密码至少 6 位'
                else: con.execute('UPDATE users SET password_hash=? WHERE id=?',(generate_password_hash(new_password),uid)); msg='用户配置和密码已更新'
            else: msg='用户配置已更新'
            con.commit()
        elif act=='delete_user':
            uid=request.form.get('user_id')
            if str(uid)==str(current_user()['id']): msg='不能删除当前登录管理员'
            else:
                con.execute('DELETE FROM usage_logs WHERE user_id=?',(uid,)); con.execute('DELETE FROM api_keys WHERE user_id=?',(uid,)); con.execute('DELETE FROM ticket_messages WHERE ticket_id IN (SELECT id FROM tickets WHERE user_id=?)',(uid,)); con.execute('DELETE FROM tickets WHERE user_id=?',(uid,)); con.execute('DELETE FROM invoices WHERE user_id=?',(uid,)); con.execute('DELETE FROM orders WHERE user_id=?',(uid,)); con.execute('DELETE FROM users WHERE id=?',(uid,)); con.commit(); msg='用户及关联数据已删除'
        elif act=='reset_user_usage':
            uid=request.form.get('user_id'); con.execute('UPDATE users SET tokens_used_today=0,tokens_reset_date=? WHERE id=?',(dt.date.today().isoformat(),uid)); con.execute('DELETE FROM usage_logs WHERE user_id=?',(uid,)); con.commit(); msg='用户用量与日志已清理'
        elif act=='update_key':
            con.execute('UPDATE api_keys SET name=?,is_active=?,rate_limit=?,quota_daily=?,allowed_ips=? WHERE id=?',(request.form.get('name',''),1 if request.form.get('is_active')=='1' else 0,int(request.form.get('rate_limit') or 60),request.form.get('quota_daily') or None,request.form.get('allowed_ips',''),request.form.get('key_id'))); con.commit(); msg='API Key 配置已更新'
        elif act=='delete_key':
            kid=request.form.get('key_id'); con.execute('DELETE FROM usage_logs WHERE api_key_id=?',(kid,)); con.execute('DELETE FROM api_keys WHERE id=?',(kid,)); con.commit(); msg='API Key 及关联日志已删除'
        elif act=='mark_paid':
            o=con.execute('SELECT * FROM orders WHERE id=?',(request.form.get('order_id'),)).fetchone()
            if o:
                cfg=get_plan_config(True).get(o['plan_id'],PLAN_CONFIG['free']); exp=(dt.datetime.now()+dt.timedelta(days=cfg['days'])).isoformat(); con.execute('UPDATE orders SET status="paid",paid_at=CURRENT_TIMESTAMP WHERE id=?',(o['id'],)); con.execute('UPDATE users SET plan=?,plan_expires_at=? WHERE id=?',(o['plan_id'],exp,o['user_id'])); con.commit(); msg='订单已确认并开通套餐'
        elif act=='update_order':
            con.execute('UPDATE orders SET plan_id=?,amount=?,payment_method=?,trade_no=?,status=?,paid_at=? WHERE id=?',(request.form.get('plan_id',''),float(request.form.get('amount') or 0),request.form.get('payment_method',''),request.form.get('trade_no',''),request.form.get('status','pending'),request.form.get('paid_at') or None,request.form.get('order_id'))); con.commit(); msg='订单已更新'
        elif act=='delete_order':
            oid=request.form.get('order_id'); con.execute('DELETE FROM invoices WHERE order_id=?',(oid,)); con.execute('DELETE FROM orders WHERE id=?',(oid,)); con.commit(); msg='订单及关联发票已删除'
        elif act=='ticket_reply':
            tid=request.form.get('ticket_id'); reply=request.form.get('reply','').strip(); status=request.form.get('status','pending')
            if reply: con.execute('INSERT INTO ticket_messages(ticket_id,user_id,author_role,message) VALUES(?,?,?,?)',(tid,current_user()['id'],'admin',reply))
            con.execute("UPDATE tickets SET title=COALESCE(NULLIF(?,''),title),category=?,priority=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",(request.form.get('title',''),request.form.get('category','general'),request.form.get('priority','normal'),status,tid)); con.commit(); msg='工单已回复/更新'
        elif act=='delete_ticket':
            tid=request.form.get('ticket_id'); con.execute('DELETE FROM ticket_messages WHERE ticket_id=?',(tid,)); con.execute('DELETE FROM tickets WHERE id=?',(tid,)); con.commit(); msg='工单已删除'
        elif act=='clear_usage_logs':
            con.execute('DELETE FROM usage_logs'); con.execute('UPDATE users SET tokens_used_today=0,tokens_reset_date=?',(dt.date.today().isoformat(),)); con.commit(); msg='全部用量日志和今日用量已清理'
        elif act=='save_plan':
            pid=request.form.get('id','').strip()
            if re.match(r'^[a-zA-Z0-9_-]{2,32}$',pid): con.execute('INSERT INTO plans(id,name,price,daily_tokens,rate_limit,days,is_active,sort_order,description,updated_at) VALUES(?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET name=excluded.name,price=excluded.price,daily_tokens=excluded.daily_tokens,rate_limit=excluded.rate_limit,days=excluded.days,is_active=excluded.is_active,sort_order=excluded.sort_order,description=excluded.description,updated_at=CURRENT_TIMESTAMP',(pid,request.form.get('name',''),float(request.form.get('price') or 0),int(request.form.get('daily_tokens') or 0),int(request.form.get('rate_limit') or 60),int(request.form.get('days') or 30),1 if request.form.get('is_active')=='1' else 0,int(request.form.get('sort_order') or 100),request.form.get('description',''))); con.commit(); msg='套餐已保存'
            else: msg='套餐 ID 格式错误'
        elif act=='delete_plan':
            pid=request.form.get('id','')
            if pid!='free': con.execute('DELETE FROM plans WHERE id=?',(pid,)); con.commit(); msg='套餐已删除'
        elif act=='save_project':
            slug=request.form.get('slug','').strip()
            if re.match(r'^[a-zA-Z0-9_-]{2,64}$',slug):
                pid=request.form.get('project_id')
                if pid: con.execute('UPDATE managed_projects SET name=?,slug=?,description=?,base_url=?,status=?,sort_order=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(request.form.get('name',''),slug,request.form.get('description',''),request.form.get('base_url',''),request.form.get('status','active'),int(request.form.get('sort_order') or 100),pid))
                else: con.execute('INSERT INTO managed_projects(name,slug,description,base_url,status,sort_order) VALUES(?,?,?,?,?,?)',(request.form.get('name',''),slug,request.form.get('description',''),request.form.get('base_url',''),request.form.get('status','active'),int(request.form.get('sort_order') or 100)))
                con.commit(); msg='管理项目已保存'
            else: msg='项目 slug 格式错误'
        elif act=='delete_project': con.execute('DELETE FROM managed_projects WHERE id=?',(request.form.get('project_id'),)); con.commit(); msg='管理项目已删除'
        elif act=='bulk_models':
            # 按"上游同名"批量维护：一次操作该供应商名下所有模型行
            pname=request.form.get('provider_name','').strip()
            op=request.form.get('op','').strip()
            val=request.form.get('value','').strip()
            extra=request.form.get('extra','').strip()
            if not pname:
                msg='请先选择要维护的上游名称'
            elif op=='enable':
                cur=con.execute('UPDATE model_providers SET is_active=1,updated_at=CURRENT_TIMESTAMP WHERE name=?',(pname,)); con.commit(); msg='已启用「%s」的 %s 个模型' % (pname,cur.rowcount)
            elif op=='disable':
                cur=con.execute('UPDATE model_providers SET is_active=0,updated_at=CURRENT_TIMESTAMP WHERE name=?',(pname,)); con.commit(); msg='已禁用「%s」的 %s 个模型' % (pname,cur.rowcount)
            elif op=='delete':
                cur=con.execute('DELETE FROM model_providers WHERE name=?',(pname,)); con.commit(); msg='已删除「%s」的 %s 个模型' % (pname,cur.rowcount)
            elif op=='key':
                if not val: msg='请填写新的 API Key'
                else:
                    cur=con.execute('UPDATE model_providers SET api_key=?,updated_at=CURRENT_TIMESTAMP WHERE name=?',(val,pname)); con.commit(); msg='已把「%s」的 %s 个模型统一改为新 Key' % (pname,cur.rowcount)
            elif op=='base':
                if not val.startswith('http'): msg='Base URL 必须以 http 开头'
                else:
                    cur=con.execute('UPDATE model_providers SET base_url=?,updated_at=CURRENT_TIMESTAMP WHERE name=?',(val.rstrip('/'),pname)); con.commit(); msg='已把「%s」的 %s 个模型统一改为 %s' % (pname,cur.rowcount,val)
            elif op=='timeout':
                try: t=int(val)
                except ValueError: t=0
                if t<=0: msg='超时必须是正整数秒'
                else:
                    cur=con.execute('UPDATE model_providers SET timeout_seconds=?,updated_at=CURRENT_TIMESTAMP WHERE name=?',(t,pname)); con.commit(); msg='已把「%s」的 %s 个模型超时统一设为 %s 秒' % (pname,cur.rowcount,t)
            elif op=='rename':
                if not val: msg='请填写新的供应商名称'
                else:
                    cur=con.execute('UPDATE model_providers SET name=?,updated_at=CURRENT_TIMESTAMP WHERE name=?',(val,pname)); con.commit(); msg='已把「%s」重命名为「%s」，共 %s 个模型' % (pname,val,cur.rowcount)
            elif op=='default':
                con.execute('UPDATE model_providers SET is_default=0')
                if extra:
                    cur=con.execute('UPDATE model_providers SET is_default=1 WHERE name=? AND model_id=?',(pname,extra))
                    hit=cur.rowcount
                else:
                    row=con.execute('SELECT id FROM model_providers WHERE name=? ORDER BY sort_order ASC,id ASC LIMIT 1',(pname,)).fetchone()
                    hit=con.execute('UPDATE model_providers SET is_default=1 WHERE id=?',(row['id'],)).rowcount if row else 0
                con.commit(); msg=('已把「%s」的 %s 设为默认模型' % (pname,extra)) if hit else '没有找到要设为默认的模型'
            elif op=='ocr_off':
                cur=con.execute("UPDATE model_providers SET is_active=0,updated_at=CURRENT_TIMESTAMP WHERE name=? AND modalities LIKE '%ocr%' AND modalities NOT LIKE '%text%'",(pname,)); con.commit(); msg='已禁用「%s」下 %s 个纯 OCR 模型（聊天客户端无法调用它们）' % (pname,cur.rowcount)
            elif op=='resort':
                try: base_num=int(extra) if extra else 100
                except ValueError: base_num=100
                rows=con.execute('SELECT id FROM model_providers WHERE name=? ORDER BY sort_order ASC,id ASC',(pname,)).fetchall()
                for i,row in enumerate(rows): con.execute('UPDATE model_providers SET sort_order=? WHERE id=?',(base_num+i,row['id']))
                con.commit(); msg='已把「%s」的 %s 个模型排序重排为 %s 起连续编号' % (pname,len(rows),base_num)
            else:
                msg='未知的批量操作：%s' % op
        elif act=='save_model':
            mid=request.form.get('model_row_id')
            modalities=[]
            for m in ['text','image','video','audio']:
                if request.form.get('mod_'+m)=='1': modalities.append(m)
            if not modalities: modalities=['text']
            vals=(request.form.get('name',''),request.form.get('provider_type','openai'),request.form.get('base_url','').rstrip('/'),request.form.get('api_key',''),request.form.get('model_id',''),request.form.get('display_name',''),1 if request.form.get('is_default')=='1' else 0,1 if request.form.get('is_active')=='1' else 0,int(request.form.get('sort_order') or 100),int(request.form.get('timeout_seconds') or 300),request.form.get('endpoint_type','chat_completions'),json.dumps(modalities,ensure_ascii=False),1 if request.form.get('supports_stream')=='1' else 0,1 if request.form.get('supports_tools')=='1' else 0,int(request.form.get('max_input_tokens') or 0) or None,int(request.form.get('max_output_tokens') or 0) or None,request.form.get('extra_config','{}') or '{}')
            if vals[6]: con.execute('UPDATE model_providers SET is_default=0')
            if mid: con.execute('UPDATE model_providers SET name=?,provider_type=?,base_url=?,api_key=?,model_id=?,display_name=?,is_default=?,is_active=?,sort_order=?,timeout_seconds=?,endpoint_type=?,modalities=?,supports_stream=?,supports_tools=?,max_input_tokens=?,max_output_tokens=?,extra_config=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',vals+(mid,))
            else: con.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order,timeout_seconds,endpoint_type,modalities,supports_stream,supports_tools,max_input_tokens,max_output_tokens,extra_config) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',vals)
            con.commit(); msg='模型配置已保存'
        elif act=='discover_models':
            name=request.form.get('name','第三方模型').strip() or '第三方模型'; base_url=request.form.get('base_url','').rstrip('/'); api_key=request.form.get('api_key',''); timeout=int(request.form.get('timeout_seconds') or 20); sort_order=int(request.form.get('sort_order') or 100)
            try:
                specs=fetch_openai_model_specs(base_url,api_key,timeout); added=0
                for spec in specs:
                    mid=spec['id']; caps=infer_model_capabilities(mid,name,spec.get('meta') or {})
                    if not con.execute('SELECT 1 FROM model_providers WHERE provider_type=? AND base_url=? AND model_id=?',('openai',base_url,mid)).fetchone():
                        con.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order,timeout_seconds,endpoint_type,modalities,supports_stream,supports_tools,max_input_tokens,max_output_tokens) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(name,'openai',base_url,api_key,mid,mid,0,1,sort_order,300,'chat_completions',json.dumps(caps['modalities'],ensure_ascii=False),caps['supports_stream'],caps['supports_tools'],caps.get('max_input_tokens'),caps.get('max_output_tokens'))); added+=1
                con.commit(); msg=f'已读取到 {len(specs)} 个模型，新增 {added} 个；已存在的自动跳过；已按名称/元数据自动识别多模态能力'
            except Exception as e: msg='读取模型失败：'+str(e)
        elif act=='detect_model_caps':
            rows=con.execute('SELECT * FROM model_providers').fetchall(); changed=0; multi=0
            for row in rows:
                caps=infer_model_capabilities(row['model_id'], row['name'], {})
                mods=json.dumps(caps['modalities'],ensure_ascii=False)
                con.execute('UPDATE model_providers SET modalities=?,supports_stream=?,max_input_tokens=COALESCE(max_input_tokens,?),max_output_tokens=COALESCE(max_output_tokens,?),supports_vision=?,supports_video=? WHERE id=?',(mods,caps['supports_stream'],caps.get('max_input_tokens'),caps.get('max_output_tokens'),1 if 'image' in caps['modalities'] else 0,1 if 'video' in caps['modalities'] else 0,row['id']))
                changed+=1
                if len(caps['modalities'])>1: multi+=1
            con.commit(); msg=f'已重新识别 {changed} 个模型能力，其中疑似多模态 {multi} 个'
        elif act=='probe_models':
            _ok,_info=start_probe_models(); msg=_info
        elif act=='reset_model_stats':
            reset_model_stats(); msg='已清空全部速度统计，auto 将回到默认顺序（可重新测速）'
        elif act=='delete_model': con.execute('DELETE FROM model_providers WHERE id=?',(request.form.get('model_row_id'),)); con.commit(); msg='模型配置已删除'
    st=get_settings(); pay_missing=[]
    if st.get('payment_enabled')!='1': pay_missing.append('未启用')
    if not (st.get('epay_api_url') or '').strip(): pay_missing.append('易支付网关为空')
    if not (st.get('epay_pid') or '').strip(): pay_missing.append('PID为空')
    if not (st.get('epay_key') or '').strip(): pay_missing.append('Key为空')
    pay_status='<span class="ok">已启用，配置完整</span>' if not pay_missing else '<span class="bad">演示模式：'+h('；'.join(pay_missing))+'</span>'
    def yn(name,val): return f'<select name="{name}" class="input mini"><option value="1" {sel(val,1)}>启用</option><option value="0" {sel(val,0)}>禁用</option></select>'
    def plan_opts(cur): return ''.join([f'<option value="{h(pid)}" {sel(cur,pid)}>{h(pid)}</option>' for pid in get_plan_config(True).keys()])
    users=con.execute('SELECT * FROM users ORDER BY id DESC LIMIT 100').fetchall(); keys=con.execute('SELECT k.*,u.username FROM api_keys k JOIN users u ON u.id=k.user_id ORDER BY k.id DESC LIMIT 100').fetchall(); orders=con.execute('SELECT o.*,u.username FROM orders o JOIN users u ON u.id=o.user_id ORDER BY o.id DESC LIMIT 100').fetchall(); tickets=con.execute('SELECT t.*,u.username FROM tickets t JOIN users u ON u.id=t.user_id ORDER BY t.id DESC LIMIT 50').fetchall(); plans_rows=con.execute('SELECT * FROM plans ORDER BY sort_order ASC,price ASC,id ASC').fetchall(); projects=con.execute('SELECT * FROM managed_projects ORDER BY sort_order ASC,id DESC').fetchall(); model_rows=con.execute('SELECT * FROM model_providers ORDER BY is_default DESC,sort_order ASC,id ASC').fetchall(); usage=con.execute('SELECT u.username,sum(l.tokens_input+l.tokens_output) total_tokens,count(*) calls,max(l.created_at) last_at FROM usage_logs l JOIN users u ON u.id=l.user_id GROUP BY l.user_id ORDER BY total_tokens DESC LIMIT 20').fetchall()
    uh=''.join([f'<tr><td>{x["id"]}</td><td><form method="post"><input type="hidden" name="act" value="update_user"><input type="hidden" name="user_id" value="{x["id"]}"><input class="input mini" name="username" value="{h(x["username"])}"><input class="input" name="email" value="{h(x["email"])}"><input class="input" type="password" name="new_password" placeholder="新密码，留空不改"></td><td><select name="plan" class="input mini">{plan_opts(x["plan"])}</select></td><td><input class="input mini" name="daily_token_limit" value="{h(x["daily_token_limit"] or "")}" placeholder="tokens/日"><br><input class="input mini" name="custom_rate_limit" value="{h(x["custom_rate_limit"] or "")}" placeholder="RPM"><br><input class="input mini" name="max_api_keys" value="{h(x["max_api_keys"] or 5)}" placeholder="Key数量"></td><td><input class="input" name="plan_expires_at" value="{h(x["plan_expires_at"] or "")}"></td><td><input class="input mini" name="balance" value="{h(x["balance"])}"></td><td>{yn("is_active",x["is_active"])}{yn("is_admin",x["is_admin"])}</td><td><input class="input mini" name="tokens_used_today" value="{h(x["tokens_used_today"])}"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="reset_user_usage"><input type="hidden" name="user_id" value="{x["id"]}"><button class="btn btn2">清用量</button></form><form method="post"><input type="hidden" name="act" value="delete_user"><input type="hidden" name="user_id" value="{x["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>' for x in users])
    kh=''.join([f'<tr><td>{k["id"]}</td><td>{h(k["username"])}</td><td><form method="post"><input type="hidden" name="act" value="update_key"><input type="hidden" name="key_id" value="{k["id"]}"><input class="input mini" name="name" value="{h(k["name"])}"><br><span class="muted">{h(k["key_prefix"])}...</span></td><td>{yn("is_active",k["is_active"])}</td><td><input class="input mini" name="rate_limit" value="{h(k["rate_limit"])}"></td><td><input class="input mini" name="quota_daily" value="{h(k["quota_daily"] or "")}"></td><td><input class="input" name="allowed_ips" value="{h(k["allowed_ips"] or "")}"></td><td>{h(k["last_used_at"] or "-")}</td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="delete_key"><input type="hidden" name="key_id" value="{k["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>' for k in keys]) or '<tr><td colspan="9">暂无 API Key</td></tr>'
    oh=''.join([f'<tr><td>{o["id"]}</td><td>{h(o["order_no"])}</td><td>{h(o["username"])}</td><td><form method="post"><input type="hidden" name="act" value="update_order"><input type="hidden" name="order_id" value="{o["id"]}"><input class="input mini" name="plan_id" value="{h(o["plan_id"])}"></td><td><input class="input mini" name="amount" value="{h(o["amount"])}"></td><td><select class="input mini" name="payment_method"><option value="alipay" {sel(o["payment_method"],"alipay")}>支付宝</option><option value="wxpay" {sel(o["payment_method"],"wxpay")}>微信</option><option value="manual" {sel(o["payment_method"],"manual")}>手动</option></select></td><td><input class="input mini" name="trade_no" value="{h(o["trade_no"] or "")}"></td><td><select class="input mini" name="status"><option value="pending" {sel(o["status"],"pending")}>pending</option><option value="paid" {sel(o["status"],"paid")}>paid</option><option value="cancelled" {sel(o["status"],"cancelled")}>cancelled</option><option value="refunded" {sel(o["status"],"refunded")}>refunded</option></select></td><td><input class="input" name="paid_at" value="{h(o["paid_at"] or "")}"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="mark_paid"><input type="hidden" name="order_id" value="{o["id"]}"><button class="btn btn2">确认支付</button></form><form method="post"><input type="hidden" name="act" value="delete_order"><input type="hidden" name="order_id" value="{o["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>' for o in orders]) or '<tr><td colspan="10">暂无订单</td></tr>'
    th=[]
    for t in tickets:
        msgs=con.execute('SELECT * FROM ticket_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 3',(t['id'],)).fetchall(); mh=''.join([f'<p><span class="pill">{h(m["author_role"])}</span> {h(m["message"])}</p>' for m in reversed(msgs)])
        opts=''.join([f'<option value="{s}" {sel(t["status"],s)}>{s}</option>' for s in ['open','pending','closed']])
        th.append(f'<tr><td>{t["id"]}</td><td>{h(t["username"])}</td><td><form method="post"><input type="hidden" name="act" value="ticket_reply"><input type="hidden" name="ticket_id" value="{t["id"]}"><input class="input" name="title" value="{h(t["title"])}"><br><span class="muted">{h(t["content"] or "")}</span>{mh}</td><td><input class="input mini" name="category" value="{h(t["category"])}"></td><td><input class="input mini" name="priority" value="{h(t["priority"])}"></td><td><select name="status" class="input mini">{opts}</select><textarea class="input" name="reply" placeholder="管理员回复"></textarea><button class="btn">保存/回复</button></form><form method="post"><input type="hidden" name="act" value="delete_ticket"><input type="hidden" name="ticket_id" value="{t["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>')
    th=''.join(th) or '<tr><td colspan="6">暂无工单</td></tr>'
    plan_html=''.join([f'<tr><td><form method="post"><input type="hidden" name="act" value="save_plan"><input class="input mini" name="id" value="{h(p["id"])}"></td><td><input class="input mini" name="name" value="{h(p["name"])}"></td><td><input class="input mini" name="price" value="{h(p["price"])}"></td><td><input class="input mini" name="daily_tokens" value="{h(p["daily_tokens"])}"></td><td><input class="input mini" name="rate_limit" value="{h(p["rate_limit"])}"></td><td><input class="input mini" name="days" value="{h(p["days"])}"></td><td>{yn("is_active",p["is_active"])}</td><td><input class="input mini" name="sort_order" value="{h(p["sort_order"])}"></td><td><input class="input" name="description" value="{h(p["description"] or "")}"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="delete_plan"><input type="hidden" name="id" value="{h(p["id"])}"><button class="btn btn-danger">删除</button></form></td></tr>' for p in plans_rows])
    new_plan='<tr><td><form method="post"><input type="hidden" name="act" value="save_plan"><input class="input mini" name="id" placeholder="plan_id"></td><td><input class="input mini" name="name"></td><td><input class="input mini" name="price" value="0"></td><td><input class="input mini" name="daily_tokens" value="10000"></td><td><input class="input mini" name="rate_limit" value="60"></td><td><input class="input mini" name="days" value="30"></td><td><select name="is_active" class="input mini"><option value="1">启用</option><option value="0">禁用</option></select></td><td><input class="input mini" name="sort_order" value="100"></td><td><input class="input" name="description"></td><td><button class="btn">新增</button></form></td></tr>'
    proj_html=''.join([f'<tr><td>{p["id"]}<form method="post"><input type="hidden" name="act" value="save_project"><input type="hidden" name="project_id" value="{p["id"]}"></td><td><input class="input mini" name="name" value="{h(p["name"])}"></td><td><input class="input mini" name="slug" value="{h(p["slug"])}"></td><td><input class="input" name="description" value="{h(p["description"] or "")}"></td><td><input class="input" name="base_url" value="{h(p["base_url"] or "")}"></td><td><input class="input mini" name="status" value="{h(p["status"])}"></td><td><input class="input mini" name="sort_order" value="{h(p["sort_order"])}"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="delete_project"><input type="hidden" name="project_id" value="{p["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>' for p in projects])
    new_proj='<tr><td>新建<form method="post"><input type="hidden" name="act" value="save_project"></td><td><input class="input mini" name="name"></td><td><input class="input mini" name="slug"></td><td><input class="input" name="description"></td><td><input class="input" name="base_url"></td><td><input class="input mini" name="status" value="active"></td><td><input class="input mini" name="sort_order" value="100"></td><td><button class="btn">新增</button></form></td></tr>'
    def mod_checked(row,mod): return 'checked' if mod in get_provider_modalities(row) else ''
    def endpoint_opts(cur):
        return ''.join([f'<option value="{v}" {sel(cur,v)}>{label}</option>' for v,label in [('chat_completions','OpenAI Chat'),('responses','OpenAI Responses'),('anthropic_messages','Anthropic Messages')]])
    model_html=''.join([f'''<tr><td>{m["id"]}<form method="post"><input type="hidden" name="act" value="save_model"><input type="hidden" name="model_row_id" value="{m["id"]}"></td><td><input class="input mini" name="name" value="{h(m["name"])}"><br><select class="input mini" name="endpoint_type">{endpoint_opts(get_provider_endpoint_type(m))}</select></td><td><select class="input mini" name="provider_type"><option value="openai" {sel(m["provider_type"],"openai")}>HTTP API</option><option value="ollama" {sel(m["provider_type"],"ollama")}>Ollama</option></select></td><td><input class="input" name="base_url" value="{h(m["base_url"])}" placeholder="https://api.xxx/v1"><br><input class="input" name="extra_config" value="{h(m["extra_config"] or '{}')}" placeholder='{{"anthropic_version":"2023-06-01"}}'></td><td><input class="input" name="api_key" value="{h(m["api_key"])}"></td><td><input class="input mini" name="model_id" value="{h(m["model_id"])}"><br><input class="input mini" name="display_name" value="{h(m["display_name"] or "")}"></td><td>{yn("is_default",m["is_default"])}{yn("is_active",m["is_active"])}</td><td><label><input type="checkbox" name="mod_text" value="1" {mod_checked(m,'text')}>文</label><label><input type="checkbox" name="mod_image" value="1" {mod_checked(m,'image')}>图</label><label><input type="checkbox" name="mod_video" value="1" {mod_checked(m,'video')}>视频</label><label><input type="checkbox" name="mod_audio" value="1" {mod_checked(m,'audio')}>音频</label><br>{yn("supports_stream",m["supports_stream"])}<span class="muted">stream</span>{yn("supports_tools",m["supports_tools"])}<span class="muted">tools</span></td><td><input class="input mini" name="sort_order" value="{h(m["sort_order"])}"><input class="input mini" name="timeout_seconds" value="{h(m["timeout_seconds"])}"><input class="input mini" name="max_input_tokens" value="{h(m["max_input_tokens"] or "")}" placeholder="输入token"><input class="input mini" name="max_output_tokens" value="{h(m["max_output_tokens"] or "")}" placeholder="输出token"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="delete_model"><input type="hidden" name="model_row_id" value="{m["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>''' for m in model_rows])
    new_model='''<tr><td>新建<form method="post"><input type="hidden" name="act" value="save_model"></td><td><input class="input mini" name="name" placeholder="供应商名"><br><select class="input mini" name="endpoint_type"><option value="chat_completions">OpenAI Chat</option><option value="responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option></select></td><td><select class="input mini" name="provider_type"><option value="openai">HTTP API</option><option value="ollama">Ollama</option></select></td><td><input class="input" name="base_url" placeholder="https://api.xxx/v1"><br><input class="input" name="extra_config" value="{}"></td><td><input class="input" name="api_key"></td><td><input class="input mini" name="model_id" placeholder="gpt-4o-mini"><br><input class="input mini" name="display_name"></td><td><select name="is_default" class="input mini"><option value="0">默认否</option><option value="1">默认是</option></select><select name="is_active" class="input mini"><option value="1">启用</option><option value="0">禁用</option></select></td><td><label><input type="checkbox" name="mod_text" value="1" checked>文</label><label><input type="checkbox" name="mod_image" value="1">图</label><label><input type="checkbox" name="mod_video" value="1">视频</label><label><input type="checkbox" name="mod_audio" value="1">音频</label><br><select name="supports_stream" class="input mini"><option value="1">stream开</option><option value="0">stream关</option></select><select name="supports_tools" class="input mini"><option value="0">tools关</option><option value="1">tools开</option></select></td><td><input class="input mini" name="sort_order" value="100"><input class="input mini" name="timeout_seconds" value="300"><input class="input mini" name="max_input_tokens" placeholder="输入token"><input class="input mini" name="max_output_tokens" placeholder="输出token"></td><td><button class="btn">新增</button></form></td></tr>'''
    discover_form='''<form method="post"><input type="hidden" name="act" value="discover_models"><div class="grid"><div><label>供应商名称</label><input class="input" name="name" placeholder="例如：硅基流动/自建 NewAPI"></div><div><label>Base URL</label><input class="input" name="base_url" placeholder="https://api.xxx/v1"></div><div><label>API Key</label><input class="input" name="api_key" placeholder="sk-..."></div><div><label>排序</label><input class="input" name="sort_order" value="100"></div><div><label>读取超时秒</label><input class="input" name="timeout_seconds" value="20"></div></div><button class="btn btn2">通过 /v1/models 自动读取并批量导入</button></form><form method="post" style="margin-top:8px"><input type="hidden" name="act" value="detect_model_caps"><button class="btn btn2">一键重新识别全部模型多模态能力</button><span class="muted">按上游元数据和模型名规则识别，不消耗对话额度。</span></form>'''
    usage_html=''.join([f'<tr><td>{h(u["username"])}</td><td>{u["calls"]}</td><td>{u["total_tokens"]}</td><td>{h(u["last_at"])}</td></tr>' for u in usage]) or '<tr><td colspan="4">暂无调用日志</td></tr>'
    # ---- 按上游名称汇总，支持一次性批量维护该上游下所有模型行 ----
    prov_agg={}
    for r in con.execute('SELECT name,provider_type,base_url,api_key,count(*) n,sum(is_active) a,sum(is_default) d FROM model_providers GROUP BY name,provider_type,base_url,api_key'):
        g=prov_agg.setdefault(r['name'],{'n':0,'a':0,'d':0,'types':set(),'bases':[],'keys':[]})
        g['n']+=r['n']; g['a']+=r['a'] or 0; g['d']+=r['d'] or 0
        g['types'].add(r['provider_type'])
        if r['base_url'] and r['base_url'] not in g['bases']: g['bases'].append(r['base_url'])
        if r['api_key'] and r['api_key'] not in g['keys']: g['keys'].append(r['api_key'])
    _bulk=[]
    for nm,g in sorted(prov_agg.items(), key=lambda kv:(-kv[1]['n'], kv[0])):
        bases='<br>'.join('<span class="key-line">%s</span>'%h(b) for b in g['bases'][:2])+(('<br><span class="muted">…共 %s 个不同地址</span>'%len(g['bases'])) if len(g['bases'])>2 else '')
        keyinfo=('<br>'.join(h(k[:8]+'…'+k[-4:]) for k in g['keys'][:1])) or '<span class="muted">-</span>'
        _bulk.append(f'''<tr><td><b>{h(nm)}</b><br><span class="muted">{h('/'.join(sorted(g['types'])))}</span></td><td>{g['n']}</td><td>{g['a']}</td><td>{g['d']}</td><td>{bases}</td><td>{keyinfo}</td><td><form method="post" class="inline-form"><input type="hidden" name="act" value="bulk_models"><input type="hidden" name="provider_name" value="{h(nm)}"><select name="op" class="input mini"><option value="enable">启用全部</option><option value="disable">禁用全部</option><option value="delete">删除全部</option><option value="ocr_off">禁用纯OCR模型</option><option value="default">设为默认模型</option><option value="key">统一改 API Key</option><option value="base">统一改 Base URL</option><option value="timeout">统一改超时秒</option><option value="resort">重排排序号</option><option value="rename">重命名该上游</option></select><input class="input mini" name="value" placeholder="新值（Key/Base/超时/新名称）"><input class="input mini" name="extra" placeholder="可选：默认模型ID / 排序基准"><button class="btn btn2">执行</button></form></td></tr>''')
    bulk_html=''.join(_bulk) or '<tr><td colspan="7">暂无模型供应商</td></tr>'
    bulk_section=f'''<h3>按上游批量维护</h3><p class="muted">同一"上游名称"下的模型会被一起处理：换 Key、换 Base URL、批量启停、重命名、重排排序，不用再逐行点保存。</p>
<table width="100%"><tr><th>上游</th><th>模型数</th><th>启用</th><th>默认</th><th>Base URL</th><th>Key</th><th>批量操作</th></tr>{bulk_html}</table>'''
    # ---- 模型实测速度面板（auto 模式的排序依据）----
    _srows=con.execute("SELECT p.id,p.name,p.model_id,p.provider_type,p.endpoint_type,COALESCE(s.ok_count,0) ok,COALESCE(s.fail_count,0) bad,COALESCE(s.avg_ms,0) avg,COALESCE(s.fail_streak,0) streak,s.last_error err FROM model_providers p LEFT JOIN model_stats s ON s.provider_id=p.id WHERE p.is_active=1").fetchall()
    _tested=sorted([r for r in _srows if int(r['ok'] or 0)>0 and float(r['avg'] or 0)>0], key=lambda r: float(r['avg']))
    fast_rows=''.join(f'<tr><td>{i}</td><td>{h(r["model_id"])}</td><td>{h(r["name"])}</td><td><b>{int(float(r["avg"]))} ms</b></td><td>{int(r["ok"])}/{int(r["bad"])}</td></tr>' for i,r in enumerate(_tested[:12],1)) or '<tr><td colspan="5" class="muted">还没有测速数据，点下方「开始测速」</td></tr>'
    _agg={}
    for r in _tested:
        a=_agg.setdefault(r['name'],{'n':0,'sum':0.0,'min':1e9,'ok':0,'bad':0})
        a['n']+=1; a['sum']+=float(r['avg']); a['min']=min(a['min'],float(r['avg'])); a['ok']+=int(r['ok'] or 0); a['bad']+=int(r['bad'] or 0)
    up_rows=''.join(f'<tr><td>{h(nm)}</td><td>{a["n"]}</td><td>{int(a["sum"]/a["n"])} ms</td><td>{int(a["min"])} ms</td><td>{a["ok"]}/{a["bad"]}</td></tr>' for nm,a in sorted(_agg.items(), key=lambda kv: kv[1]['sum']/kv[1]['n'])) or '<tr><td colspan="5" class="muted">暂无数据</td></tr>'
    _bad=[r for r in _srows if int(r['streak'] or 0)>=2 or (int(r['bad'] or 0)>0 and int(r['ok'] or 0)==0)]
    bad_rows=''.join(f'<tr><td>{h(r["model_id"])}</td><td>{h(r["name"])}</td><td>{int(r["ok"] or 0)}/{int(r["bad"] or 0)}</td><td class="muted">{h((r["err"] or "")[:90])}</td></tr>' for r in _bad[:15]) or '<tr><td colspan="4" class="muted">暂无连续失败的模型</td></tr>'
    ps=PROBE_STATE; prog=''
    if ps.get('running'):
        pct=int(100*ps['done']/ps['total']) if ps.get('total') else 0
        prog=f'<p class="ok">测速进行中：{ps["done"]}/{ps["total"]}（成功 {ps["ok"]} · 失败 {ps["fail"]}），开始于 {h(ps["started_at"])}，刷新页面看进度</p><div style="height:8px;background:var(--line);border-radius:6px;overflow:hidden;margin:6px 0 10px"><div style="height:8px;width:{pct}%;background:linear-gradient(90deg,var(--primary),var(--primary2))"></div></div>'
    elif ps.get('finished_at'):
        prog=f'<p class="muted">上次测速：{h(ps["started_at"])} → {h(ps["finished_at"])}，共 {ps["total"]} 个（成功 {ps["ok"]} · 失败 {ps["fail"]}）</p>'
    auto_pick=''
    try:
        _cand=auto_sorted_rows(active_model_rows()); _sm=model_stats_map()
        if _cand:
            _st=_sm.get(int(_cand[0]['id'])); _ms=int(float(_st['avg_ms'])) if (_st and _st['avg_ms']) else None
            auto_pick=f'<p>当前 auto 首选：<b>{h(_cand[0]["model_id"])}</b>（{h(_cand[0]["name"])}{("，实测约 %d ms"%_ms) if _ms else "，暂无测速数据"}）· 排序方式 <code>{h(AUTO_SORT_MODE)}</code></p>'
    except Exception: pass
    probe_section=f'''<h3>模型测速 / auto 智能选路</h3>
<p class="muted">「开始测速」会对所有启用的模型各发一次最小请求（不传 temperature，避免部分模型只接受 temperature=1 而误判），记录真实响应耗时并写入统计。之后 <code>model:"auto"</code> 会<b>按实测速度优先挑最快的模型</b>；连续失败的自动排到最后，从没测过的给中性分（仍有机会被选中）。专用模型（embed/rerank/guard/safety/translate/detector/moderation/parse，可用 <code>AUTO_EXCLUDE_PATTERNS</code> 改）会被降权，不当首选。设 <code>AUTO_SORT_MODE=manual</code> 可改回手工排序。</p>
{auto_pick}{prog}
<div style="display:flex;gap:10px;flex-wrap:wrap;margin:8px 0 14px">
<form method="post"><input type="hidden" name="act" value="probe_models"><button class="btn">开始测速（全部启用模型）</button></form>
<form method="post"><input type="hidden" name="act" value="reset_model_stats"><button class="btn btn-danger">清空速度统计</button></form>
</div>
<div class="grid"><div><h4>最快的 12 个模型</h4><table width="100%"><tr><th>#</th><th>模型</th><th>上游</th><th>平均耗时</th><th>成功/失败</th></tr>{fast_rows}</table></div>
<div><h4>上游平均耗时</h4><table width="100%"><tr><th>上游</th><th>已测模型</th><th>平均</th><th>最快</th><th>成功/失败</th></tr>{up_rows}</table></div></div>
<h4>连续失败 / 从未成功的模型（建议在下方批量维护里禁用）</h4><table width="100%"><tr><th>模型</th><th>上游</th><th>成功/失败</th><th>最近错误</th></tr>{bad_rows}</table>'''
    body=f'''<div class="card"><h2>管理后台</h2>{'<p class="ok">'+h(msg)+'</p>' if msg else ''}
<h3>易支付配置</h3><p>当前支付状态：{pay_status}</p><form method="post"><input type="hidden" name="act" value="save_epay"><div class="grid"><div><label>启用支付</label><select class="input" name="payment_enabled"><option value="0" {sel(st.get('payment_enabled'),'0')}>禁用/演示</option><option value="1" {sel(st.get('payment_enabled'),'1')}>启用</option></select></div><div><label>易支付网关</label><input class="input" name="epay_api_url" value="{h(st.get('epay_api_url',''))}" placeholder="https://epay.example.com"></div><div><label>商户 PID</label><input class="input" name="epay_pid" value="{h(st.get('epay_pid',''))}"></div><div><label>商户 Key</label><input class="input" name="epay_key" value="{h(st.get('epay_key',''))}"></div><div><label>域名</label><input class="input" name="domain" value="{h(st.get('domain',''))}"></div><div><label>公网 Base URL</label><input class="input" name="public_base_url" value="{h(st.get('public_base_url',''))}"></div></div><button class="btn">保存易支付配置</button></form>
<h3>SMTP 邮件配置</h3><form method="post"><input type="hidden" name="act" value="save_smtp"><div class="grid"><div><label>启用 SMTP</label><select class="input" name="smtp_enabled"><option value="0" {sel(st.get('smtp_enabled'),'0')}>禁用</option><option value="1" {sel(st.get('smtp_enabled'),'1')}>启用</option></select></div><div><label>SMTP Host</label><input class="input" name="smtp_host" value="{h(st.get('smtp_host',''))}" placeholder="smtp.example.com"></div><div><label>端口</label><input class="input" name="smtp_port" value="{h(st.get('smtp_port','587'))}"></div><div><label>加密</label><select class="input" name="smtp_encryption"><option value="tls" {sel(st.get('smtp_encryption'),'tls')}>TLS/STARTTLS</option><option value="ssl" {sel(st.get('smtp_encryption'),'ssl')}>SSL</option><option value="none" {sel(st.get('smtp_encryption'),'none')}>不加密</option></select></div><div><label>账号</label><input class="input" name="smtp_username" value="{h(st.get('smtp_username',''))}"></div><div><label>密码/授权码</label><input class="input" name="smtp_password" value="{h(st.get('smtp_password',''))}"></div><div><label>发件邮箱</label><input class="input" name="smtp_from_email" value="{h(st.get('smtp_from_email',''))}"></div><div><label>发件名称</label><input class="input" name="smtp_from_name" value="{h(st.get('smtp_from_name','LLM Platform'))}"></div></div><button class="btn">保存 SMTP 配置</button></form><form method="post"><input type="hidden" name="act" value="test_smtp"><input type="hidden" name="smtp_enabled" value="{h(st.get('smtp_enabled','0'))}"><input type="hidden" name="smtp_host" value="{h(st.get('smtp_host',''))}"><input type="hidden" name="smtp_port" value="{h(st.get('smtp_port','587'))}"><input type="hidden" name="smtp_username" value="{h(st.get('smtp_username',''))}"><input type="hidden" name="smtp_password" value="{h(st.get('smtp_password',''))}"><input type="hidden" name="smtp_encryption" value="{h(st.get('smtp_encryption','tls'))}"><input type="hidden" name="smtp_from_email" value="{h(st.get('smtp_from_email',''))}"><input type="hidden" name="smtp_from_name" value="{h(st.get('smtp_from_name','LLM Platform'))}"><div class="grid"><div><label>测试收件邮箱</label><input class="input" name="test_email" value="{h(current_user()['email'] or '')}"></div></div><button class="btn btn2">发送测试邮件</button></form>
{openai_compat_docs_html(st.get('public_base_url') or PUBLIC_BASE_URL)}{bulk_section}{probe_section}<h3>模型配置管理（三方 OpenAI 兼容/Ollama 中转）</h3><p class="muted">OpenAI 兼容供应商支持填写 Base URL + API Key 后自动请求 <code>/models</code> 批量导入模型 ID；多模态能力请按上游真实能力勾选，网关会据此做路由过滤。</p>{discover_form}<table width="100%"><tr><th>ID</th><th>名称/协议</th><th>类型</th><th>Base URL/扩展</th><th>API Key</th><th>模型ID/显示名</th><th>状态</th><th>能力</th><th>排序/超时/Token</th><th>操作</th></tr>{model_html}{new_model}</table>
<h3>套餐 CRUD</h3><table width="100%"><tr><th>ID</th><th>名称</th><th>价格</th><th>日额度</th><th>RPM</th><th>天数</th><th>状态</th><th>排序</th><th>说明</th><th>操作</th></tr>{plan_html}{new_plan}</table>
<h3>管理项目 CRUD</h3><table width="100%"><tr><th>ID</th><th>名称</th><th>Slug</th><th>说明</th><th>链接</th><th>状态</th><th>排序</th><th>操作</th></tr>{proj_html}{new_proj}</table>
<h3>用户 / 套餐 / 额度</h3><table width="100%"><tr><th>ID</th><th>用户</th><th>套餐</th><th>自定义额度 / Key数</th><th>到期</th><th>余额</th><th>状态</th><th>今日已用</th><th>操作</th></tr>{uh}</table>
<h3>API Key 管理</h3><table width="100%"><tr><th>ID</th><th>用户</th><th>Key</th><th>状态</th><th>RPM</th><th>Key日额度</th><th>IP白名单</th><th>最后使用</th><th>操作</th></tr>{kh}</table>
<h3>订单管理</h3><table width="100%"><tr><th>ID</th><th>订单号</th><th>用户</th><th>套餐</th><th>金额</th><th>方式</th><th>交易号</th><th>状态</th><th>支付时间</th><th>操作</th></tr>{oh}</table>
<h3>工单管理</h3><table width="100%"><tr><th>ID</th><th>用户</th><th>内容/最近消息</th><th>分类</th><th>优先级</th><th>状态/回复</th></tr>{th}</table>
<h3>用量排行</h3><form method="post"><input type="hidden" name="act" value="clear_usage_logs"><button class="btn btn-danger">清理全部调用日志/重置今日用量</button></form><table width="100%"><tr><th>用户</th><th>调用次数</th><th>总 Tokens</th><th>最后调用</th></tr>{usage_html}</table></div>'''
    return page(body)

@app.route('/health')
def health():
    ollama=False; models=[]
    try:
        r=requests.get(f'{OLLAMA_BASE_URL}/api/tags',timeout=1.5); ollama=r.ok; models=[m.get('name') for m in r.json().get('models',[])] if r.ok else []
    except Exception: pass
    providers=[{'id':m['id'],'name':m['name'],'type':m['provider_type'],'model':m['model_id'],'active':bool(m['is_active']),'default':bool(m['is_default'])} for m in db().execute('SELECT * FROM model_providers ORDER BY is_default DESC,sort_order ASC,id ASC').fetchall()]
    return jsonify({'ok':True,'db':os.path.exists(DB_PATH),'ollama':ollama,'model':MODEL_NAME,'ollama_models':models,'model_providers':providers})

@app.route('/k',methods=['GET','POST'])
def key_path_helper():
    """直接访问 /k/<key>（URL 里没带端点路径）时的提示。"""
    info={'ok':True,
        'why':'部分 CDN / 反代会丢弃 Authorization 头，把 API Key 放进 URL 路径即可绕过',
        'base_url_examples':['https://<host>/k/<API_KEY>/v1','https://<host>/k/<API_KEY>'],
        'endpoints':['/k/<API_KEY>/v1/chat/completions','/k/<API_KEY>/v1/models','/k/<API_KEY>/v1/responses','/k/<API_KEY>/v1/messages'],
        'other_ways':['请求头 X-API-Key / api-key','查询串 ?api_key=<API_KEY>']}
    if request.method=='POST':
        info['ok']=False
        info['hint']='请求路径缺少端点。请把 base_url 设为 https://<host>/k/<API_KEY>/v1（客户端会自动补 /chat/completions）'
        return jsonify(info),400
    return jsonify(info)

@app.route('/v1/models')
def models():
    if (os.environ.get('MODELS_REQUIRE_AUTH') or '').strip().lower() in ('1','true','yes','on') and not api_auth()[0]:
        return jsonify({'error':{'message':'Unauthorized: missing/invalid API key','type':'auth_error'}}),401
    data=[]
    for m in active_model_rows(): data.append({'id':m['display_name'] or m['model_id'],'object':'model','created':int(time.time()),'owned_by':m['name'],'provider_type':m['provider_type'],'endpoint_type':get_provider_endpoint_type(m),'capabilities':{'modalities':sorted(get_provider_modalities(m)),'stream':bool(m['supports_stream']),'tools':bool(m['supports_tools']),'max_input_tokens':m['max_input_tokens'],'max_output_tokens':m['max_output_tokens']}})
    resp=jsonify({'object':'list','data':data})
    resp.headers['Cache-Control']='no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma']='no-cache'
    return resp

def run_gateway_request(payload, target_api='chat_completions'):
    key,_=api_auth()
    if not key:
        body={'error':{'message':'Unauthorized: missing/invalid Bearer API key','type':'auth_error'}}
        if os.environ.get('AUTH_DEBUG'):
            cands=api_key_candidates()
            dbg=[{'sha256':hashlib.sha256(c.encode()).hexdigest()[:16],'len':len(c)} for c in cands]
            body['error']['auth_debug']={'candidates':dbg or '(no api key found in Authorization / X-API-Key / query)'}
        return jsonify(body),401
    messages=payload.get('messages') if 'messages' in payload else responses_input_to_chat_messages(payload.get('input',''))
    input_tokens=token_count(messages)
    quota_err=check_quota_and_reset(key,input_tokens)
    if quota_err: return quota_err
    start=time.time()
    requested=payload.get('model')
    raw_candidates=candidate_model_rows(requested)
    if not raw_candidates: return jsonify({'error':{'message':'No active model provider configured','type':'model_error','code':'unsupported_model'}}),502
    endpoint_types=None
    if target_api=='messages':
        # Try anthropic_messages models first, fall back to chat_completions for auto/any model
        candidates=filter_candidates_by_capability(raw_candidates,payload,{'anthropic_messages'})
        if not candidates:
            candidates=filter_candidates_by_capability(raw_candidates,payload,{'chat_completions'})
    else:
        candidates=filter_candidates_by_capability(raw_candidates,payload,endpoint_types)
    if not candidates:
        return jsonify({'error':{'message':'No provider matches requested model/protocol/capabilities','type':'capability_error','code':'unsupported_modality','required_modalities':sorted(detect_modalities_from_payload(payload))}}),400
    # Streaming responses cannot be safely retried after bytes may have been sent to the client.
    if payload.get('stream'): candidates=candidates[:1]
    errors=[]
    for provider in candidates:
        t_try=time.time()
        try:
            resp=proxy_provider(provider,payload,key,input_tokens,start,target_api)
            record_model_result(provider['id'],True,round((time.time()-t_try)*1000))
            return resp
        except Exception as e:
            record_model_result(provider['id'],False,round((time.time()-t_try)*1000),str(e)[:200])
            errors.append({'provider':provider['name'],'model':provider['model_id'],'type':provider['provider_type'],'endpoint_type':get_provider_endpoint_type(provider),'error':str(e)[:500]})
            continue
    provider=candidates[0]
    if os.environ.get('DEMO_FALLBACK','0')=='1' and target_api=='chat_completions':
        content='演示模式：平台网关、用户系统、API Key 鉴权、套餐限额都已正常工作；当前所有模型上游不可用，所以这里返回模拟回复。收到的问题：'+content_text(messages[-1].get('content','') if messages else '')
        out_tokens=max(1,len(content)//2); model_id=(provider['model_id'] if provider else MODEL_NAME)+'-demo'
        record_usage(key,input_tokens,out_tokens,model_id,'/v1/chat/completions',start)
        return jsonify({'id':'chatcmpl-demo','object':'chat.completion','created':int(time.time()),'model':model_id,'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':input_tokens,'completion_tokens':out_tokens,'total_tokens':input_tokens+out_tokens},'fallback_errors':errors})
    return jsonify({'error':{'message':'All model providers failed','type':'gateway_error','code':'provider_error','fallback_errors':errors}}),502

@app.route('/v1/chat/completions',methods=['POST'])
def chat_completions():
    payload=request.get_json(force=True,silent=True) or {}
    return run_gateway_request(payload,'chat_completions')

@app.route('/v1/responses',methods=['POST'])
def responses_api():
    payload=request.get_json(force=True,silent=True) or {}
    return run_gateway_request(payload,'responses')

@app.route('/v1/messages',methods=['POST'])
def messages_api():
    payload=request.get_json(force=True,silent=True) or {}
    return run_gateway_request(payload,'messages')

init_db()

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5088')),debug=False)
