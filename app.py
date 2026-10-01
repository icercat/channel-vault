"""Channel Vault: stdlib web API, persistent queues, isolated yt-dlp subprocesses."""
import base64
import hmac
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote
from urllib.request import Request, urlopen
from providers import platform_for, source_url, x_posts

DATA = Path(os.getenv('DATA_DIR', '/data'))
MEDIA = Path(os.getenv('DOWNLOAD_DIR', '/downloads'))
COOKIES = Path(os.getenv('COOKIES_DIR', '/cookies'))
RUNTIME = {}
CONFIG_GENERATION = 0
WORKER_THREADS = []
COOKIE_LOCK = threading.Lock()
STOP = threading.Event()
PROCESSES = set()
PROCESS_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
WEB = Path(__file__).parent / 'web'

def env_int(name, default, minimum=1):
    return max(minimum, int(env_value(name, default)))

def env_value(name,default=None):
    return RUNTIME.get(name,os.getenv(name,default))

RUNTIME_SCHEMA = {
    'LIVE_POLL_SECONDS': (30,10,3600), 'LIVE_SCAN_LIMIT': (10,1,200),
    'SCAN_SECONDS': (21600,60,604800), 'UPDATE_SECONDS': (86400,300,604800),
    'DOWNLOAD_WORKERS': (2,1,16), 'LIVE_WORKERS': (4,1,16),
    'TRANSCODE_WORKERS': (1,1,8), 'TRANSCODE_THREADS': (2,1,32),
    'PLAYBACK_HEIGHT': (1080,144,4320), 'TWITTER_POLL_SECONDS': (300,60,86400),
}

def runtime_settings():
    value={name:env_int(name,bounds[0]) for name,bounds in RUNTIME_SCHEMA.items()}
    value['X_DISCOVERY_MODE']=env_value('X_DISCOVERY_MODE','cookies')
    value['YTDLP_CHANNEL']=env_value('YTDLP_CHANNEL','stable')
    value['LIVE_FROM_START']=str(env_value('LIVE_FROM_START','false')).lower()=='true'
    return value

def wait_config(seconds):
    generation=CONFIG_GENERATION
    deadline=time.monotonic()+seconds
    while not STOP.is_set():
        if generation!=CONFIG_GENERATION:return
        remaining=deadline-time.monotonic()
        if remaining<=0:return
        STOP.wait(min(1,remaining))

def db():
    con = sqlite3.connect(DATA / 'vault.sqlite', timeout=30)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=30000')
    return con

def execute(sql, args=()):
    with db() as con:
        return con.execute(sql, args).lastrowid

def rows(sql, args=()):
    with db() as con:
        return [dict(r) for r in con.execute(sql, args)]

def log(message, job=None, level='INFO'):
    message = str(message)[-8000:]
    print(f'[{level}] {message}', flush=True)
    execute('INSERT INTO logs(ts,level,job,message) VALUES(?,?,?,?)', (time.time(), level, job, message))

def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute('PRAGMA journal_mode=WAL')
        con.executescript('''
        CREATE TABLE IF NOT EXISTS channels (
          id INTEGER PRIMARY KEY, url TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, scan_due REAL DEFAULT 0,
          last_scan REAL, last_live REAL, error TEXT);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, channel_id INTEGER REFERENCES channels(id),
          media_key TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
          kind TEXT NOT NULL, live INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER DEFAULT 0,
          retry_at REAL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
          error TEXT, UNIQUE(channel_id, media_key));
        CREATE TABLE IF NOT EXISTS files (
          id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, path TEXT UNIQUE NOT NULL);
        CREATE TABLE IF NOT EXISTS logs (
          id INTEGER PRIMARY KEY, ts REAL NOT NULL, level TEXT, job INTEGER, message TEXT);
        CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,live,retry_at);
        CREATE INDEX IF NOT EXISTS logs_job ON logs(job,id);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sources (
          id INTEGER PRIMARY KEY,channel_id INTEGER NOT NULL,platform TEXT NOT NULL,
          url TEXT NOT NULL UNIQUE,enabled INTEGER DEFAULT 1,scan_due REAL DEFAULT 0,
          last_scan REAL,last_live REAL,error TEXT,provider_user_id TEXT,last_post_id TEXT);
        CREATE TABLE IF NOT EXISTS images (
          id INTEGER PRIMARY KEY,channel_id INTEGER NOT NULL,source_id INTEGER,
          role TEXT NOT NULL,path TEXT NOT NULL,mime TEXT NOT NULL,
          digest TEXT NOT NULL,created REAL NOT NULL,UNIQUE(channel_id,role,digest));
        ''')
        # Non-destructive upgrade of installations from v1.
        for table, columns in {
            'channels': {'avatar': 'TEXT', 'banner': 'TEXT', 'description': 'TEXT',
                         'active_avatar': 'INTEGER', 'active_banner': 'INTEGER',
                         'custom_name': 'INTEGER DEFAULT 0','image_status':'TEXT','image_error':'TEXT'},
            'jobs': {'thumbnail': 'TEXT', 'duration': 'REAL', 'height': 'INTEGER',
                     'derivative_status': "TEXT DEFAULT 'pending'", 'derivative_error': 'TEXT', 'source_id':'INTEGER'},
            'files': {'role': "TEXT DEFAULT 'video'"},
        }.items():
            existing = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in existing:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        con.execute("UPDATE jobs SET status='queued',retry_at=0,error='程序重啟，重新排程' WHERE status IN ('downloading','recording')")
        con.execute("UPDATE jobs SET derivative_status='pending' WHERE derivative_status='processing'")
        con.execute("UPDATE channels SET image_status='idle' WHERE image_status='processing'")
        for c in con.execute('SELECT id,url FROM channels').fetchall():
            platform,url=source_url(c['url'])
            con.execute('INSERT OR IGNORE INTO sources(channel_id,platform,url) VALUES(?,?,?)',(c['id'],platform,url))
            con.execute('UPDATE jobs SET source_id=(SELECT id FROM sources WHERE channel_id=? AND url=? LIMIT 1) WHERE channel_id=? AND source_id IS NULL', (c['id'],url,c['id']))
    (DATA/'assets').mkdir(exist_ok=True)
    (DATA/'cookie-runs').mkdir(exist_ok=True)
    RUNTIME.clear()
    for r in rows("SELECT key,value FROM settings WHERE key LIKE 'runtime:%'"):
        RUNTIME[r['key'].split(':',1)[1]]=json.loads(r['value'])
    # Retain v2 images before starting any refresh.
    for c in rows('SELECT * FROM channels'):
        for role in ('avatar','banner'):
            legacy=DATA/'assets'/f"channel-{c['id']}-{role}.img"
            if legacy.is_file() and not c['active_'+role]:
                store_image(c['id'],role,legacy.read_bytes(),c[role] or 'image/jpeg',activate=True)

THEMES = ('yt', 'twitch', 'trans', 'dark', 'light', 'neon', 'neon-pink')

def settings():
    values = {r['key']: json.loads(r['value']) for r in rows('SELECT * FROM settings')}
    return {'theme': values.get('theme', 'trans'), 'accent': values.get('accent', ''),
            'background': values.get('background', ''), 'surface': values.get('surface', ''),
            'runtime':runtime_settings(), 'cookies':cookie_status(),
            'x_api_configured':(COOKIES/'x-bearer.txt').is_file()}

def save_settings(values):
    global CONFIG_GENERATION
    previous_runtime=runtime_settings()
    if values.get('theme') not in THEMES:
        raise ValueError('未知配色')
    clean = {'theme': values['theme']}
    for key in ('accent', 'background', 'surface'):
        value = values.get(key, '')
        if value and not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError('顏色必須使用 #RRGGBB')
        clean[key] = value
    with db() as con:
        for key, value in clean.items():
            con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key,json.dumps(value)))
        runtime=values.get('runtime',{})
        validated={}
        for name,value in runtime.items():
            if name in RUNTIME_SCHEMA:
                _,low,high=RUNTIME_SCHEMA[name]
                if isinstance(value,bool) or not isinstance(value,int) or not low<=value<=high:
                    raise ValueError(f'{name} 必須介於 {low} 與 {high}')
            elif name=='X_DISCOVERY_MODE':
                if value not in ('cookies','api'):raise ValueError('X 追蹤方式必須是 cookies 或 api')
            elif name=='YTDLP_CHANNEL':
                if value not in ('stable','nightly'):raise ValueError('更新來源必須是 stable 或 nightly')
            elif name=='LIVE_FROM_START':
                if not isinstance(value,bool):raise ValueError('LIVE_FROM_START 必須是布林值')
            else:raise ValueError(f'不允許修改 {name}')
            validated[name]=value
        for name,value in validated.items():
            con.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('runtime:'+name,json.dumps(value)))
        if any(k in validated and validated[k]!=previous_runtime.get(k) for k in ('SCAN_SECONDS','X_DISCOVERY_MODE')):
            con.execute('UPDATE sources SET scan_due=0')
    RUNTIME.update(validated)
    if any(previous_runtime.get(k)!=v for k,v in validated.items()):CONFIG_GENERATION+=1
    return settings()

def cookie_status():
    return {platform:{'configured':(COOKIES/(platform+'.txt')).is_file(),
                      'legacy_available':(COOKIES/'cookies.txt').is_file(),
                      'enabled':env_value(platform.upper()+'_COOKIES_ENABLED',True)} for platform in ('youtube','twitch','twitter')}

def save_secret(body):
    global CONFIG_GENERATION
    platform=body.get('platform')
    if platform not in ('youtube','twitch','twitter','x-api'):
        raise ValueError('未知 cookies 平台')
    if 'enabled' in body and not isinstance(body['enabled'],bool):raise ValueError('enabled 必須是布林值')
    COOKIES.mkdir(parents=True,exist_ok=True)
    path=COOKIES/('x-bearer.txt' if platform=='x-api' else platform+'.txt')
    with COOKIE_LOCK:
        if body.get('delete'):
            path.unlink(missing_ok=True)
        elif 'content' in body:
            content=body['content']
            if not isinstance(content,str) or len(content.encode())>2*1024*1024:
                raise ValueError('內容格式不正確或超過 2 MB')
            if platform=='x-api':
                content=content.strip()
                if not re.fullmatch(r'[A-Za-z0-9_%+./=-]{10,4096}',content):raise ValueError('Bearer Token 格式不正確')
            else:
                validate_cookies(content)
            temp=path.with_suffix('.tmp')
            temp.write_text(content,encoding='utf-8');temp.chmod(0o600);temp.replace(path)
        if platform!='x-api' and 'enabled' in body:
            name=platform.upper()+'_COOKIES_ENABLED'
            execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',('runtime:'+name,json.dumps(body['enabled'])))
            RUNTIME[name]=body['enabled']
    CONFIG_GENERATION+=1
    if platform=='x-api':execute("UPDATE sources SET scan_due=0 WHERE platform='twitter'")
    return {'ok':True,'cookies':cookie_status(),'x_api_configured':(COOKIES/'x-bearer.txt').is_file()}

def validate_cookies(content):
    if not content.startswith(('# Netscape HTTP Cookie File','# HTTP Cookie File')):
        raise ValueError('請上傳 Netscape cookies.txt 格式')
    count=0
    for line in content.splitlines():
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):continue
        parts=line.split('\t')
        if len(parts)!=7 or parts[1] not in ('TRUE','FALSE') or parts[3] not in ('TRUE','FALSE'):
            raise ValueError('cookies 必須包含 7 個 Tab 分隔欄位')
        try:int(parts[4])
        except ValueError:raise ValueError('cookies 到期時間不正確') from None
        count+=1
    if not count:raise ValueError('cookies 檔案內沒有 cookie')

def store_image(cid,role,data,mime,source_id=None,activate=False):
    digest=hashlib.sha256(data).hexdigest()
    found=rows('SELECT id,path FROM images WHERE channel_id=? AND role=? AND digest=?',(cid,role,digest))
    if found:ident=found[0]['id']
    else:
        filename=uuid.uuid4().hex+'.img'
        (DATA/'assets'/filename).write_bytes(data)
        ident=execute('INSERT INTO images(channel_id,source_id,role,path,mime,digest,created) VALUES(?,?,?,?,?,?,?)',(cid,source_id,role,filename,mime,digest,time.time()))
    current=rows(f'SELECT active_{role} AS active FROM channels WHERE id=?',(cid,))
    if current and (activate or not current[0]['active']):
        execute(f'UPDATE channels SET active_{role}=?,{role}=? WHERE id=?',(ident,mime,cid))
    return ident

def cache_channel_images(cid, meta, source_id=None,activate=False):
    thumbs = meta.get('thumbnails') or []
    saved=0
    for role in ('avatar', 'banner'):
        candidates = [t for t in thumbs if str(t.get('id','')) == role+'_uncropped']
        if not candidates:
            candidates = [t for t in thumbs if role in str(t.get('id',''))]
        if not candidates and role=='banner':
            candidates = [t for t in thumbs if t.get('preference') in (-10,-5)]
        if not candidates:
            continue
        url = candidates[-1].get('url','')
        if not image_url_allowed(url):
            continue
        try:
            with urlopen(Request(url, headers={'User-Agent':'Mozilla/5.0'}),timeout=20) as response:
                if not image_url_allowed(response.url):
                    raise ValueError('Unexpected image host')
                content_type = response.headers.get_content_type()
                if content_type not in ('image/jpeg','image/png','image/webp'):
                    raise ValueError('Unexpected image type')
                data = response.read(8*1024*1024+1)
                if len(data)>8*1024*1024:
                    raise ValueError('Image too large')
            store_image(cid,role,data,content_type,source_id,activate)
            saved+=1
        except Exception as exc:
            log(f'頻道 {cid} {role} 圖片更新失敗：{exc}',level='WARN')
    if meta.get('description'):
        execute('UPDATE channels SET description=? WHERE id=?',(meta['description'],cid))
    return saved

def image_url_allowed(url):
    u=urlsplit(url)
    host=u.hostname or ''
    return u.scheme=='https' and any(host==domain or host.endswith('.'+domain) for domain in ('ytimg.com','ggpht.com','googleusercontent.com','jtvnw.net','ttvnw.net','twimg.com'))

def update_job_metadata(jobid, meta):
    thumbnail=meta.get('thumbnail')
    if not thumbnail and meta.get('thumbnails'):
        thumbnail=meta['thumbnails'][-1].get('url')
    # Only use known image hosts for browser-side thumbnail fallbacks.
    if not image_url_allowed(thumbnail or ''):
        thumbnail=None
    execute('UPDATE jobs SET thumbnail=COALESCE(?,thumbnail),duration=COALESCE(?,duration) WHERE id=?',
        (thumbnail,meta.get('duration'),jobid))

def register_file(jobid, path, role='video'):
    path=safe_file(path)
    if path.is_file():
        execute('INSERT INTO files(job_id,path,role) VALUES(?,?,?) ON CONFLICT(path) DO UPDATE SET role=excluded.role',
            (jobid,str(path.relative_to(MEDIA.resolve())),role))

def channel_url(value):
    return source_url(value)[1]

def link_url(value):
    u = urlsplit(value.strip())
    if u.scheme != 'https' or u.username or u.password:
        raise ValueError('單次下載請提供 HTTPS 網址')
    platform_for(value)
    if u.port not in (None,443):
        raise ValueError('網址不接受自訂 port')
    return value.strip()

def ytdlp():
    pointer = DATA / 'runtime-current'
    if pointer.exists():
        candidate = Path(pointer.read_text().strip()) / 'bin/python'
        if candidate.is_file():
            return [str(candidate), '-m', 'yt_dlp']
    return ['/opt/bootstrap/bin/python', '-m', 'yt_dlp']

def base_args(url=None):
    args = ['--ignore-config', '--no-colors', '--js-runtimes', 'deno', '--socket-timeout', '20']
    platform=platform_for(url) if url else 'youtube'
    if env_value(platform.upper()+'_COOKIES_ENABLED',True):
        specific=COOKIES/(platform+'.txt')
        source=specific if specific.is_file() else COOKIES/'cookies.txt'
        with COOKIE_LOCK:
            if source.is_file():
                folder=DATA/'cookie-runs';folder.mkdir(exist_ok=True)
                snapshot=folder/(uuid.uuid4().hex+'.txt')
                snapshot.write_bytes(source.read_bytes());snapshot.chmod(0o600)
                args += ['--cookies',str(snapshot)]
    return args

def cleanup_cookie_args(args):
    if '--cookies' in args:
        path=Path(args[args.index('--cookies')+1])
        if path.parent==DATA/'cookie-runs':path.unlink(missing_ok=True)

def probe(url, flat=False, limit=None, timeout=90):
    args = ytdlp() + base_args(url) + ['--dump-single-json','--skip-download']
    if flat:
        args += ['--flat-playlist']
    else:
        args += ['--yes-playlist' if platform_for(url)=='twitter' and '/status/' in url else '--no-playlist']
    if limit:
        args += ['--playlist-end', str(limit)]
    try:
        result = subprocess.run(args + ['--',url], capture_output=True, text=True, timeout=timeout)
    finally:
        cleanup_cookie_args(args)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or 'yt-dlp metadata failed')
    return json.loads(result.stdout)

def enqueue(cid, url, key, title, kind='video', live=False, source_id=None):
    now = time.time()
    with db() as con:
        if cid is not None:
            old = con.execute('SELECT * FROM jobs WHERE channel_id=? AND media_key=?',(cid,key)).fetchone()
            if old:
                # A scheduled/live item can be promoted ahead of backlog workers.
                if live and old['status'] in ('queued','failed'):
                    con.execute("UPDATE jobs SET live=1,kind='live',status='queued',retry_at=0 WHERE id=?",(old['id'],))
                return old['id']
        return con.execute('INSERT INTO jobs(channel_id,media_key,url,title,kind,live,created,updated,source_id) VALUES(?,?,?,?,?,?,?,?,?)',
            (cid,key,url,title,kind,int(live),now,now,source_id)).lastrowid

def item_url(entry):
    ident = entry.get('id')
    return f'https://www.youtube.com/watch?v={ident}' if ident else None

def _scan_youtube(c,source_id=None):
    errors = []
    for suffix, kind in [('videos','video'),('shorts','video'),('streams','live')]:
        if STOP.is_set():
            return
        try:
            meta = probe(c['url']+'/'+suffix, flat=True, timeout=1800)
            if meta.get('channel'):
                execute('UPDATE channels SET name=? WHERE id=? AND custom_name=0',(meta['channel'],c['id']))
            if suffix == 'videos':
                cache_channel_images(c['id'],meta,source_id)
            count = 0
            for e in meta.get('entries') or []:
                if not e or not item_url(e):
                    continue
                status = e.get('live_status')
                if status == 'is_upcoming':
                    continue
                ident=enqueue(c['id'], item_url(e), e['id'], e.get('title') or e['id'],kind,status=='is_live',source_id)
                update_job_metadata(ident,e)
                count += 1
            log(f"{c['name']} /{suffix}：已掃描 {count} 項")
        except Exception as exc:
            errors.append(f'{suffix}: {exc}')
            log(f"頻道掃描 {c['name']} /{suffix}：{exc}",level='ERROR')
    execute('UPDATE channels SET last_scan=?,error=? WHERE id=?',(time.time(),'\n'.join(errors) or None,c['id']))

LIVE_PROBE_CACHE = {}
def _poll_youtube(c,source_id=None):
    # Scan recent streams rather than only /live (a channel can have multiple streams).
    meta = probe(c['url']+'/streams',flat=True,limit=env_int('LIVE_SCAN_LIMIT',10),timeout=60)
    now = time.time()
    for e in meta.get('entries') or []:
        if not e or not item_url(e):
            continue
        status = e.get('live_status')
        if status in ('was_live','post_live','not_live'):
            continue
        old = rows('SELECT status,live FROM jobs WHERE channel_id=? AND media_key=?',(c['id'],e['id']))
        if old and (old[0]['status']=='done' or old[0]['status'] in ('recording','downloading')):
            continue
        if status != 'is_live':
            # Flat metadata may omit live_status; cache finished probes to avoid repeated requests.
            cache_key = (source_id or c['id'],e['id'])
            if LIVE_PROBE_CACHE.get(cache_key,0)>now:
                continue
            detail = probe(item_url(e),timeout=45)
            status = detail.get('live_status')
            LIVE_PROBE_CACHE[cache_key] = now + (env_int('LIVE_POLL_SECONDS',30) if status=='is_upcoming' else 3600)
        if status == 'is_live':
            enqueue(c['id'],item_url(e),e['id'],e.get('title') or e['id'],'live',True,source_id)
    execute('UPDATE channels SET last_live=? WHERE id=?',(time.time(),c['id']))

def ensure_sources(c):
    if not rows('SELECT id FROM sources WHERE channel_id=?',(c['id'],)):
        platform,url=source_url(c['url'])
        execute('INSERT OR IGNORE INTO sources(channel_id,platform,url) VALUES(?,?,?)',(c['id'],platform,url))
    return rows('SELECT * FROM sources WHERE channel_id=? AND enabled=1 ORDER BY id',(c['id'],))

def scan_channel(c):
    for source in ensure_sources(c):scan_source(source)

def poll_live(c):
    for source in ensure_sources(c):poll_source(source)

def scan_source(s):
    c=rows('SELECT * FROM channels WHERE id=?',(s['channel_id'],))[0]
    c['url']=s['url']
    errors=[]
    try:
        if s['platform']=='youtube':
            _scan_youtube(c,s['id'])
            errors=[r['error'] for r in rows('SELECT error FROM channels WHERE id=?',(c['id'],)) if r['error']]
        elif s['platform']=='twitch':
            for suffix,kind in [('/videos?filter=all&sort=time','live'),('/clips?filter=clips&range=all','video')]:
                try:
                    meta=probe(s['url']+suffix,flat=True,timeout=1800)
                    for e in meta.get('entries') or []:
                        if not e or not e.get('url') or not e.get('id'):continue
                        ident=enqueue(c['id'],e['url'],'twitch:'+e['id'],e.get('title') or e['id'],kind,False,s['id'])
                        update_job_metadata(ident,e)
                except Exception as exc:errors.append(str(exc))
        else:
            scan_twitter(s,history=True)
    except Exception as exc:errors.append(str(exc))
    error='\n'.join(errors) or None
    execute('UPDATE sources SET last_scan=?,error=? WHERE id=?',(time.time(),error,s['id']))
    execute('UPDATE channels SET last_scan=? WHERE id=?',(time.time(),c['id']))
    if error:log(f"來源掃描 {s['url']}：{error}",level='ERROR')

def scan_twitter_cookies(s,history=False):
    args=base_args(s['url'])
    if '--cookies' not in args:
        raise RuntimeError('免費 X 追蹤需要上傳並啟用 Twitter / X 登入 cookies')
    cookie=args[args.index('--cookies')+1]
    command=[ytdlp()[0],'-m','gallery_dl','--config-ignore','--no-input',
             '--no-colors','--dump-json','--cookies',cookie,'--retries','2',
             '--http-timeout','20','--sleep-request','2',
             '-o','extractor.twitter.retweets=false',
             '-o','extractor.twitter.videos=true']
    if not history:command+=['--post-range','1:100']
    try:
        result=subprocess.run(command+[s['url']+'/media'],capture_output=True,text=True,timeout=1800 if history else 300)
        if result.returncode:
            raise RuntimeError('gallery-dl 掃描失敗；請檢查登入 cookies 是否過期、X 限流及工具更新（code '+str(result.returncode)+'）')
        messages=json.loads(result.stdout)
        from providers import gallery_entries
        entries=gallery_entries(messages,s['url'].rsplit('/',1)[1],None if history else s.get('last_post_id'))
        newest=s.get('last_post_id');count=0
        for e in entries:
            if STOP.is_set():return
            ident=enqueue(s['channel_id'],e['url'],'twitter:'+e['id'],e['title'],'video',False,s['id'])
            update_job_metadata(ident,e);count+=1
            if not newest or int(e['id'])>int(newest):newest=e['id']
        execute('UPDATE sources SET last_post_id=? WHERE id=?',(newest,s['id']))
        log(f"X cookies 掃描 {s['url']}：找到 {count} 個影片貼文")
    finally:cleanup_cookie_args(args)

def scan_twitter(s,history=False):
    if env_value('X_DISCOVERY_MODE','cookies')=='cookies':
        return scan_twitter_cookies(s,history)
    path=COOKIES/'x-bearer.txt'
    token=path.read_text().strip() if path.is_file() else ''
    newest=s.get('last_post_id')
    for batch in x_posts(s['url'].rsplit('/',1)[1],token,s.get('provider_user_id'),None if history else newest,history):
        if STOP.is_set():return
        for e in batch['entries']:
            ident=enqueue(s['channel_id'],e['url'],'twitter:'+e['id'],e['title'],'live' if e.get('broadcast') else 'video',bool(e.get('broadcast') and not history),s['id'])
            update_job_metadata(ident,e)
        candidate=batch.get('newest_id')
        if candidate and (not newest or int(candidate)>int(newest)):newest=candidate
        execute('UPDATE sources SET provider_user_id=?,last_post_id=? WHERE id=?',(batch['user_id'],newest,s['id']))

def poll_source(s):
    c=rows('SELECT * FROM channels WHERE id=?',(s['channel_id'],))[0];c['url']=s['url']
    if s['platform']=='youtube':
        _poll_youtube(c,s['id'])
    elif s['platform']=='twitch':
        try:
            meta=probe(s['url'],timeout=60)
        except Exception as exc:
            if any(word in str(exc).lower() for word in ('is not live','offline','not currently live')):
                meta={}
            else:raise
        if meta.get('is_live') or meta.get('live_status')=='is_live':
            key='twitch:live:'+str(meta.get('id') or s['id'])+':'+str(meta.get('timestamp') or meta.get('release_timestamp') or '')
            ident=enqueue(c['id'],s['url'],key,meta.get('title') or c['name'],'live',True,s['id'])
            update_job_metadata(ident,meta)
    elif time.time()-(s.get('last_live') or 0)>=env_int('TWITTER_POLL_SECONDS',300):
        scan_twitter(s)
    else:return
    execute('UPDATE sources SET last_live=? WHERE id=?',(time.time(),s['id']))
    execute('UPDATE channels SET last_live=? WHERE id=?',(time.time(),c['id']))

IMAGE_REFRESH = set()
IMAGE_LOCK = threading.Lock()

def refresh_images(cid):
    with IMAGE_LOCK:
        if cid in IMAGE_REFRESH:return
        IMAGE_REFRESH.add(cid)
    execute("UPDATE channels SET image_status='processing',image_error=NULL WHERE id=?",(cid,))
    try:
        sources=rows("SELECT * FROM sources WHERE channel_id=? AND platform='youtube' ORDER BY id",(cid,))
        if not sources:raise ValueError('此訂閱沒有 YouTube 來源')
        for source in sources:
            meta=probe(source['url']+'/videos',flat=True,limit=1,timeout=90)
            if not cache_channel_images(cid,meta,source['id'],activate=True):
                raise RuntimeError('此次未取得頭像或 banner；舊圖片仍保留')
        execute("UPDATE channels SET image_status='ready' WHERE id=?",(cid,))
        log(f'頻道 {cid} 圖片已重新取得，舊版本完整保留')
    except Exception as exc:
        execute("UPDATE channels SET image_status='failed',image_error=? WHERE id=?",(str(exc),cid))
        log(f'圖片重新取得失敗：{exc}',level='ERROR')
    finally:
        with IMAGE_LOCK:IMAGE_REFRESH.discard(cid)

def scanning_loop():
    while not STOP.wait(2):
        found = rows('SELECT s.* FROM sources s JOIN channels c ON c.id=s.channel_id WHERE s.enabled=1 AND c.enabled=1 AND s.scan_due<=? ORDER BY s.scan_due LIMIT 1',(time.time(),))
        if found:
            c = found[0]
            execute('UPDATE sources SET scan_due=? WHERE id=?',(time.time()+env_int('SCAN_SECONDS',21600),c['id']))
            scan_source(c)

def live_loop():
    while not STOP.is_set():
        started = time.monotonic()
        for c in rows('SELECT s.* FROM sources s JOIN channels c ON c.id=s.channel_id WHERE s.enabled=1 AND c.enabled=1'):
            try:
                poll_source(c)
            except Exception as exc:
                execute('UPDATE sources SET last_live=?,error=? WHERE id=?',(time.time(),str(exc),c['id']))
                log(f"來源更新 {c['url']}：{exc}",level='WARN')
        wait_config(max(1,env_int('LIVE_POLL_SECONDS',30)-(time.monotonic()-started)))

def claim(live):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        r = con.execute('''SELECT j.* FROM jobs j LEFT JOIN channels c ON c.id=j.channel_id LEFT JOIN sources s ON s.id=j.source_id
          WHERE j.status='queued' AND j.live=? AND j.retry_at<=?
          AND (j.channel_id IS NULL OR c.enabled=1) AND (j.source_id IS NULL OR s.enabled=1) ORDER BY j.id LIMIT 1''',(int(live),time.time())).fetchone()
        if r:
            con.execute('UPDATE jobs SET status=?,attempts=attempts+1,updated=? WHERE id=?',('recording' if live else 'downloading',time.time(),r['id']))
            return dict(r)

def safe_file(value):
    p = Path(value).resolve()
    if not p.is_relative_to(MEDIA.resolve()):
        raise ValueError('file outside download directory')
    return p

def download(j):
    jobid = j['id']
    try:
        meta = probe(j['url'])
        update_job_metadata(jobid,meta)
        status = meta.get('live_status')
        if status == 'is_upcoming':
            execute("UPDATE jobs SET status='queued',retry_at=?,updated=? WHERE id=?",(time.time()+60,time.time(),jobid))
            return
        live = status == 'is_live' or meta.get('is_live') is True
        kind = 'live' if live or meta.get('was_live') or status in ('was_live','post_live') or j['kind']=='live' else 'video'
        execute('UPDATE jobs SET title=?,kind=?,live=?,status=? WHERE id=?',
            (meta.get('title') or j['title'],kind,int(live),'recording' if live else 'downloading',jobid))
        # If a flat scan missed is_live, hand it to reserved live workers immediately.
        if live and not j['live']:
            execute("UPDATE jobs SET status='queued',retry_at=0 WHERE id=?",(jobid,))
            return
        folder = MEDIA / (f"channel-{j['channel_id']}" if j['channel_id'] else 'single') / ('直播' if kind=='live' else '影片') / str(jobid)
        folder.mkdir(parents=True,exist_ok=True)
        template = str(folder / '%(upload_date)s_%(title).150B_[%(id)s].%(ext)s')
        # Each job's immutable archive prevents duplicate completed downloads after crash.
        playlist_flag='--yes-playlist' if platform_for(j['url'])=='twitter' and '/status/' in j['url'] else '--no-playlist'
        args = ytdlp()+base_args(j['url'])+[playlist_flag,'--newline','--progress','--continue',
            '--retries','10','--fragment-retries','10','--retry-sleep','5',
            '--no-abort-on-error','--abort-on-unavailable-fragments',
            '--download-archive',str(folder/'archive.txt'),
            '--write-info-json','--write-thumbnail','--merge-output-format','mkv',
            '-f','bv*+ba/b','--format-sort-force','-S','res,fps,vbr,abr','-o',template,
            '--print','after_move:VAULT_FILE:%(filepath)j',
            '--print-to-file','after_move:%(filepath)j',str(folder/'completed.jsonl')]
        if live:
            args += ['--live-from-start' if str(env_value('LIVE_FROM_START','false')).lower()=='true' else '--no-live-from-start']
        log(f"{'開始直播錄製' if live else '開始下載'}：{j['url']}",jobid)
        try:
            p = subprocess.Popen(args+['--',j['url']],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
        except Exception:
            cleanup_cookie_args(args);raise
        with PROCESS_LOCK:
            PROCESSES.add(p)
        try:
            for line in p.stdout:
                line = line.strip()
                if line.startswith('VAULT_FILE:'):
                    path = safe_file(json.loads(line[len('VAULT_FILE:'):]))
                    register_file(jobid,path)
                elif line:
                    log(line,jobid)
            rc = p.wait()
        finally:
            with PROCESS_LOCK:
                PROCESSES.discard(p)
            cleanup_cookie_args(args)
        if STOP.is_set():
            execute("UPDATE jobs SET status='queued',retry_at=0 WHERE id=?",(jobid,))
            return
        # Reconcile outputs if completion occurred immediately before interruption.
        manifest = folder/'completed.jsonl'
        if manifest.exists():
            for line in manifest.read_text().splitlines():
                try:
                    path = safe_file(json.loads(line))
                    if path.is_file() and path.stat().st_size:
                        register_file(jobid,path)
                except (ValueError, OSError):
                    continue
        if rc or not rows('SELECT id FROM files WHERE job_id=?',(jobid,)):
            raise RuntimeError(f'yt-dlp exit={rc}，詳見此任務 Log；可能需 cookies 或 PO Token')
        for thumb in folder.iterdir():
            if thumb.suffix.lower() in ('.jpg','.jpeg','.png','.webp'):
                register_file(jobid,thumb,'thumbnail')
        execute("UPDATE jobs SET status='done',derivative_status='pending',error=NULL,updated=? WHERE id=?",(time.time(),jobid))
        log('最高畫質原檔下載完成；已排程 MP3 與網頁播放版本',jobid)
    except Exception as exc:
        attempts = rows('SELECT attempts FROM jobs WHERE id=?',(jobid,))[0]['attempts']
        execute('UPDATE jobs SET status=?,retry_at=?,error=?,updated=? WHERE id=?',
            ('queued' if attempts<5 else 'failed',time.time()+min(3600,60*2**min(attempts,6)),str(exc),time.time(),jobid))
        log(str(exc),jobid,'ERROR')

def worker(live=False,index=0):
    while not STOP.wait(1):
        if index>=env_int('LIVE_WORKERS' if live else 'DOWNLOAD_WORKERS',4 if live else 2):continue
        j = claim(live)
        if j:
            download(j)

def ffmpeg_run(args, jobid):
    """Track converters so shutdown interrupts them just like live recorders."""
    p=subprocess.Popen(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y']+args,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
    with PROCESS_LOCK:
        PROCESSES.add(p)
    try:
        output=p.communicate()[0]
        if p.returncode:
            raise RuntimeError(output[-2000:] or f'FFmpeg exit={p.returncode}')
        if STOP.is_set():
            raise RuntimeError('轉檔已中斷，重啟後重新排程')
    finally:
        with PROCESS_LOCK:
            PROCESSES.discard(p)

def derivatives(jobid):
    try:
        originals=rows("SELECT * FROM files WHERE job_id=? AND role='video'",(jobid,))
        if not originals:
            raise RuntimeError('找不到原始影音檔')
        for item in originals:
            source=safe_file(MEDIA/item['path'])
            if not source.is_file():
                raise RuntimeError(f'原檔不存在：{source.name}')
            for thumb in source.parent.iterdir():
                if thumb.suffix.lower() in ('.jpg','.jpeg','.png','.webp'):
                    register_file(jobid,thumb,'thumbnail')
            metadata=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(source)],text=True,timeout=60))
            videos=[s for s in metadata['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')]
            audio=any(s.get('codec_type')=='audio' for s in metadata['streams'])
            if videos:
                execute('UPDATE jobs SET height=? WHERE id=?',(videos[0].get('height'),jobid))
            mp3=source.with_name(source.stem+'.audio.mp3')
            if audio:
                if not mp3.is_file():
                    log('製作 MP3 留存副本…',jobid)
                    temp=mp3.with_name(mp3.stem+'.building.mp3')
                    ffmpeg_run(['-i',str(source),'-map','0:a:0','-vn','-c:a','libmp3lame','-q:a','0',str(temp)],jobid)
                    temp.replace(mp3)
                register_file(jobid,mp3,'mp3')
            else:
                log('來源沒有音軌，無法產生 MP3',jobid,'WARN')
            if videos:
                preview=source.with_name(source.stem+'.playback.mp4')
                if not preview.is_file():
                    log('製作瀏覽器播放副本（原檔最高畫質保留）…',jobid)
                    temp=preview.with_name(preview.stem+'.building.mp4')
                    height=env_int('PLAYBACK_HEIGHT',1080,2)
                    ffmpeg_run(['-i',str(source),'-map','0:v:0','-map','0:a:0?',
                        '-vf',f'scale=-2:min({height}\\,ih),pad=ceil(iw/2)*2:ceil(ih/2)*2',
                        '-c:v','libx264','-preset','fast','-crf','20','-pix_fmt','yuv420p',
                        '-c:a','aac','-b:a','192k','-movflags','+faststart',
                        '-threads',str(env_int('TRANSCODE_THREADS',2)),str(temp)],jobid)
                    temp.replace(preview)
                register_file(jobid,preview,'preview')
        execute("UPDATE jobs SET derivative_status='ready',derivative_error=NULL WHERE id=?",(jobid,))
        log('MP3 / 播放副本處理完成',jobid)
    except Exception as exc:
        execute('UPDATE jobs SET derivative_status=?,derivative_error=? WHERE id=?',('pending' if STOP.is_set() else 'failed',str(exc),jobid))
        log(f'轉檔失敗：{exc}',jobid,'ERROR')

def derivative_loop(index=0):
    while not STOP.wait(2):
        if index>=env_int('TRANSCODE_WORKERS',1):continue
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            found=con.execute("SELECT id FROM jobs WHERE status='done' AND derivative_status='pending' ORDER BY id LIMIT 1").fetchone()
            if found:
                con.execute("UPDATE jobs SET derivative_status='processing' WHERE id=?",(found['id'],))
        if found:
            derivatives(found['id'])

def worker_supervisor():
    counts={'DOWNLOAD_WORKERS':0,'LIVE_WORKERS':0,'TRANSCODE_WORKERS':0}
    while not STOP.is_set():
        for name in counts:
            target=env_int(name,RUNTIME_SCHEMA[name][0])
            while counts[name]<target:
                index=counts[name];counts[name]+=1
                fn=(lambda idx=index:derivative_loop(idx)) if name=='TRANSCODE_WORKERS' else (lambda idx=index,live=name=='LIVE_WORKERS':worker(live,idx))
                t=threading.Thread(target=fn,daemon=True);t.start();WORKER_THREADS.append(t)
        STOP.wait(1)

def update_runtime():
    if not UPDATE_LOCK.acquire(blocking=False):
        return
    try:
        dest = DATA/'runtimes'/uuid.uuid4().hex
        log('檢查並更新 yt-dlp…')
        dest.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable,'-m','venv',str(dest)],check=True,timeout=120,capture_output=True)
        args = [str(dest/'bin/python'),'-m','pip','install','--no-cache-dir','--upgrade','gallery-dl','yt-dlp[default]']
        if env_value('YTDLP_CHANNEL','stable')=='nightly':
            args.insert(-1,'--pre')
        result = subprocess.run(args,capture_output=True,text=True,timeout=600)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        version = subprocess.check_output([str(dest/'bin/python'),'-m','yt_dlp','--version'],text=True,timeout=20).strip()
        old = subprocess.check_output(ytdlp()+['--version'],text=True,timeout=20).strip()
        if version == old and subprocess.run([ytdlp()[0],'-m','gallery_dl','--version'],capture_output=True).returncode==0 and subprocess.check_output([str(dest/'bin/python'),'-m','gallery_dl','--version'],text=True).strip()==subprocess.check_output([ytdlp()[0],'-m','gallery_dl','--version'],text=True).strip():
            import shutil
            shutil.rmtree(dest)
            log(f'yt-dlp {version} 已是最新版本')
        else:
            temp = DATA/'runtime-next'
            temp.write_text(str(dest))
            temp.replace(DATA/'runtime-current')
            log(f'yt-dlp {old} → {version}；新任務使用新版，現有錄製不中斷')
        execute('DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 100000)')
    except Exception as exc:
        log(f'更新失敗，保留現有版本：{exc}',level='ERROR')
    finally:
        UPDATE_LOCK.release()

def update_loop():
    while not STOP.is_set():
        update_runtime()
        wait_config(env_int('UPDATE_SECONDS',86400))

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def authorized(self):
        expected = os.getenv('WEB_USER','admin')+':'+os.getenv('WEB_PASSWORD','')
        try:
            header = self.headers.get('Authorization','')
            actual = base64.b64decode(header[6:],validate=True).decode() if header.startswith('Basic ') else ''
            return bool(os.getenv('WEB_PASSWORD')) and hmac.compare_digest(actual.encode(),expected.encode())
        except Exception:
            return False

    def respond(self,obj,status=200):
        body=json.dumps(obj,ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self,path,content_type,attachment=False):
        size=path.stat().st_size
        start,end=0,size-1
        partial=False
        header=self.headers.get('Range') if not attachment else None
        if header:
            try:
                match=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
                if not match or not any(match.groups()) or size==0:
                    raise ValueError()
                left,right=match.groups()
                if left:
                    start=int(left);end=min(int(right),size-1) if right else size-1
                else:
                    length=int(right)
                    if length==0:raise ValueError()
                    start=max(0,size-length)
                if start>=size or end<start:raise ValueError()
                partial=True
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range',f'bytes */{size}')
                self.send_header('Content-Length','0');self.end_headers();return
        self.send_response(206 if partial else 200)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(max(0,end-start+1)))
        self.send_header('Accept-Ranges','bytes')
        self.send_header('X-Content-Type-Options','nosniff')
        if partial:
            self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        if attachment:
            self.send_header('Content-Disposition',"attachment; filename*=UTF-8''"+quote(path.name))
        self.end_headers()
        if self.command=='HEAD':return
        with path.open('rb') as f:
            f.seek(start);remaining=end-start+1
            while remaining>0:
                chunk=f.read(min(1024*1024,remaining))
                if not chunk:break
                self.wfile.write(chunk);remaining-=len(chunk)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path=='/health':
            return self.respond({'ok':True})
        if not self.authorized():
            self.send_response(401)
            self.send_header('WWW-Authenticate','Basic realm="Channel Vault", charset="UTF-8"')
            self.end_headers()
            return
        u=urlsplit(self.path)
        q=parse_qs(u.query)
        try:
            if u.path=='/api/settings':
                return self.respond(settings())
            if u.path=='/api/channels':
                result=rows('''SELECT c.*,
                  (SELECT COUNT(*) FROM jobs WHERE channel_id=c.id AND status='done') AS downloaded,
                  (SELECT COUNT(*) FROM jobs WHERE channel_id=c.id AND status='recording') AS recording
                  FROM channels c ORDER BY id DESC''')
                for c in result:
                    c['sources']=rows('SELECT * FROM sources WHERE channel_id=? ORDER BY id',(c['id'],))
                return self.respond(result)
            match=re.fullmatch(r'/api/channels/(\d+)/images',u.path)
            if match:
                return self.respond(rows('SELECT id,source_id,role,mime,created FROM images WHERE channel_id=? ORDER BY id DESC',(int(match[1]),)))
            if u.path=='/api/jobs':
                where='1=1'; args=[]
                if q.get('channel'):
                    where+=' AND j.channel_id=?'; args.append(int(q['channel'][0]))
                if q.get('single'):
                    where+=' AND j.channel_id IS NULL'
                if q.get('kind'):
                    where+=' AND j.kind=?'; args.append(q['kind'][0])
                if q.get('status'):
                    where+=' AND j.status=?'; args.append(q['status'][0])
                result=rows(f'SELECT j.*,s.platform AS source_platform,s.url AS source_url FROM jobs j LEFT JOIN sources s ON s.id=j.source_id WHERE {where} ORDER BY j.id DESC LIMIT 100 OFFSET ?',args+[max(0,int(q.get('offset',['0'])[0]))])
                for j in result:
                    j['files']=rows('SELECT id,path,role FROM files WHERE job_id=? ORDER BY id',(j['id'],))
                return self.respond(result)
            if u.path=='/api/logs':
                where='id>?'; args=[int(q.get('after',['0'])[0])]
                if q.get('job'):
                    where+=' AND job=?';args.append(int(q['job'][0]))
                return self.respond(rows(f'SELECT * FROM (SELECT * FROM logs WHERE {where} ORDER BY id DESC LIMIT 300) ORDER BY id',args))
            if u.path=='/api/status':
                return self.respond({'version':subprocess.check_output(ytdlp()+['--version'],text=True,timeout=10).strip(),
                    'queues':rows('SELECT status,COUNT(*) AS count FROM jobs GROUP BY status'),
                    'live_poll_seconds':env_int('LIVE_POLL_SECONDS',30)})
            asset=re.fullmatch(r'/asset/channel/(\d+)/(avatar|banner)',u.path)
            if asset:
                found=rows(f'SELECT {asset[2]} AS mime,active_{asset[2]} AS active FROM channels WHERE id=?',(int(asset[1]),))
                path=DATA/'assets'/f'channel-{asset[1]}-{asset[2]}.img'
                if found and found[0]['active']:
                    item=rows('SELECT path,mime FROM images WHERE id=?',(found[0]['active'],))
                    if item:path=DATA/'assets'/item[0]['path'];found[0]['mime']=item[0]['mime']
                if not found or not path.is_file():
                    return self.respond({'error':'圖片尚未取得'},404)
                return self.send_file(path,found[0]['mime'] or 'image/jpeg')
            asset=re.fullmatch(r'/asset/image/(\d+)',u.path)
            if asset:
                found=rows('SELECT path,mime FROM images WHERE id=?',(int(asset[1]),))
                if not found:return self.respond({'error':'找不到圖片'},404)
                path=DATA/'assets'/found[0]['path']
                if not path.is_file():return self.respond({'error':'圖片已移除'},404)
                return self.send_file(path,found[0]['mime'])
            file_match=re.fullmatch(r'/(file|stream)/(\d+)',u.path)
            if file_match:
                found=rows('SELECT path FROM files WHERE id=?',(int(file_match[2]),))
                if not found:
                    return self.respond({'error':'找不到檔案'},404)
                path=safe_file(MEDIA/found[0]['path'])
                if not path.is_file():
                    return self.respond({'error':'檔案已從磁碟移除'},404)
                return self.send_file(path,mimetypes.guess_type(path.name)[0] or 'application/octet-stream',file_match[1]=='file')
            path=WEB/('index.html' if u.path=='/' else u.path.lstrip('/'))
            if not path.resolve().is_relative_to(WEB.resolve()) or not path.is_file():
                return self.respond({'error':'Not found'},404)
            body=path.read_bytes()
            self.send_response(200)
            self.send_header('Content-Type','text/html; charset=utf-8' if path.suffix=='.html' else 'text/javascript; charset=utf-8' if path.suffix=='.js' else 'text/css; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('X-Content-Type-Options','nosniff')
            self.end_headers();self.wfile.write(body)
        except (BrokenPipeError,ConnectionResetError):
            pass
        except Exception as exc:
            self.respond({'error':str(exc)},400)

    def do_POST(self):
        if not self.authorized():
            return self.respond({'error':'Unauthorized'},401)
        # JSON-only mutations prevent cross-site HTML form requests.
        if self.headers.get('Content-Type','').split(';')[0]!='application/json':
            return self.respond({'error':'Expected application/json'},415)
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=3*1024*1024:
                raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(size))
            route=urlsplit(self.path).path
            if route=='/api/settings':
                return self.respond(save_settings(body))
            if route=='/api/secrets':
                return self.respond(save_secret(body))
            if route=='/api/channels':
                platform,url=source_url(body['url'])
                name=str(body.get('name') or url.rsplit('/',1)[1]).strip()[:100]
                with db() as con:
                    ident=con.execute('INSERT INTO channels(url,name,custom_name) VALUES(?,?,?)',(url,name,int(bool(body.get('name'))))).lastrowid
                    con.execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(ident,platform,url))
                log(f'新增訂閱：{url}')
                return self.respond({'id':ident},201)
            match=re.fullmatch(r'/api/channels/(\d+)/sources',route)
            if match:
                cid=int(match[1]);platform,url=source_url(body['url'])
                if not rows('SELECT id FROM channels WHERE id=?',(cid,)):raise ValueError('找不到此訂閱')
                ident=execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(cid,platform,url))
                execute('UPDATE channels SET custom_name=1 WHERE id=?',(cid,))
                return self.respond({'id':ident},201)
            match=re.fullmatch(r'/api/sources/(\d+)/toggle',route)
            if match:
                execute('UPDATE sources SET enabled=1-enabled,scan_due=0 WHERE id=?',(int(match[1]),))
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/channels/(\d+)/name',route)
            if match:
                name=str(body.get('name','')).strip()
                if not name or len(name)>100:raise ValueError('名稱必須為 1–100 字')
                execute('UPDATE channels SET name=?,custom_name=1 WHERE id=?',(name,int(match[1])))
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/channels/(\d+)/images/refresh',route)
            if match:
                cid=int(match[1])
                if not rows('SELECT id FROM channels WHERE id=?',(cid,)):raise ValueError('找不到此訂閱')
                threading.Thread(target=refresh_images,args=(cid,),daemon=True).start()
                return self.respond({'ok':True},202)
            match=re.fullmatch(r'/api/channels/(\d+)/images/select',route)
            if match:
                cid=int(match[1])
                item=rows('SELECT * FROM images WHERE id=? AND channel_id=?',(int(body['id']),cid))
                if not item:raise ValueError('此圖片不屬於目前訂閱')
                role=item[0]['role']
                execute(f'UPDATE channels SET active_{role}=?,{role}=? WHERE id=?',(item[0]['id'],item[0]['mime'],cid))
                return self.respond({'ok':True})
            if route=='/api/download':
                url=link_url(body['url'])
                cid=int(body['channel_id']) if body.get('channel_id') else None
                source_id=None
                if cid:
                    if not rows('SELECT id FROM channels WHERE id=?',(cid,)):raise ValueError('找不到此訂閱')
                    found=rows('SELECT id FROM sources WHERE channel_id=? AND platform=? LIMIT 1',(cid,platform_for(url)))
                    source_id=found[0]['id'] if found else None
                ident=enqueue(cid,url,uuid.uuid4().hex,url,source_id=source_id)
                return self.respond({'id':ident},201)
            if route=='/api/update':
                threading.Thread(target=update_runtime,daemon=True).start()
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/channels/(\d+)/(scan|toggle|delete)',route)
            if match:
                cid=int(match[1]); action=match[2]
                if action=='scan':
                    execute('UPDATE channels SET scan_due=0 WHERE id=?',(cid,))
                    execute('UPDATE sources SET scan_due=0 WHERE channel_id=?',(cid,))
                elif action=='toggle':
                    execute('UPDATE channels SET enabled=1-enabled WHERE id=?',(cid,))
                else:
                    # Keep downloaded history and in-flight jobs; disable subscription.
                    execute('UPDATE channels SET enabled=0 WHERE id=?',(cid,))
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/jobs/(\d+)/retry',route)
            if match:
                execute("UPDATE jobs SET status='queued',attempts=0,retry_at=0,error=NULL WHERE id=? AND status IN ('failed','done')",(int(match[1]),))
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/jobs/(\d+)/process',route)
            if match:
                execute("UPDATE jobs SET derivative_status='pending',derivative_error=NULL WHERE id=? AND status='done' AND derivative_status!='processing'",(int(match[1]),))
                return self.respond({'ok':True})
            return self.respond({'error':'Not found'},404)
        except Exception as exc:
            self.respond({'error':str(exc)},400)

def shutdown(*_):
    STOP.set()
    with PROCESS_LOCK:
        for p in list(PROCESSES):
            try:
                os.killpg(p.pid,signal.SIGINT)
            except ProcessLookupError:
                pass

def main():
    if not os.getenv('WEB_PASSWORD'):
        sys.exit('Set WEB_PASSWORD before starting')
    initialize()
    signal.signal(signal.SIGTERM,shutdown)
    signal.signal(signal.SIGINT,shutdown)
    threads=[]
    targets=[scanning_loop,live_loop,update_loop,worker_supervisor]
    for target in targets:
        t=threading.Thread(target=target,daemon=True);t.start();threads.append(t)
    server=ThreadingHTTPServer(('0.0.0.0',8080),Handler)
    server.timeout=1
    log('Channel Vault 啟動，Web UI port 8080')
    while not STOP.is_set():
        server.handle_request()
    server.server_close()
    deadline=time.monotonic()+45
    for t in threads+WORKER_THREADS:
        t.join(max(0,deadline-time.monotonic()))

if __name__=='__main__':
    main()
