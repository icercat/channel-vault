"""Channel Vault: stdlib web API, persistent queues, isolated yt-dlp subprocesses."""
import base64
import hmac
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

DATA = Path(os.getenv('DATA_DIR', '/data'))
MEDIA = Path(os.getenv('DOWNLOAD_DIR', '/downloads'))
STOP = threading.Event()
PROCESSES = set()
PROCESS_LOCK = threading.Lock()
UPDATE_LOCK = threading.Lock()
WEB = Path(__file__).parent / 'web'

def env_int(name, default, minimum=1):
    return max(minimum, int(os.getenv(name, default)))

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
        ''')
        # Non-destructive upgrade of installations from v1.
        for table, columns in {
            'channels': {'avatar': 'TEXT', 'banner': 'TEXT', 'description': 'TEXT'},
            'jobs': {'thumbnail': 'TEXT', 'duration': 'REAL', 'height': 'INTEGER',
                     'derivative_status': "TEXT DEFAULT 'pending'", 'derivative_error': 'TEXT'},
            'files': {'role': "TEXT DEFAULT 'video'"},
        }.items():
            existing = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
            for name, declaration in columns.items():
                if name not in existing:
                    con.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
        con.execute("UPDATE jobs SET status='queued',retry_at=0,error='程序重啟，重新排程' WHERE status IN ('downloading','recording')")
        con.execute("UPDATE jobs SET derivative_status='pending' WHERE derivative_status='processing'")
    (DATA/'assets').mkdir(exist_ok=True)

THEMES = ('yt', 'twitch', 'trans', 'dark', 'light', 'neon', 'neon-pink')

def settings():
    values = {r['key']: json.loads(r['value']) for r in rows('SELECT * FROM settings')}
    return {'theme': values.get('theme', 'trans'), 'accent': values.get('accent', ''),
            'background': values.get('background', ''), 'surface': values.get('surface', '')}

def save_settings(values):
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
    return clean

def cache_channel_images(cid, meta):
    thumbs = meta.get('thumbnails') or []
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
            file = DATA/'assets'/f'channel-{cid}-{role}.img'
            temp=file.with_suffix('.tmp');temp.write_bytes(data);temp.replace(file)
            execute(f'UPDATE channels SET {role}=? WHERE id=?',(content_type,cid))
        except Exception as exc:
            log(f'頻道 {cid} {role} 圖片更新失敗：{exc}',level='WARN')
    if meta.get('description'):
        execute('UPDATE channels SET description=? WHERE id=?',(meta['description'],cid))

def image_url_allowed(url):
    u=urlsplit(url)
    host=u.hostname or ''
    return u.scheme=='https' and any(host==domain or host.endswith('.'+domain) for domain in ('ytimg.com','ggpht.com','googleusercontent.com'))

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
    u = urlsplit(value.strip())
    if u.scheme != 'https' or u.hostname not in ('www.youtube.com', 'youtube.com'):
        raise ValueError('訂閱請使用 https://www.youtube.com/@帳號 或 /channel/UC…')
    p = u.path.rstrip('/')
    p = re.sub(r'/(videos|shorts|streams|featured|live)$', '', p)
    if not re.fullmatch(r'/(@[\w.%-]+|channel/UC[\w-]+|c/[\w.%-]+|user/[\w.%-]+)', p):
        raise ValueError('請輸入頻道網址，而非影片或播放清單網址')
    return 'https://www.youtube.com' + p

def link_url(value):
    u = urlsplit(value.strip())
    if u.scheme != 'https' or u.hostname not in ('youtube.com','www.youtube.com','youtu.be','m.youtube.com','x.com','www.x.com','twitter.com','www.twitter.com') or u.username or u.password:
        raise ValueError('單次下載支援 YouTube、X.com、Twitter 的 HTTPS 網址')
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

def base_args():
    args = ['--ignore-config', '--no-colors', '--js-runtimes', 'deno', '--socket-timeout', '20']
    if Path('/cookies/cookies.txt').is_file():
        # yt-dlp may save cookies; mount a directory so atomic file replacement is possible.
        args += ['--cookies', '/cookies/cookies.txt']
    return args

def probe(url, flat=False, limit=None, timeout=90):
    args = ytdlp() + base_args() + ['--dump-single-json','--skip-download']
    if flat:
        args += ['--flat-playlist']
    else:
        args += ['--no-playlist']
    if limit:
        args += ['--playlist-end', str(limit)]
    result = subprocess.run(args + ['--',url], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr[-2000:] or 'yt-dlp metadata failed')
    return json.loads(result.stdout)

def enqueue(cid, url, key, title, kind='video', live=False):
    now = time.time()
    with db() as con:
        if cid is not None:
            old = con.execute('SELECT * FROM jobs WHERE channel_id=? AND media_key=?',(cid,key)).fetchone()
            if old:
                # A scheduled/live item can be promoted ahead of backlog workers.
                if live and old['status'] in ('queued','failed'):
                    con.execute("UPDATE jobs SET live=1,kind='live',status='queued',retry_at=0 WHERE id=?",(old['id'],))
                return old['id']
        return con.execute('INSERT INTO jobs(channel_id,media_key,url,title,kind,live,created,updated) VALUES(?,?,?,?,?,?,?,?)',
            (cid,key,url,title,kind,int(live),now,now)).lastrowid

def item_url(entry):
    ident = entry.get('id')
    return f'https://www.youtube.com/watch?v={ident}' if ident else None

def scan_channel(c):
    errors = []
    for suffix, kind in [('videos','video'),('shorts','video'),('streams','live')]:
        if STOP.is_set():
            return
        try:
            meta = probe(c['url']+'/'+suffix, flat=True, timeout=1800)
            if meta.get('channel'):
                execute('UPDATE channels SET name=? WHERE id=?',(meta['channel'],c['id']))
            if suffix == 'videos':
                cache_channel_images(c['id'],meta)
            count = 0
            for e in meta.get('entries') or []:
                if not e or not item_url(e):
                    continue
                status = e.get('live_status')
                if status == 'is_upcoming':
                    continue
                ident=enqueue(c['id'], item_url(e), e['id'], e.get('title') or e['id'],kind,status=='is_live')
                update_job_metadata(ident,e)
                count += 1
            log(f"{c['name']} /{suffix}：已掃描 {count} 項")
        except Exception as exc:
            errors.append(f'{suffix}: {exc}')
            log(f"頻道掃描 {c['name']} /{suffix}：{exc}",level='ERROR')
    execute('UPDATE channels SET last_scan=?,error=? WHERE id=?',(time.time(),'\n'.join(errors) or None,c['id']))

LIVE_PROBE_CACHE = {}
def poll_live(c):
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
            cache_key = (c['id'],e['id'])
            if LIVE_PROBE_CACHE.get(cache_key,0)>now:
                continue
            detail = probe(item_url(e),timeout=45)
            status = detail.get('live_status')
            LIVE_PROBE_CACHE[cache_key] = now + (env_int('LIVE_POLL_SECONDS',30) if status=='is_upcoming' else 3600)
        if status == 'is_live':
            enqueue(c['id'],item_url(e),e['id'],e.get('title') or e['id'],'live',True)
    execute('UPDATE channels SET last_live=? WHERE id=?',(time.time(),c['id']))

def scanning_loop():
    while not STOP.wait(2):
        found = rows('SELECT * FROM channels WHERE enabled=1 AND scan_due<=? ORDER BY scan_due LIMIT 1',(time.time(),))
        if found:
            c = found[0]
            execute('UPDATE channels SET scan_due=? WHERE id=?',(time.time()+env_int('SCAN_SECONDS',21600),c['id']))
            scan_channel(c)

def live_loop():
    while not STOP.is_set():
        started = time.monotonic()
        for c in rows('SELECT * FROM channels WHERE enabled=1'):
            try:
                poll_live(c)
            except Exception as exc:
                log(f"直播檢查 {c['name']}：{exc}",level='WARN')
        STOP.wait(max(1,env_int('LIVE_POLL_SECONDS',30)-(time.monotonic()-started)))

def claim(live):
    with db() as con:
        con.execute('BEGIN IMMEDIATE')
        r = con.execute('''SELECT j.* FROM jobs j LEFT JOIN channels c ON c.id=j.channel_id
          WHERE j.status='queued' AND j.live=? AND j.retry_at<=?
          AND (j.channel_id IS NULL OR c.enabled=1) ORDER BY j.id LIMIT 1''',(int(live),time.time())).fetchone()
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
        live = status == 'is_live'
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
        args = ytdlp()+base_args()+['--no-playlist','--newline','--progress','--continue',
            '--retries','10','--fragment-retries','10','--retry-sleep','5',
            '--no-abort-on-error','--abort-on-unavailable-fragments',
            '--download-archive',str(folder/'archive.txt'),
            '--write-info-json','--write-thumbnail','--merge-output-format','mkv',
            '-f','bv*+ba/b','--format-sort-force','-S','res,fps,vbr,abr','-o',template,
            '--print','after_move:VAULT_FILE:%(filepath)j',
            '--print-to-file','after_move:%(filepath)j',str(folder/'completed.jsonl')]
        if live:
            args += ['--live-from-start' if os.getenv('LIVE_FROM_START','false').lower()=='true' else '--no-live-from-start']
        log(f"{'開始直播錄製' if live else '開始下載'}：{j['url']}",jobid)
        p = subprocess.Popen(args+['--',j['url']],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,start_new_session=True)
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

def worker(live=False):
    while not STOP.wait(1):
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

def derivative_loop():
    while not STOP.wait(2):
        with db() as con:
            con.execute('BEGIN IMMEDIATE')
            found=con.execute("SELECT id FROM jobs WHERE status='done' AND derivative_status='pending' ORDER BY id LIMIT 1").fetchone()
            if found:
                con.execute("UPDATE jobs SET derivative_status='processing' WHERE id=?",(found['id'],))
        if found:
            derivatives(found['id'])

def update_runtime():
    if not UPDATE_LOCK.acquire(blocking=False):
        return
    try:
        dest = DATA/'runtimes'/uuid.uuid4().hex
        log('檢查並更新 yt-dlp…')
        dest.parent.mkdir(exist_ok=True)
        subprocess.run([sys.executable,'-m','venv',str(dest)],check=True,timeout=120,capture_output=True)
        args = [str(dest/'bin/python'),'-m','pip','install','--no-cache-dir','--upgrade','yt-dlp[default]']
        if os.getenv('YTDLP_CHANNEL','stable')=='nightly':
            args.insert(-1,'--pre')
        result = subprocess.run(args,capture_output=True,text=True,timeout=600)
        if result.returncode:
            raise RuntimeError(result.stderr[-2000:])
        version = subprocess.check_output([str(dest/'bin/python'),'-m','yt_dlp','--version'],text=True,timeout=20).strip()
        old = subprocess.check_output(ytdlp()+['--version'],text=True,timeout=20).strip()
        if version == old:
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
        STOP.wait(env_int('UPDATE_SECONDS',86400))

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
                return self.respond(rows('''SELECT c.*,
                  (SELECT COUNT(*) FROM jobs WHERE channel_id=c.id AND status='done') AS downloaded,
                  (SELECT COUNT(*) FROM jobs WHERE channel_id=c.id AND status='recording') AS recording
                  FROM channels c ORDER BY id DESC'''))
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
                result=rows(f'SELECT j.* FROM jobs j WHERE {where} ORDER BY j.id DESC LIMIT 100 OFFSET ?',args+[max(0,int(q.get('offset',['0'])[0]))])
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
                found=rows(f'SELECT {asset[2]} AS mime FROM channels WHERE id=?',(int(asset[1]),))
                path=DATA/'assets'/f'channel-{asset[1]}-{asset[2]}.img'
                if not found or not path.is_file():
                    return self.respond({'error':'圖片尚未取得'},404)
                return self.send_file(path,found[0]['mime'] or 'image/jpeg')
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
            if not 0<size<=16384:
                raise ValueError('Invalid request size')
            body=json.loads(self.rfile.read(size))
            route=urlsplit(self.path).path
            if route=='/api/settings':
                return self.respond(save_settings(body))
            if route=='/api/channels':
                url=channel_url(body['url'])
                ident=execute('INSERT INTO channels(url,name) VALUES(?,?)',(url,url.rsplit('/',1)[1]))
                log(f'新增訂閱：{url}')
                return self.respond({'id':ident},201)
            if route=='/api/download':
                url=link_url(body['url'])
                ident=enqueue(None,url,uuid.uuid4().hex,url)
                return self.respond({'id':ident},201)
            if route=='/api/update':
                threading.Thread(target=update_runtime,daemon=True).start()
                return self.respond({'ok':True})
            match=re.fullmatch(r'/api/channels/(\d+)/(scan|toggle|delete)',route)
            if match:
                cid=int(match[1]); action=match[2]
                if action=='scan':
                    execute('UPDATE channels SET scan_due=0 WHERE id=?',(cid,))
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
    targets=[scanning_loop,live_loop,update_loop]+[lambda:worker(False)]*env_int('DOWNLOAD_WORKERS',2)+[lambda:worker(True)]*env_int('LIVE_WORKERS',4)+[derivative_loop]*env_int('TRANSCODE_WORKERS',1)
    for target in targets:
        t=threading.Thread(target=target,daemon=True);t.start();threads.append(t)
    server=ThreadingHTTPServer(('0.0.0.0',8080),Handler)
    server.timeout=1
    log('Channel Vault 啟動，Web UI port 8080')
    while not STOP.is_set():
        server.handle_request()
    server.server_close()
    deadline=time.monotonic()+45
    for t in threads:
        t.join(max(0,deadline-time.monotonic()))

if __name__=='__main__':
    main()
