#!/usr/bin/env python3
"""小手机共用仓库服务端：只用 Python 标准库（3.8+），不用装任何包。
不登录即可上传 / 浏览 / 下载；多人举报自动隐藏；管理员可下架、恢复、删除。
和账号系统完全无关，没有任何密钥写在代码里（全部走环境变量）。"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get('PORT', 8787))
DATA = os.environ.get('DATA_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data'))
ADMIN_KEY = os.environ.get('ADMIN_KEY', '')              # 不设置则管理功能关闭
SALT = os.environ.get('IP_SALT', 'xsj-repo-salt')        # 给 IP 做哈希的盐，不存原始 IP
TRUST_PROXY = os.environ.get('TRUST_PROXY') == '1'       # 放在 nginx 后面时设为 1
REPORT_HIDE = int(os.environ.get('REPORT_HIDE', 3))      # 几个不同的人举报后自动隐藏
MAX_BODY = int(os.environ.get('MAX_BODY', 6 * 1024 * 1024))
MAX_WORK_BYTES = int(os.environ.get('MAX_WORK_BYTES', 4 * 1024 * 1024))  # 单个作品附件总大小（解码后）
MAX_FILES = 10
UP_PER_HOUR = int(os.environ.get('UP_PER_HOUR', 10))
CATS = ['world', 'preset', 'persona', 'sticker', 'chat', 'voice', 'offline', 'other']

os.makedirs(os.path.join(DATA, 'works'), exist_ok=True)
LOCK = threading.RLock()

# ---------- 存储 ----------
works = {}
for fn in os.listdir(os.path.join(DATA, 'works')):
    if not fn.endswith('.json'):
        continue
    try:
        with open(os.path.join(DATA, 'works', fn), encoding='utf-8') as f:
            w = json.load(f)
        if w.get('id'):
            works[w['id']] = w
    except Exception:
        print('skip', fn)


def file_of(wid):
    return os.path.join(DATA, 'works', wid + '.json')


def persist(w):
    tmp = file_of(w['id']) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(w, f, ensure_ascii=False)
    os.replace(tmp, file_of(w['id']))


def sha(s):
    return hashlib.sha256(str(s).encode('utf-8')).hexdigest()


def dev_key(v):
    v = str(v or '').strip()
    if not re.fullmatch(r'[0-9a-zA-Z_-]{8,64}', v):
        raise Err(400, '缺少设备标识')
    return sha('dev:' + v + SALT)


def meta(w, dk=None):
    lk = w.get('likes') or []
    return {'likes': len(lk), 'liked': bool(dk and dk in lk), 'id': w['id'], 'title': w['title'], 'author': w['author'], 'cat': w['cat'],
            'desc': str(w.get('desc') or '')[:200], 'ts': w['ts'], 'poster': w.get('poster'),
            'files': [{'name': f['name'], 'size': f['size']} for f in w['files']]}


# ---------- 限流（内存，按 IP 哈希） ----------
buckets = {}


def hit(key, limit, window):
    now = time.time()
    with LOCK:
        b = buckets.get(key)
        if not b or now - b[0] > window:
            b = [now, 0]
        b[1] += 1
        buckets[key] = b
        if len(buckets) > 20000:  # 防止无限增长
            for k in [k for k, v in buckets.items() if now - v[0] > 3600]:
                buckets.pop(k, None)
        return b[1] <= limit


class Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


# ---------- 校验上传内容 ----------
def clean(b):
    if not isinstance(b, dict):
        raise Err(400, '内容格式不对')
    title = str(b.get('title') or '').strip()[:40]
    if not title:
        raise Err(400, '作品名不能为空')
    cat = b.get('cat') if b.get('cat') in CATS else 'other'
    author = str(b.get('author') or '').strip()[:30] or '佚名'
    desc = str(b.get('desc') or '').strip()[:2000]
    fl = b.get('files')
    if not isinstance(fl, list) or not fl:
        raise Err(400, '至少要有一个附件')
    if len(fl) > MAX_FILES:
        raise Err(400, '附件最多 %d 个' % MAX_FILES)
    total, files = 0, []
    for f in fl:
        if not isinstance(f, dict) or not isinstance(f.get('data'), str) or not f.get('name'):
            raise Err(400, '附件格式不对')
        enc = 'b' if f.get('enc') == 'b' else 't'
        size = len(f['data']) * 3 // 4 if enc == 'b' else len(f['data'].encode('utf-8'))
        total += size
        name = re.sub(r'[\\/\x00-\x1f]', '_', str(f['name']))[:120]
        files.append({'name': name, 'mime': str(f.get('mime') or '')[:80], 'enc': enc, 'size': size, 'data': f['data']})
    if total > MAX_WORK_BYTES:
        raise Err(400, '附件总共不能超过 %dMB' % round(MAX_WORK_BYTES / 1048576))
    # 马甲：昵称 ≤20 字，头像必须是 data:image 且 ≤40000 字符，否则只保留昵称
    poster = None
    p = b.get('poster')
    if isinstance(p, dict):
        pn = str(p.get('name') or '').strip()[:20]
        pa = p.get('avatar')
        if not (isinstance(pa, str) and pa.startswith('data:image/') and len(pa) <= 40000):
            pa = ''
        if pn:
            poster = {'name': pn, 'avatar': pa}
    return {'title': title, 'cat': cat, 'author': author, 'desc': desc, 'poster': poster, 'files': files}


ADMIN_HTML = r'''<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>仓库管理</title>
<style>body{font:15px/1.6 system-ui,sans-serif;max-width:720px;margin:0 auto;padding:16px}.w{border-bottom:1px solid #ddd;padding:10px 0}.h{opacity:.5}button{margin-right:6px}small{color:#777}</style>
<h2>仓库管理</h2><div id=o></div>
<script>
const key=prompt('管理密钥')||'';const H={'X-Admin-Key':key};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function load(){const r=await fetch('/api/admin/list',{headers:H}),j=await r.json();if(!r.ok){o.textContent=j.error;return}
o.innerHTML=j.works.map(w=>'<div class="w'+(w.hidden?' h':'')+'"><b>'+esc(w.title)+'</b> <small>'+esc(w.author)+' · '+w.cat+' · 举报 '+w.reports.length+(w.hidden?' · 已隐藏':'')+'</small><br><small>'+esc(w.reports.map(r=>r.reason).filter(Boolean).join(' / '))+'</small><br>'+
(w.hidden?'<button onclick="act(\''+w.id+'\',\'restore\')">恢复</button>':'<button onclick="act(\''+w.id+'\',\'hide\')">下架</button>')+'<button onclick="del(\''+w.id+'\')">删除</button></div>').join('')||'空'}
async function act(id,a){await fetch('/api/admin/'+id+'/'+a,{method:'POST',headers:H});load()}
async function del(id){if(confirm('永久删除？')){await fetch('/api/works/'+id,{method:'DELETE',headers:H});load()}}
load()
</script>'''

ID_RE = re.compile(r'^/api/works/([0-9a-f]{12})(?:/(pack|report|like))?$')
ADM_RE = re.compile(r'^/api/admin/([0-9a-f]{12})/(hide|restore)$')


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'xsj-repo'

    def log_message(self, *a):
        pass

    # ----- 工具 -----
    def ip(self):
        if TRUST_PROXY:
            x = (self.headers.get('X-Forwarded-For') or '').split(',')[0].strip()
            if x:
                return x
        return self.client_address[0]

    def send(self, code, obj):
        is_html = isinstance(obj, str)
        body = (obj if is_html else json.dumps(obj, ensure_ascii=False)).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'text/html; charset=utf-8' if is_html else 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET,POST,DELETE,OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type,X-Delete-Token,X-Admin-Key,X-Device-Id')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD' and code != 204:
            self.wfile.write(body)

    def read_json(self, lenient=False):
        n = int(self.headers.get('Content-Length') or 0)
        if n > MAX_BODY:
            self.close_connection = True
            raise Err(413, '内容太大')
        raw = self.rfile.read(n) if n else b''
        try:
            return json.loads(raw.decode('utf-8') or '{}')
        except Exception:
            if lenient:
                return {}
            raise Err(400, '不是有效的 JSON')

    def is_admin(self):
        return bool(ADMIN_KEY) and (self.headers.get('X-Admin-Key') or '') == ADMIN_KEY

    # ----- 路由 -----
    def do_OPTIONS(self):
        self.send(204, '')

    def do_GET(self):
        self.run()

    def do_POST(self):
        self.run()

    def do_DELETE(self):
        self.run()

    def run(self):
        try:
            self.route()
        except Err as e:
            self.send(e.code, {'error': e.msg})
        except Exception as e:  # noqa
            print('error', repr(e))
            try:
                self.send(500, {'error': '服务器出错了'})
            except Exception:
                pass

    def route(self):
        u = urlparse(self.path)
        p = u.path.rstrip('/') or '/'
        q = parse_qs(u.query)
        m = self.command
        ipk = sha(self.ip() + SALT)
        if not hit('r:' + ipk, 240, 60):
            raise Err(429, '请求太频繁，稍后再试')

        if p in ('/', '/api/health'):
            with LOCK:
                n = sum(1 for w in works.values() if not w.get('hidden'))
            return self.send(200, {'ok': True, 'name': 'xsj-repo', 'works': n})

        # 批量查点赞数（「我的」列表用）
        if m == 'GET' and p == '/api/likes':
            ids = [i for i in ((q.get('ids') or [''])[0]).split(',') if re.fullmatch(r'[0-9a-f]{12}', i)][:50]
            try:
                dk = dev_key(self.headers.get('X-Device-Id')) if self.headers.get('X-Device-Id') else None
            except Err:
                dk = None
            out = {}
            with LOCK:
                for i in ids:
                    w = works.get(i)
                    if w:
                        lk = w.get('likes') or []
                        out[i] = {'likes': len(lk), 'liked': bool(dk and dk in lk)}
            return self.send(200, {'likes': out})

        # 列表
        if m == 'GET' and p == '/api/works':
            cat = (q.get('cat') or [''])[0]
            kw = (q.get('q') or [''])[0].strip().lower()
            try:
                dk = dev_key(self.headers.get('X-Device-Id')) if self.headers.get('X-Device-Id') else None
            except Err:
                dk = None
            try:
                limit = min(100, int((q.get('limit') or ['30'])[0]) or 30)
                offset = max(0, int((q.get('offset') or ['0'])[0]))
            except ValueError:
                limit, offset = 30, 0
            with LOCK:
                al = [w for w in works.values() if not w.get('hidden') and (not cat or w['cat'] == cat)
                      and (not kw or kw in (w['title'] + ' ' + w['author']).lower())]
                al.sort(key=lambda w: w['ts'], reverse=True)
                return self.send(200, {'total': len(al), 'works': [meta(w, dk) for w in al[offset:offset + limit]]})

        # 上传
        if m == 'POST' and p == '/api/works':
            if not hit('u:' + ipk, UP_PER_HOUR, 3600):
                raise Err(429, '上传太频繁，一小时最多 %d 个' % UP_PER_HOUR)
            c = clean(self.read_json())
            token = secrets.token_hex(16)
            w = dict(c, id=secrets.token_hex(6), ts=int(time.time() * 1000), hidden=False, reports=[], tokenHash=sha(token))
            with LOCK:
                works[w['id']] = w
                persist(w)
            return self.send(200, {'id': w['id'], 'token': token})

        mm = ID_RE.match(p)
        if mm:
            wid, act = mm.group(1), mm.group(2)
            with LOCK:
                w = works.get(wid)
            if not w:
                raise Err(404, '作品不存在')
            tok = self.headers.get('X-Delete-Token') or ''
            mine = bool(tok) and bool(w.get('tokenHash')) and sha(tok) == w['tokenHash']
            adm = self.is_admin()
            if w.get('hidden') and not mine and not adm:
                raise Err(404, '作品不存在或已被下架')

            # 下载：格式和本地「作品包」一致，前端直接复用导入
            if m == 'GET' and act in (None, 'pack'):
                return self.send(200, {'app': 'xsj-repo', 'v': 1, 'works': [{
                    'title': w['title'], 'author': w['author'], 'cat': w['cat'], 'desc': w['desc'], 'ts': w['ts'],
                    'files': [{'name': f['name'], 'size': f['size'], 'mime': f['mime'], 'enc': f['enc'], 'data': f['data']} for f in w['files']]}]})
            # 点赞：按设备标识去重，liked=false 取消
            if m == 'POST' and act == 'like':
                if not hit('l:' + ipk, 120, 3600):
                    raise Err(429, '操作太频繁，稍后再试')
                dk = dev_key(self.headers.get('X-Device-Id'))
                b = self.read_json(lenient=True)
                want = bool(b.get('liked', True))
                with LOCK:
                    lk = w.setdefault('likes', [])
                    if want and dk not in lk and len(lk) < 100000:
                        lk.append(dk)
                    elif not want and dk in lk:
                        lk.remove(dk)
                    persist(w)
                    return self.send(200, {'likes': len(lk), 'liked': dk in lk})
            # 举报
            if m == 'POST' and act == 'report':
                if not hit('p:' + ipk, 30, 3600):
                    raise Err(429, '举报太频繁')
                b = self.read_json(lenient=True)
                with LOCK:
                    if not any(r['by'] == ipk for r in w['reports']):
                        w['reports'].append({'by': ipk, 'reason': str(b.get('reason') or '')[:200], 'ts': int(time.time() * 1000)})
                    if len(w['reports']) >= REPORT_HIDE:
                        w['hidden'] = True
                    persist(w)
                return self.send(200, {'ok': True})
            # 删除：作者凭 token，或管理员
            if m == 'DELETE' and act is None:
                if not mine and not adm:
                    raise Err(403, '没有权限删除')
                with LOCK:
                    works.pop(wid, None)
                    try:
                        os.remove(file_of(wid))
                    except OSError:
                        pass
                return self.send(200, {'ok': True})

        # 管理
        if p == '/admin':
            return self.send(200, ADMIN_HTML)
        if p.startswith('/api/admin/'):
            if not self.is_admin():
                raise Err(403, '管理密钥不对')
            if m == 'GET' and p == '/api/admin/list':
                with LOCK:
                    al = sorted(works.values(), key=lambda w: (-len(w['reports']), -w['ts']))[:300]
                    out = [dict(meta(w), poster=(w.get('poster') or {}).get('name', ''), hidden=bool(w.get('hidden')),
                                reports=[{'reason': r['reason'], 'ts': r['ts']} for r in w['reports']]) for w in al]
                return self.send(200, {'works': out})
            am = ADM_RE.match(p)
            if m == 'POST' and am:
                with LOCK:
                    w = works.get(am.group(1))
                    if not w:
                        raise Err(404, '作品不存在')
                    w['hidden'] = am.group(2) == 'hide'
                    if not w['hidden']:
                        w['reports'] = []
                    persist(w)
                return self.send(200, {'ok': True})
        raise Err(404, '没有这个接口')


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), H)
    srv.daemon_threads = True
    print('xsj-repo listening on :%d data=%s%s' % (PORT, DATA, '' if ADMIN_KEY else ' (管理功能未开启：请设置 ADMIN_KEY)'))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
