import base64
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import app

class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        app.DATA=Path(self.tmp.name)/'data';app.MEDIA=Path(self.tmp.name)/'downloads'
        app.STOP.clear();app.LIVE_PROBE_CACHE.clear();app.initialize()
        self.cid=app.execute('INSERT INTO channels(url,name) VALUES(?,?)',('https://www.youtube.com/@test','測試'))
    def tearDown(self):
        self.tmp.cleanup()
    def test_subscription_normalization_and_rejection(self):
        self.assertEqual(app.channel_url('https://youtube.com/@test/streams'),'https://www.youtube.com/@test')
        for value in ['http://youtube.com/@test','https://evil.test/@test','https://youtube.com/watch?v=abc']:
            with self.assertRaises(ValueError):app.channel_url(value)
    def test_single_url_validation(self):
        self.assertEqual(app.link_url('https://x.com/u/status/123'),'https://x.com/u/status/123')
        for value in ['https://127.0.0.1/x','https://x.com:8080/u','https://user@x.com/u']:
            with self.assertRaises(ValueError):app.link_url(value)
    def test_scan_dedup_and_reserved_live_queue(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','影片')
        self.assertEqual(ident,app.enqueue(self.cid,'https://youtu.be/a','a','影片'))
        app.enqueue(self.cid,'https://youtu.be/a','a','直播','live',True)
        self.assertIsNone(app.claim(False))
        self.assertEqual(app.claim(True)['id'],ident)
        self.assertIsNone(app.claim(True))
    def test_pause_prevents_new_download_claims(self):
        app.enqueue(self.cid,'https://youtu.be/a','a','a')
        app.execute('UPDATE channels SET enabled=0 WHERE id=?',(self.cid,))
        self.assertIsNone(app.claim(False))
    def test_restart_requeues_active_jobs(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a','live',True)
        app.claim(True);app.initialize()
        self.assertEqual(app.rows('SELECT status FROM jobs WHERE id=?',(ident,))[0]['status'],'queued')
    def test_scan_handles_all_tabs_and_upcoming(self):
        def probe(url,**kwargs):
            return {'channel':'Real name','entries':[{'id':url.rsplit('/',1)[1],'title':'Title'}, {'id':'future','live_status':'is_upcoming'}]}
        with patch.object(app,'probe',side_effect=probe):app.scan_channel(app.rows('SELECT * FROM channels')[0])
        self.assertEqual(len(app.rows('SELECT * FROM jobs')),3)
        self.assertEqual(app.rows('SELECT kind FROM jobs WHERE media_key=?',('streams',))[0]['kind'],'live')
        self.assertEqual(app.rows('SELECT name FROM channels')[0]['name'],'Real name')
    def test_live_poll_discovers_multiple_active_streams(self):
        with patch.object(app,'probe',return_value={'entries':[{'id':'a','live_status':'is_live'},{'id':'b','live_status':'is_live'},{'id':'c','live_status':'is_upcoming'}]}) as p:
            # Probe upcoming video should return metadata, not a playlist.
            p.side_effect=[{'entries':[{'id':'a','live_status':'is_live'},{'id':'b','live_status':'is_live'},{'id':'c','live_status':'is_upcoming'}]}, {'live_status':'is_upcoming'}]
            app.poll_live(app.rows('SELECT * FROM channels')[0])
        self.assertEqual(len(app.rows('SELECT * FROM jobs WHERE live=1')),2)
    def test_flat_live_miss_promotes_to_live_worker(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a')
        j=app.claim(False)
        with patch.object(app,'probe',return_value={'title':'LIVE','live_status':'is_live'}),patch.object(app.subprocess,'Popen') as pop:
            app.download(j);pop.assert_not_called()
        self.assertEqual(app.claim(True)['id'],ident)
    def test_failures_backoff_and_stop_after_five_attempts(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a')
        with patch.object(app,'probe',side_effect=RuntimeError('network failed')):
            for _ in range(5):
                app.execute('UPDATE jobs SET retry_at=0 WHERE id=?',(ident,));app.download(app.claim(False))
        self.assertEqual(app.rows('SELECT status FROM jobs WHERE id=?',(ident,))[0]['status'],'failed')
    def test_download_live_command_and_final_media_manifest(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a','live',True)
        j=app.claim(True);commands=[]
        class FakeProcess:
            def __init__(self,args,**kwargs):
                commands.append(args)
                template=args[args.index('-o')+1]
                final=Path(template).parent/'test.mkv';final.write_bytes(b'video')
                manifest=Path(args[args.index('--print-to-file')+2]);manifest.write_text(json.dumps(str(final))+'\n')
                self.stdout=iter(['progress\n','VAULT_FILE:'+json.dumps(str(final))+'\n'])
            def wait(self):return 0
        with patch.object(app,'probe',return_value={'title':'LIVE','live_status':'is_live'}),patch.object(app.subprocess,'Popen',FakeProcess):
            app.download(j)
        self.assertIn('--no-live-from-start',commands[0])
        self.assertEqual(app.rows('SELECT status FROM jobs WHERE id=?',(ident,))[0]['status'],'done')
        self.assertEqual(len(app.rows('SELECT * FROM files')),1)
    def test_file_path_escape_blocked(self):
        with self.assertRaises(ValueError):app.safe_file(app.MEDIA/'../secret')
    def test_http_auth_subscription_and_download(self):
        with patch.dict(os.environ,{'WEB_USER':'admin','WEB_PASSWORD':'testing'}):
            server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{server.server_port}'
            auth='Basic '+base64.b64encode(b'admin:testing').decode()
            def request(path,body=None,headers=None):
                req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
                with urllib.request.urlopen(req) as r:return r.status,r.read()
            try:
                self.assertEqual(request('/health')[0],200)
                with self.assertRaises(urllib.error.HTTPError) as e:request('/api/channels')
                self.assertEqual(e.exception.code,401)
                headers={'Authorization':auth,'Content-Type':'application/json'}
                self.assertEqual(request('/api/download',{'url':'https://x.com/u/status/123'},headers)[0],201)
                code,data=request('/api/jobs?single=1',headers=headers)
                self.assertEqual(len(json.loads(data)),1)
                self.assertEqual(request('/',headers=headers)[0],200)
                with self.assertRaises(urllib.error.HTTPError) as e:request('/api/download',{'url':'https://x.com/u/status/123'},{'Authorization':auth})
                self.assertEqual(e.exception.code,415)
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
