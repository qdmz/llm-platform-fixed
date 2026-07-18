import os
import re
import time
import json
import hashlib
import secrets
import sqlite3
import datetime as dt
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
SECRET_KEY = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)
EPAY_API_URL = os.environ.get('EPAY_API_URL', '').rstrip('/')
EPAY_PID = os.environ.get('EPAY_PID', '')
EPAY_KEY = os.environ.get('EPAY_KEY', '')
DOMAIN = os.environ.get('DOMAIN', 'newapi.ypvps.com')
PUBLIC_BASE_URL = os.environ.get('PUBLIC_BASE_URL', f'https://{DOMAIN}')

PLAN_CONFIG = {
    'free': {'name': '免费版', 'price': 0, 'daily_tokens': 10000, 'rate_limit': 20, 'days': 3650, 'description': '免费体验'},
    'starter': {'name': '入门版', 'price': 29, 'daily_tokens': 200000, 'rate_limit': 60, 'days': 30, 'description': '个人轻量调用'},
    'pro': {'name': '专业版', 'price': 99, 'daily_tokens': 1000000, 'rate_limit': 180, 'days': 30, 'description': '生产项目推荐'},
    'enterprise': {'name': '企业版', 'price': 399, 'daily_tokens': 5000000, 'rate_limit': 600, 'days': 30, 'description': '团队与高并发'},
    'yearly': {'name': '年付专业版', 'price': 999, 'daily_tokens': 1500000, 'rate_limit': 240, 'days': 365, 'description': '年付优惠'},
}

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=False)
CORS(app)

@app.after_request
def no_cache_dynamic_pages(resp):
    if request.path in ['/', '/login', '/register', '/logout', '/dashboard', '/playground', '/admin'] or request.path.startswith('/ticket/') or request.path.startswith('/pay/'):
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['Expires'] = '0'
    return resp

HTML = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>商业化 LLM 平台</title><style>
body{margin:0;background:#f6f7fb;color:#1f2937;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Arial,"PingFang SC",sans-serif}.wrap{max-width:1280px;margin:0 auto;padding:28px}.hero{background:linear-gradient(135deg,#111827,#2563eb);color:white;border-radius:22px;padding:34px;box-shadow:0 16px 45px #1d4ed833}.hero h1{margin:0 0 10px;font-size:32px}.hero p{opacity:.9}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px;margin-top:18px}.card{background:white;border-radius:18px;padding:20px;box-shadow:0 8px 30px #11182714;margin-top:16px}.muted{color:#6b7280}.pill{display:inline-block;padding:4px 10px;border-radius:999px;background:#e0f2fe;color:#0369a1;font-size:12px}.ok{color:#059669}.bad{color:#dc2626}.btn{display:inline-block;border:0;border-radius:10px;background:#2563eb;color:white;padding:10px 14px;text-decoration:none;cursor:pointer}.btn2{background:#111827}.btn-danger{background:#dc2626}.input{width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:10px;padding:10px;margin:6px 0 10px}pre{background:#0b1020;color:#d1e7ff;border-radius:14px;padding:14px;overflow:auto}table{border-collapse:collapse}td,th{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;vertical-align:top}th{background:#f9fafb}.mini{width:120px}.price{font-size:30px;font-weight:800}.nav a{color:white;margin-right:16px}.two{display:grid;grid-template-columns:1.1fr .9fr;gap:16px}.playground{grid-template-columns:minmax(0,1.2fr) minmax(360px,.8fr)}.chat-toolbar{display:grid;grid-template-columns:minmax(220px,1fr) auto;gap:10px;align-items:end}.chat-prompt{min-height:120px;max-height:220px;resize:vertical;margin-top:10px}.chat-result{min-height:220px;max-height:520px}.result-card{background:#f8fafc;border:1px solid #e5e7eb;border-radius:14px;padding:14px;margin-top:10px}.result-meta{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}.result-meta span{background:#eef2ff;color:#3730a3;border-radius:999px;padding:4px 9px;font-size:12px}.assistant-answer{white-space:pre-wrap;line-height:1.7;font-size:15px;background:white;border:1px solid #e5e7eb;border-radius:12px;padding:14px;color:#111827}.raw-json summary{cursor:pointer;color:#2563eb;margin-top:12px}.raw-json pre{max-height:360px}.curl-box{max-height:260px}.key-line{word-break:break-all}@media(max-width:900px){.two,.playground{grid-template-columns:1fr}.chat-toolbar{grid-template-columns:1fr}}
</style></head><body><div class="wrap"><section class="hero"><h1>企业级商业化 LLM 平台</h1><p>OpenAI 兼容 API · 用户/套餐/API Key/订单/工单/发票 · 易支付 · Ollama/第三方模型中转</p>{{nav|safe}}</section>{{body|safe}}</div></body></html>'''

def h(v):
    return escape('' if v is None else str(v), quote=True)

def has_request_context_safe():
    try: return has_request_context()
    except Exception: return False


def page(body):
    u=current_user() if has_request_context_safe() else None
    if u:
        admin_links='<a href="/admin">管理后台</a><a href="/v1/models">/v1/models</a><a href="/health">健康检查</a>' if u['is_admin'] else ''
        nav=f'<div class="nav"><a href="/">首页</a><a href="/dashboard">用户控制台</a><a href="/playground">聊天测试</a>{admin_links}<a href="/logout">退出（{h(u["username"])}）</a></div>'
    else:
        nav='<div class="nav"><a href="/">首页</a><a href="/login">登录</a><a href="/register">注册</a></div>'
    return render_template_string(HTML, body=body, nav=nav)

def db():
    if not hasattr(g, 'db'):
        os.makedirs(DATA_DIR, exist_ok=True)
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    con = getattr(g, 'db', None)
    if con: con.close()

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
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
CREATE TABLE IF NOT EXISTS model_providers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,provider_type TEXT DEFAULT 'openai',base_url TEXT DEFAULT '',api_key TEXT DEFAULT '',model_id TEXT NOT NULL,display_name TEXT DEFAULT '',is_default INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1,sort_order INTEGER DEFAULT 100,timeout_seconds INTEGER DEFAULT 300,created_at TEXT DEFAULT CURRENT_TIMESTAMP,updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS invoices(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,order_id INTEGER NOT NULL,invoice_no TEXT UNIQUE NOT NULL,company_name TEXT NOT NULL,tax_id TEXT,amount REAL NOT NULL,status TEXT DEFAULT 'pending',created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id),FOREIGN KEY(order_id) REFERENCES orders(id));
CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT DEFAULT '',updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS email_activations(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,email TEXT NOT NULL,token TEXT UNIQUE NOT NULL,expires_at TEXT NOT NULL,used_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(user_id) REFERENCES users(id));
''')
    for ddl in ['ALTER TABLE users ADD COLUMN daily_token_limit INTEGER','ALTER TABLE users ADD COLUMN custom_rate_limit INTEGER','ALTER TABLE users ADD COLUMN max_api_keys INTEGER DEFAULT 5','ALTER TABLE users ADD COLUMN email_verified_at TEXT','ALTER TABLE api_keys ADD COLUMN quota_daily INTEGER','ALTER TABLE api_keys ADD COLUMN key_plain TEXT']:
        try: c.execute(ddl)
        except sqlite3.OperationalError: pass
    for k,v in {'epay_api_url': EPAY_API_URL, 'epay_pid': EPAY_PID, 'epay_key': EPAY_KEY, 'domain': DOMAIN, 'public_base_url': PUBLIC_BASE_URL, 'payment_enabled': '0' if not (EPAY_API_URL and EPAY_PID and EPAY_KEY) else '1', 'smtp_enabled': '0', 'smtp_host': '', 'smtp_port': '587', 'smtp_username': '', 'smtp_password': '', 'smtp_encryption': 'tls', 'smtp_from_email': '', 'smtp_from_name': 'LLM Platform'}.items():
        c.execute('INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)', (k, v or ''))
    for idx,(pid,cfg) in enumerate(PLAN_CONFIG.items()):
        c.execute('INSERT OR IGNORE INTO plans(id,name,price,daily_tokens,rate_limit,days,is_active,sort_order,description) VALUES(?,?,?,?,?,?,?,?,?)', (pid,cfg['name'],cfg['price'],cfg['daily_tokens'],cfg['rate_limit'],cfg['days'],1,idx*10,cfg.get('description','')))
    c.execute('INSERT OR IGNORE INTO managed_projects(name,slug,description,base_url,status,sort_order) VALUES(?,?,?,?,?,?)', ('默认 LLM 网关','llm-gateway','OpenAI 兼容接口与模型中转平台',PUBLIC_BASE_URL,'active',10))
    c.execute('DELETE FROM model_providers WHERE id NOT IN (SELECT MIN(id) FROM model_providers GROUP BY provider_type, model_id, base_url)')
    if not c.execute('SELECT 1 FROM model_providers WHERE provider_type=? AND model_id=? AND base_url=?',('ollama',MODEL_NAME,OLLAMA_BASE_URL)).fetchone():
        c.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order) VALUES(?,?,?,?,?,?,?,?,?)', ('本地 Ollama','ollama',OLLAMA_BASE_URL,'',MODEL_NAME,MODEL_NAME,1,1,10))
    c.execute('INSERT OR IGNORE INTO users(username,email,password_hash,is_admin,invite_code,tokens_reset_date) VALUES(?,?,?,?,?,?)', ('admin','admin@example.com',generate_password_hash(ADMIN_PASSWORD),1,secrets.token_hex(6).upper(),dt.date.today().isoformat()))
    con.commit(); con.close()

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
    return {r['id']:{'name':r['name'],'price':float(r['price'] or 0),'daily_tokens':int(r['daily_tokens'] or 0),'rate_limit':int(r['rate_limit'] or 60),'days':int(r['days'] or 30),'description':r['description'] or '','is_active':int(r['is_active'] or 0)} for r in rows} or PLAN_CONFIG.copy()

def public_base_url():
    st=get_settings()
    return (st.get('public_base_url') or f"https://{st.get('domain') or DOMAIN}").rstrip('/')

def fetch_openai_models(base_url, api_key='', timeout=20):
    base=(base_url or '').rstrip('/')
    if not base: raise RuntimeError('Base URL 为空')
    headers={'Accept':'application/json'}
    if api_key: headers['Authorization']='Bearer '+api_key
    r=requests.get(base+'/models',headers=headers,timeout=timeout)
    if not r.ok: raise RuntimeError(f'读取模型失败 HTTP {r.status_code}: {r.text[:500]}')
    obj=r.json(); data=obj.get('data') if isinstance(obj,dict) else obj
    models=[]
    if isinstance(data,list):
        for item in data:
            mid=item.get('id') if isinstance(item,dict) else str(item)
            if mid: models.append(str(mid))
    return sorted(set(models))

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

def token_count(messages):
    return sum(len((m.get('content') or '')) for m in messages)//2+1

def api_auth():
    auth=request.headers.get('Authorization','')
    if not auth.startswith('Bearer '): return None,None
    raw=auth.split(' ',1)[1].strip(); key_hash=hashlib.sha256(raw.encode()).hexdigest()
    row=db().execute('SELECT k.*,u.plan,u.tokens_used_today,u.tokens_reset_date,u.is_active user_active,u.daily_token_limit,u.custom_rate_limit FROM api_keys k JOIN users u ON u.id=k.user_id WHERE k.key_hash=? AND k.is_active=1',(key_hash,)).fetchone()
    if not row or not row['user_active']: return None,None
    return row,raw

def epay_sign(params):
    filtered={k:v for k,v in params.items() if k not in ('sign','sign_type') and v not in ('',None)}
    s='&'.join(f'{k}={filtered[k]}' for k in sorted(filtered))+(get_settings().get('epay_key') or '')
    return hashlib.md5(s.encode()).hexdigest()

def active_model_rows():
    return db().execute('SELECT * FROM model_providers WHERE is_active=1 ORDER BY is_default DESC, sort_order ASC, id ASC').fetchall()

def candidate_model_rows(requested):
    rows=list(active_model_rows())
    if not rows: return []
    def prefer_remote(row):
        # Auto mode policy: third-party/OpenAI-compatible providers first, local Ollama last.
        # Keep admin priority inside each group.
        return 1 if row['provider_type']=='ollama' else 0
    rows=sorted(rows, key=lambda r: (prefer_remote(r), 0 if r['is_default'] else 1, int(r['sort_order'] or 100), int(r['id'])))
    req=(requested or '').strip()
    # "auto" means: try third-party models by priority, then local Ollama as final fallback.
    # Empty model behaves the same as auto.
    if not req or req.lower() in ('auto','auto:fallback','fallback'):
        return rows
    exact=[]; rest=[]
    for r in rows:
        if req in (r['model_id'], r['display_name'], r['name']): exact.append(r)
        else: rest.append(r)
    return exact+rest if exact else rows

def select_model(requested):
    rows=candidate_model_rows(requested)
    return rows[0] if rows else None

def proxy_openai(provider, payload, key, input_tokens, start):
    con=db(); url=(provider['base_url'] or '').rstrip('/')
    if not url: raise RuntimeError('第三方模型 Base URL 为空')
    headers={'Content-Type':'application/json'}
    if provider['api_key']: headers['Authorization']='Bearer '+provider['api_key']
    out=dict(payload); out['model']=provider['model_id']
    r=requests.post(url+'/chat/completions',headers=headers,json=out,timeout=int(provider['timeout_seconds'] or 300),stream=bool(out.get('stream')))
    if not r.ok: raise RuntimeError(f"{provider['name']} upstream HTTP {r.status_code}: {r.text[:500]}")
    if out.get('stream'):
        def gen():
            for chunk in r.iter_content(chunk_size=None):
                if chunk: yield chunk
        return Response(gen(), mimetype=r.headers.get('content-type','text/event-stream'))
    data=r.json(); content=''
    try: content=data.get('choices',[{}])[0].get('message',{}).get('content','') or ''
    except Exception: pass
    usage=data.get('usage') or {}; out_tokens=int(usage.get('completion_tokens') or max(1,len(content)//2)); total=int(usage.get('total_tokens') or input_tokens+out_tokens)
    dur=int((time.time()-start)*1000)
    con.execute('UPDATE users SET tokens_used_today=tokens_used_today+? WHERE id=?',(total,key['user_id']))
    con.execute('UPDATE api_keys SET last_used_at=CURRENT_TIMESTAMP WHERE id=?',(key['id'],))
    con.execute('INSERT INTO usage_logs(user_id,api_key_id,tokens_input,tokens_output,model,endpoint,duration_ms,ip_address) VALUES(?,?,?,?,?,?,?,?)',(key['user_id'],key['id'],input_tokens,out_tokens,provider['model_id'],'/v1/chat/completions',dur,request.remote_addr))
    con.commit()
    if 'model' not in data: data['model']=provider['model_id']
    return jsonify(data)

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

@app.route('/')
def index():
    try:
        r=requests.get(f'{OLLAMA_BASE_URL}/api/tags',timeout=1.5); ollama='<span class="ok">已连接</span>' if r.ok else '<span class="bad">异常</span>'
    except Exception: ollama='<span class="bad">未连接</span>'
    public_base=public_base_url(); plans=get_plan_config(); projects=db().execute('SELECT * FROM managed_projects ORDER BY sort_order ASC,id DESC LIMIT 12').fetchall()
    cards=''.join([f'<div class="card"><span class="pill">{h(pid)}</span><h3>{h(v["name"])}</h3><div class="price">¥{v["price"]:g}</div><p class="muted">每日 {v["daily_tokens"]:,} tokens · 限速 {v["rate_limit"]}/分钟 · {v["days"]}天</p><p class="muted">{h(v.get("description",""))}</p><a class="btn" href="/dashboard">购买/使用</a></div>' for pid,v in plans.items()])
    project_html=''.join([f'<div class="card"><span class="pill">{h(x["status"])}</span><h3>{h(x["name"])}</h3><p class="muted">{h(x["description"])}</p>'+(f'<a class="btn btn2" href="{h(x["base_url"])}">打开项目</a>' if x['base_url'] else '')+'</div>' for x in projects]) or '<div class="card">暂无项目</div>'
    am=active_model_rows(); platform_models=''.join([f'<li><b>{h(m["model_id"])}</b> · {h(m["name"])} · {h(m["provider_type"])}</li>' for m in am]) or '<li>暂无启用模型</li>'
    u=current_user(); admin_debug=''
    if u and u['is_admin']:
        admin_debug=f'<div class="card"><h3>OpenAI 兼容接口</h3><pre>curl {h(public_base)}/v1/models\nPOST {h(public_base)}/v1/chat/completions</pre></div><div class="card"><h3>平台可用模型</h3><ul>{platform_models}</ul></div>'
    body=f'<div class="grid"><div class="card"><h3>平台状态</h3><p>Web 网关：<span class="ok">运行中</span></p><p>默认模型：<b>{h((am[0]["model_id"] if am else MODEL_NAME))}</b></p><p class="muted">登录后可创建 API Key 并在聊天测试页验证接口。</p></div>{admin_debug}</div><h2>套餐</h2><div class="grid">{cards}</div><h2>管理项目</h2><div class="grid">{project_html}</div>'
    return page(body)

@app.route('/login',methods=['GET','POST'])
def login():
    msg=''
    if request.method=='POST':
        name=request.form.get('username','').strip(); pw=request.form.get('password','')
        u=db().execute('SELECT * FROM users WHERE username=? OR email=?',(name,name)).fetchone()
        if u and check_password_hash(u['password_hash'],pw):
            if not u['is_active']:
                msg='<p class="bad">账号未激活，请先前往邮箱点击激活链接</p>'
                return page(f'<div class="card"><h2>登录</h2>{msg}<form method="post"><input class="input" name="username" placeholder="用户名/邮箱"><input class="input" type="password" name="password" placeholder="密码"><button class="btn">登录</button> <a href="/register">注册</a></form></div>')
            session['uid']=u['id']; db().execute('UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?',(u['id'],)); db().commit(); return redirect('/dashboard')
        msg='<p class="bad">账号或密码错误</p>'
    return page(f'<div class="card"><h2>登录</h2>{msg}<form method="post"><input class="input" name="username" placeholder="用户名/邮箱"><input class="input" type="password" name="password" placeholder="密码"><button class="btn">登录</button> <a href="/register">注册</a></form></div>')

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
                con=db(); cur=con.execute('INSERT INTO users(username,email,password_hash,invite_code,tokens_reset_date,is_active) VALUES(?,?,?,?,?,0)',(username,email,generate_password_hash(pw),secrets.token_hex(6).upper(),dt.date.today().isoformat())); con.commit()
                send_activation_email(cur.lastrowid,email)
                return page('<div class="card"><h2>注册成功</h2><p class="ok">激活邮件已发送，请前往邮箱点击链接完成注册。</p><p><a class="btn" href="/login">去登录</a></p></div>')
            except Exception as e: msg=f'<p class="bad">注册失败：{h(e)}</p>'
    return page(f'<div class="card"><h2>注册</h2>{msg}<form method="post"><input class="input" name="username" placeholder="用户名"><input class="input" name="email" placeholder="邮箱"><input class="input" type="password" name="password" placeholder="密码"><button class="btn">注册</button></form></div>')

@app.route('/activate')
def activate():
    token=request.args.get('token','').strip(); con=db()
    row=con.execute('SELECT * FROM email_activations WHERE token=? AND used_at IS NULL',(token,)).fetchone()
    if not row: return page('<div class="card"><h2>激活失败</h2><p class="bad">激活链接无效或已使用。</p></div>'),400
    if dt.datetime.fromisoformat(row['expires_at']) < dt.datetime.now(): return page('<div class="card"><h2>激活失败</h2><p class="bad">激活链接已过期，请重新注册或联系管理员。</p></div>'),400
    con.execute('UPDATE users SET is_active=1,email_verified_at=CURRENT_TIMESTAMP WHERE id=?',(row['user_id'],)); con.execute('UPDATE email_activations SET used_at=CURRENT_TIMESTAMP WHERE id=?',(row['id'],)); con.commit()
    return page('<div class="card"><h2>账号已激活</h2><p class="ok">邮箱验证成功，现在可以登录使用。</p><p><a class="btn" href="/login">去登录</a></p></div>')

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
            elif 'response' in data:
                answer=data.get('response') or ''
                finish='done' if data.get('done') else ''
            u=data.get('usage') or {}
            if isinstance(u,dict):
                for label,key in [('输入','prompt_tokens'),('输出','completion_tokens'),('总计','total_tokens')]:
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
            for provider in candidate_model_rows(request.form.get('model')):
                try:
                    start_call=time.time()
                    if provider and provider['provider_type']=='openai':
                        rr=requests.post((provider['base_url'] or '').rstrip()+'/chat/completions',headers={'Authorization':'Bearer '+(provider['api_key'] or ''),'Content-Type':'application/json'},json={'model':provider['model_id'],'messages':[{'role':'user','content':prompt}],'temperature':0.4,'max_tokens':256},timeout=min(60,int(provider['timeout_seconds'] or 300)))
                        if not rr.ok: raise RuntimeError(f'HTTP {rr.status_code}: {rr.text[:300]}')
                        result=format_playground_result(provider, rr.text, int((time.time()-start_call)*1000)); break
                    else:
                        model_id=(provider['model_id'] if provider else MODEL_NAME)
                        if not model_id.endswith(':latest') and ':' not in model_id: model_id=model_id+':latest'
                        rr=requests.post((provider['base_url'] if provider else OLLAMA_BASE_URL).rstrip()+'/api/generate',json={'model':model_id,'prompt':prompt,'stream':False},timeout=min(60,int(provider['timeout_seconds'] if provider else 300)))
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
<div class="card"><h3>使用说明</h3><p><b>推荐使用自动模式：</b><code>model: "auto"</code></p><ul><li>优先调用第三方 / OpenAI 兼容模型。</li><li>第三方模型故障、超时或返回错误时，自动尝试下一条启用模型。</li><li>所有第三方都不可用时，最后才切到本地 Ollama。</li><li>本地 14B 较慢，适合作为兜底备用。</li><li><code>stream=true</code> 暂不做自动切换，避免流式响应中途换模型。</li></ul><h3>curl 测试命令</h3><pre class="curl-box">{h(curl)}</pre><p class="muted key-line">当前 API Key：{h(raw or '请先在控制台创建；发送一次测试会自动生成或复用 Key')}</p></div>
</div>''')

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
    body=f'<div class="two"><div><div class="card"><h2>用户控制台</h2><p>用户：{h(u["username"])} </p><a href="/logout">退出</a></div>{plan_usage}{reveal}<div class="card"><h3>API Keys</h3><p class="muted">已创建 {key_count} / 允许 {max_keys} 个；点击输入框可全选复制。</p><ul>{key_html}</ul><form method="post"><input type="hidden" name="act" value="newkey"><input class="input" name="name" placeholder="Key 名称"><button class="btn">创建 API Key</button></form></div></div><div><div class="card"><h3>购买套餐</h3><form method="post"><input type="hidden" name="act" value="order"><select class="input" name="plan">{plans}</select><select class="input" name="payment_method"><option value="alipay">支付宝</option><option value="wxpay">微信支付</option></select><button class="btn">创建订单</button></form><h4>我的订单</h4><ul>{order_html}</ul></div><div class="card"><h3>提交工单</h3><form method="post"><input type="hidden" name="act" value="ticket"><input class="input" name="title" placeholder="标题"><textarea class="input" name="content" placeholder="问题描述"></textarea><button class="btn btn2">提交</button></form><h4>我的工单</h4><ul>{ticket_html}</ul></div></div></div>'
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
            con.execute('UPDATE tickets SET title=COALESCE(NULLIF(?,''),title),category=?,priority=?,status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',(request.form.get('title',''),request.form.get('category','general'),request.form.get('priority','normal'),status,tid)); con.commit(); msg='工单已回复/更新'
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
        elif act=='save_model':
            mid=request.form.get('model_row_id')
            vals=(request.form.get('name',''),request.form.get('provider_type','openai'),request.form.get('base_url','').rstrip('/'),request.form.get('api_key',''),request.form.get('model_id',''),request.form.get('display_name',''),1 if request.form.get('is_default')=='1' else 0,1 if request.form.get('is_active')=='1' else 0,int(request.form.get('sort_order') or 100),int(request.form.get('timeout_seconds') or 300))
            if vals[6]: con.execute('UPDATE model_providers SET is_default=0')
            if mid: con.execute('UPDATE model_providers SET name=?,provider_type=?,base_url=?,api_key=?,model_id=?,display_name=?,is_default=?,is_active=?,sort_order=?,timeout_seconds=?,updated_at=CURRENT_TIMESTAMP WHERE id=?',vals+(mid,))
            else: con.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order,timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?,?)',vals)
            con.commit(); msg='模型配置已保存'
        elif act=='discover_models':
            name=request.form.get('name','第三方模型').strip() or '第三方模型'; base_url=request.form.get('base_url','').rstrip('/'); api_key=request.form.get('api_key',''); timeout=int(request.form.get('timeout_seconds') or 20); sort_order=int(request.form.get('sort_order') or 100)
            try:
                mids=fetch_openai_models(base_url,api_key,timeout); added=0
                for mid in mids:
                    if not con.execute('SELECT 1 FROM model_providers WHERE provider_type=? AND base_url=? AND model_id=?',('openai',base_url,mid)).fetchone():
                        con.execute('INSERT INTO model_providers(name,provider_type,base_url,api_key,model_id,display_name,is_default,is_active,sort_order,timeout_seconds) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,'openai',base_url,api_key,mid,mid,0,1,sort_order,300)); added+=1
                con.commit(); msg=f'已读取到 {len(mids)} 个模型，新增 {added} 个；已存在的自动跳过'
            except Exception as e: msg='读取模型失败：'+str(e)
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
    model_html=''.join([f'<tr><td>{m["id"]}<form method="post"><input type="hidden" name="act" value="save_model"><input type="hidden" name="model_row_id" value="{m["id"]}"></td><td><input class="input mini" name="name" value="{h(m["name"])}"></td><td><select class="input mini" name="provider_type"><option value="openai" {sel(m["provider_type"],"openai")}>OpenAI兼容</option><option value="ollama" {sel(m["provider_type"],"ollama")}>Ollama</option></select></td><td><input class="input" name="base_url" value="{h(m["base_url"])}" placeholder="https://api.xxx/v1 或 http://127.0.0.1:11434"></td><td><input class="input" name="api_key" value="{h(m["api_key"])}"></td><td><input class="input mini" name="model_id" value="{h(m["model_id"])}"></td><td><input class="input mini" name="display_name" value="{h(m["display_name"] or "")}"></td><td>{yn("is_default",m["is_default"])}{yn("is_active",m["is_active"])}</td><td><input class="input mini" name="sort_order" value="{h(m["sort_order"])}"><input class="input mini" name="timeout_seconds" value="{h(m["timeout_seconds"])}"></td><td><button class="btn">保存</button></form><form method="post"><input type="hidden" name="act" value="delete_model"><input type="hidden" name="model_row_id" value="{m["id"]}"><button class="btn btn-danger">删除</button></form></td></tr>' for m in model_rows])
    new_model='<tr><td>新建<form method="post"><input type="hidden" name="act" value="save_model"></td><td><input class="input mini" name="name" placeholder="供应商名"></td><td><select class="input mini" name="provider_type"><option value="openai">OpenAI兼容</option><option value="ollama">Ollama</option></select></td><td><input class="input" name="base_url" placeholder="https://api.xxx/v1"></td><td><input class="input" name="api_key"></td><td><input class="input mini" name="model_id" placeholder="gpt-4o-mini"></td><td><input class="input mini" name="display_name"></td><td><select name="is_default" class="input mini"><option value="0">默认否</option><option value="1">默认是</option></select><select name="is_active" class="input mini"><option value="1">启用</option><option value="0">禁用</option></select></td><td><input class="input mini" name="sort_order" value="100"><input class="input mini" name="timeout_seconds" value="300"></td><td><button class="btn">新增</button></form></td></tr>'
    discover_form='''<form method="post"><input type="hidden" name="act" value="discover_models"><div class="grid"><div><label>供应商名称</label><input class="input" name="name" placeholder="例如：硅基流动/自建 NewAPI"></div><div><label>Base URL</label><input class="input" name="base_url" placeholder="https://api.xxx/v1"></div><div><label>API Key</label><input class="input" name="api_key" placeholder="sk-..."></div><div><label>排序</label><input class="input" name="sort_order" value="100"></div><div><label>读取超时秒</label><input class="input" name="timeout_seconds" value="20"></div></div><button class="btn btn2">通过 /v1/models 自动读取并批量导入</button></form>'''
    usage_html=''.join([f'<tr><td>{h(u["username"])}</td><td>{u["calls"]}</td><td>{u["total_tokens"]}</td><td>{h(u["last_at"])}</td></tr>' for u in usage]) or '<tr><td colspan="4">暂无调用日志</td></tr>'
    body=f'''<div class="card"><h2>管理后台</h2>{'<p class="ok">'+h(msg)+'</p>' if msg else ''}
<h3>易支付配置</h3><p>当前支付状态：{pay_status}</p><form method="post"><input type="hidden" name="act" value="save_epay"><div class="grid"><div><label>启用支付</label><select class="input" name="payment_enabled"><option value="0" {sel(st.get('payment_enabled'),'0')}>禁用/演示</option><option value="1" {sel(st.get('payment_enabled'),'1')}>启用</option></select></div><div><label>易支付网关</label><input class="input" name="epay_api_url" value="{h(st.get('epay_api_url',''))}" placeholder="https://epay.example.com"></div><div><label>商户 PID</label><input class="input" name="epay_pid" value="{h(st.get('epay_pid',''))}"></div><div><label>商户 Key</label><input class="input" name="epay_key" value="{h(st.get('epay_key',''))}"></div><div><label>域名</label><input class="input" name="domain" value="{h(st.get('domain',''))}"></div><div><label>公网 Base URL</label><input class="input" name="public_base_url" value="{h(st.get('public_base_url',''))}"></div></div><button class="btn">保存易支付配置</button></form>
<h3>SMTP 邮件配置</h3><form method="post"><input type="hidden" name="act" value="save_smtp"><div class="grid"><div><label>启用 SMTP</label><select class="input" name="smtp_enabled"><option value="0" {sel(st.get('smtp_enabled'),'0')}>禁用</option><option value="1" {sel(st.get('smtp_enabled'),'1')}>启用</option></select></div><div><label>SMTP Host</label><input class="input" name="smtp_host" value="{h(st.get('smtp_host',''))}" placeholder="smtp.example.com"></div><div><label>端口</label><input class="input" name="smtp_port" value="{h(st.get('smtp_port','587'))}"></div><div><label>加密</label><select class="input" name="smtp_encryption"><option value="tls" {sel(st.get('smtp_encryption'),'tls')}>TLS/STARTTLS</option><option value="ssl" {sel(st.get('smtp_encryption'),'ssl')}>SSL</option><option value="none" {sel(st.get('smtp_encryption'),'none')}>不加密</option></select></div><div><label>账号</label><input class="input" name="smtp_username" value="{h(st.get('smtp_username',''))}"></div><div><label>密码/授权码</label><input class="input" name="smtp_password" value="{h(st.get('smtp_password',''))}"></div><div><label>发件邮箱</label><input class="input" name="smtp_from_email" value="{h(st.get('smtp_from_email',''))}"></div><div><label>发件名称</label><input class="input" name="smtp_from_name" value="{h(st.get('smtp_from_name','LLM Platform'))}"></div></div><button class="btn">保存 SMTP 配置</button></form><form method="post"><input type="hidden" name="act" value="test_smtp"><input type="hidden" name="smtp_enabled" value="{h(st.get('smtp_enabled','0'))}"><input type="hidden" name="smtp_host" value="{h(st.get('smtp_host',''))}"><input type="hidden" name="smtp_port" value="{h(st.get('smtp_port','587'))}"><input type="hidden" name="smtp_username" value="{h(st.get('smtp_username',''))}"><input type="hidden" name="smtp_password" value="{h(st.get('smtp_password',''))}"><input type="hidden" name="smtp_encryption" value="{h(st.get('smtp_encryption','tls'))}"><input type="hidden" name="smtp_from_email" value="{h(st.get('smtp_from_email',''))}"><input type="hidden" name="smtp_from_name" value="{h(st.get('smtp_from_name','LLM Platform'))}"><div class="grid"><div><label>测试收件邮箱</label><input class="input" name="test_email" value="{h(current_user()['email'] or '')}"></div></div><button class="btn btn2">发送测试邮件</button></form>
<h3>模型配置管理（三方 OpenAI 兼容/Ollama 中转）</h3><p class="muted">OpenAI 兼容供应商支持填写 Base URL + API Key 后自动请求 <code>/models</code> 批量导入模型 ID。</p>{discover_form}<table width="100%"><tr><th>ID</th><th>名称</th><th>类型</th><th>Base URL</th><th>API Key</th><th>模型ID</th><th>显示名</th><th>状态</th><th>排序/超时</th><th>操作</th></tr>{model_html}{new_model}</table>
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

@app.route('/v1/models')
def models():
    data=[]
    for m in active_model_rows(): data.append({'id':m['display_name'] or m['model_id'],'object':'model','created':int(time.time()),'owned_by':m['name'],'provider_type':m['provider_type']})
    return jsonify({'object':'list','data':data})

@app.route('/v1/chat/completions',methods=['POST'])
def chat_completions():
    key,_=api_auth()
    if not key: return jsonify({'error':{'message':'Unauthorized: missing/invalid Bearer API key','type':'auth_error'}}),401
    payload=request.get_json(force=True,silent=True) or {}; messages=payload.get('messages') or []
    input_tokens=token_count(messages); today=dt.date.today().isoformat(); con=db()
    if key['tokens_reset_date']!=today: con.execute('UPDATE users SET tokens_used_today=0,tokens_reset_date=? WHERE id=?',(today,key['user_id'])); con.commit(); used=0
    else: used=key['tokens_used_today']
    plan_limit=get_plan_config(True).get(key['plan'],PLAN_CONFIG['free'])['daily_tokens']; limits=[plan_limit]
    if key['daily_token_limit']: limits.append(int(key['daily_token_limit']))
    if key['quota_daily']: limits.append(int(key['quota_daily']))
    if used+input_tokens>min(limits): return jsonify({'error':{'message':'Daily token quota exceeded','type':'quota_error'}}),429
    candidates=candidate_model_rows(payload.get('model')); start=time.time()
    if not candidates: return jsonify({'error':{'message':'No active model provider configured','type':'model_error'}}),502
    # Streaming responses cannot be safely retried after bytes may have been sent to the client.
    # For stream=true, use the first candidate only.
    if payload.get('stream'): candidates=candidates[:1]
    errors=[]
    for provider in candidates:
        try:
            if provider['provider_type']=='openai': return proxy_openai(provider,payload,key,input_tokens,start)
            return proxy_ollama(provider,payload,key,input_tokens,start)
        except Exception as e:
            errors.append({'provider':provider['name'],'model':provider['model_id'],'type':provider['provider_type'],'error':str(e)[:500]})
            continue
    provider=candidates[0]
    if os.environ.get('DEMO_FALLBACK','0')=='1':
        content='演示模式：平台网关、用户系统、API Key 鉴权、套餐限额都已正常工作；当前所有模型上游不可用，所以这里返回模拟回复。收到的问题：'+(messages[-1].get('content','') if messages else '')
        out_tokens=max(1,len(content)//2); dur=int((time.time()-start)*1000); model_id=(provider['model_id'] if provider else MODEL_NAME)+'-demo'
        con.execute('UPDATE users SET tokens_used_today=tokens_used_today+? WHERE id=?',(input_tokens+out_tokens,key['user_id'])); con.execute('INSERT INTO usage_logs(user_id,api_key_id,tokens_input,tokens_output,model,endpoint,duration_ms,ip_address) VALUES(?,?,?,?,?,?,?,?)',(key['user_id'],key['id'],input_tokens,out_tokens,model_id,'/v1/chat/completions',dur,request.remote_addr)); con.commit()
        return jsonify({'id':'chatcmpl-demo','object':'chat.completion','created':int(time.time()),'model':model_id,'choices':[{'index':0,'message':{'role':'assistant','content':content},'finish_reason':'stop'}],'usage':{'prompt_tokens':input_tokens,'completion_tokens':out_tokens,'total_tokens':input_tokens+out_tokens},'fallback_errors':errors})
    return jsonify({'error':{'message':'All model providers failed','type':'gateway_error','fallback_errors':errors}}),502

init_db()

if __name__=='__main__':
    init_db(); app.run(host='0.0.0.0',port=int(os.environ.get('PORT','5088')),debug=False)
