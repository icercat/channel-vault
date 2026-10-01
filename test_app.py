import base64
import json
import os
import hashlib
import shutil
import subprocess
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from unittest.mock import patch
import app
import providers

class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        app.DATA=Path(self.tmp.name)/'data';app.MEDIA=Path(self.tmp.name)/'downloads'
        app.COOKIES=Path(self.tmp.name)/'cookies';app.STOP.clear();app.LIVE_PROBE_CACHE.clear();app.initialize()
        self.cid=app.execute('INSERT INTO channels(url,name) VALUES(?,?)',('https://www.youtube.com/@test','測試'))
        self.sid=app.execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(self.cid,'youtube','https://www.youtube.com/@test'))
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
        self.assertEqual(commands[0][commands[0].index('-f')+1],'bv*+ba/b')
        self.assertEqual(commands[0][commands[0].index('-S')+1],'res,fps,vbr,abr')
        self.assertEqual(app.rows('SELECT status FROM jobs WHERE id=?',(ident,))[0]['status'],'done')
        self.assertEqual(len(app.rows('SELECT * FROM files')),1)
    def test_file_path_escape_blocked(self):
        with self.assertRaises(ValueError):app.safe_file(app.MEDIA/'../secret')
    def test_settings_persist_and_validate(self):
        values={'theme':'neon-pink','accent':'#FF80DD','background':'','surface':''}
        app.save_settings(values)
        self.assertEqual({k:app.settings()[k] for k in values},values)
        app.initialize()
        self.assertEqual({k:app.settings()[k] for k in values},values)
        for value in [{'theme':'missing'},{'theme':'trans','accent':'url(javascript:bad)'}]:
            with self.assertRaises(ValueError):app.save_settings(value)
    def test_runtime_settings_persist_and_reject_auth_or_invalid_counts(self):
        values={'theme':'trans','runtime':{'LIVE_WORKERS':6,'SCAN_SECONDS':120,'YTDLP_CHANNEL':'nightly','LIVE_FROM_START':True}}
        app.save_settings(values);app.initialize()
        for name,value in values['runtime'].items():self.assertEqual(app.runtime_settings()[name],value)
        for runtime in [{'WEB_PASSWORD':'bad'},{'BIND_IP':'0.0.0.0'},{'LIVE_WORKERS':0},{'LIVE_WORKERS':True},{'YTDLP_CHANNEL':'bad'}]:
            with self.assertRaises(ValueError):app.save_settings({'theme':'trans','runtime':runtime})
    def test_platform_normalization_and_multi_source(self):
        self.assertEqual(providers.source_url('https://twitter.com/Example/media'),('twitter','https://x.com/example'))
        self.assertEqual(providers.source_url('https://www.twitch.tv/Example/videos'),('twitch','https://www.twitch.tv/example'))
        for url in ['https://www.twitch.tv/videos/123','https://x.com/user/status/123','https://evil.test/creator']:
            with self.assertRaises(ValueError):providers.source_url(url)
        twitch=app.execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(self.cid,'twitch','https://www.twitch.tv/tester'))
        app.enqueue(self.cid,'https://youtube.com/watch?v=a','a','YT',source_id=self.sid)
        app.enqueue(self.cid,'https://twitch.tv/videos/123','twitch:a','TW',source_id=twitch)
        self.assertEqual(len(app.rows('SELECT * FROM jobs WHERE channel_id=?',(self.cid,))),2)
        app.execute('UPDATE sources SET enabled=0 WHERE id=?',(twitch,))
        self.assertEqual(app.claim(False)['title'],'YT');self.assertIsNone(app.claim(False))
    def test_twitch_live_detection_and_archive_discovery(self):
        sid=app.execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(self.cid,'twitch','https://www.twitch.tv/tester'))
        source=app.rows('SELECT * FROM sources WHERE id=?',(sid,))[0]
        with patch.object(app,'probe',return_value={'is_live':True,'id':'stream123','timestamp':100,'title':'Live'}):
            app.poll_source(source);app.poll_source(source)
        self.assertEqual(len(app.rows('SELECT * FROM jobs WHERE live=1')),1)
        with patch.object(app,'probe',return_value={'entries':[{'id':'v123','url':'https://www.twitch.tv/videos/123','title':'Replay'}]}):app.scan_source(source)
        self.assertEqual(len(app.rows('SELECT * FROM jobs')),2)
        self.assertEqual(app.rows("SELECT kind FROM jobs WHERE media_key='twitch:v123'")[0]['kind'],'live')
    def test_twitter_api_pagination_filters_video_posts(self):
        responses=[{'data':{'id':'42'}},{'data':[{'id':'100','text':'Movie','attachments':{'media_keys':['m1']}},{'id':'99','text':'Photo','attachments':{'media_keys':['m2']}}],'includes':{'media':[{'media_key':'m1','type':'video'},{'media_key':'m2','type':'photo'}]},'meta':{'newest_id':'100','next_token':'next'}},{'data':[{'id':'90','text':'GIF','attachments':{'media_keys':['m3']}}],'includes':{'media':[{'media_key':'m3','type':'animated_gif'}]},'meta':{'newest_id':'90'}}]
        with patch.object(providers,'x_request',side_effect=responses):
            batches=list(providers.x_posts('tester','fake-token',history=True))
        self.assertEqual([p['id'] for b in batches for p in b['entries']],['100','90'])
        with self.assertRaises(RuntimeError):list(providers.x_posts('tester',''))
    def test_twitter_discovered_posts_join_same_library(self):
        sid=app.execute('INSERT INTO sources(channel_id,platform,url) VALUES(?,?,?)',(self.cid,'twitter','https://x.com/tester'))
        source=app.rows('SELECT * FROM sources WHERE id=?',(sid,))[0]
        batch={'user_id':'42','newest_id':'100','entries':[{'id':'100','url':'https://x.com/tester/status/100','title':'X movie'}]}
        with patch.object(app,'x_posts',return_value=[batch]):app.scan_twitter(source)
        job=app.rows('SELECT * FROM jobs')[0]
        self.assertEqual(job['channel_id'],self.cid);self.assertEqual(job['source_id'],sid)
    def test_cookie_write_only_and_per_platform_snapshots(self):
        content='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t2000000000\tSID\tTEST_SECRET_VALUE\n'
        result=app.save_secret({'platform':'youtube','content':content})
        self.assertTrue(result['cookies']['youtube']['configured'])
        self.assertNotIn('TEST_SECRET_VALUE',json.dumps(app.settings()))
        args=app.base_args('https://youtube.com/watch?v=a')
        snapshot=Path(args[args.index('--cookies')+1]);self.assertEqual(snapshot.read_text(),content)
        self.assertNotEqual(snapshot,app.COOKIES/'youtube.txt')
        app.cleanup_cookie_args(args);self.assertFalse(snapshot.exists())
        self.assertNotIn('--cookies',app.base_args('https://twitch.tv/tester'))
        app.save_secret({'platform':'youtube','enabled':False})
        self.assertNotIn('--cookies',app.base_args('https://youtube.com/watch?v=a'))
        with self.assertRaises(ValueError):app.save_secret({'platform':'youtube','content':'not cookies'})
        app.save_secret({'platform':'youtube','delete':True})
        self.assertFalse((app.COOKIES/'youtube.txt').exists())
    def test_image_history_preserves_choice_and_deduplicates(self):
        first=app.store_image(self.cid,'avatar',b'first','image/png')
        second=app.store_image(self.cid,'avatar',b'second','image/png')
        self.assertEqual(app.rows('SELECT active_avatar FROM channels')[0]['active_avatar'],first)
        self.assertEqual(app.store_image(self.cid,'avatar',b'second','image/png',activate=True),second)
        self.assertEqual(len(app.rows('SELECT * FROM images')),2)
        self.assertEqual(app.rows('SELECT active_avatar FROM channels')[0]['active_avatar'],second)
        for image in app.rows('SELECT * FROM images'):self.assertTrue((app.DATA/'assets'/image['path']).is_file())
    def test_channel_images_cached_from_extractor_metadata(self):
        class Response:
            url='https://yt3.ggpht.com/test'
            class Headers:
                def get_content_type(self):return 'image/jpeg'
            headers=Headers()
            def read(self,*args):return b'jpeg-sample'
            def __enter__(self):return self
            def __exit__(self,*args):pass
        metadata={'description':'Channel bio','thumbnails':[{'id':'avatar_uncropped','url':'https://yt3.ggpht.com/avatar=s0'},{'id':'banner_uncropped','url':'https://yt3.ggpht.com/banner=s0'}]}
        with patch.object(app,'urlopen',return_value=Response()):app.cache_channel_images(self.cid,metadata)
        self.assertTrue(app.rows("SELECT id FROM images WHERE channel_id=? AND role='avatar'",(self.cid,)))
        c=app.rows('SELECT * FROM channels')[0]
        self.assertEqual(c['banner'],'image/jpeg');self.assertEqual(c['description'],'Channel bio')
        self.assertFalse(app.image_url_allowed('https://ggpht.com.evil.test/image'))
    def test_conversion_shutdown_recovery(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a')
        app.execute("UPDATE jobs SET status='done',derivative_status='processing' WHERE id=?",(ident,))
        app.initialize()
        self.assertEqual(app.rows('SELECT derivative_status FROM jobs')[0]['derivative_status'],'pending')
    def test_legacy_database_migration_keeps_history(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','Legacy title')
        source=app.MEDIA/'legacy.mp4';source.write_bytes(b'legacy')
        app.register_file(ident,source)
        with app.db() as con:
            for table,names in {'channels':['avatar','banner','description'],'jobs':['thumbnail','duration','height','derivative_status','derivative_error'],'files':['role']}.items():
                for name in names:con.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
        app.initialize()
        self.assertEqual(app.rows('SELECT title FROM jobs WHERE id=?',(ident,))[0]['title'],'Legacy title')
        self.assertEqual(app.rows('SELECT role FROM files')[0]['role'],'video')
        self.assertEqual(app.rows('SELECT derivative_status FROM jobs')[0]['derivative_status'],'pending')
    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_mp3_and_browser_copy_preserve_original(self):
        ident=app.enqueue(self.cid,'https://youtu.be/a','a','a')
        source=app.MEDIA/'source.mkv'
        subprocess.run(['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=64x64:r=10:d=1','-f','lavfi','-i','sine=frequency=440:duration=1','-shortest','-c:v','ffv1','-c:a','pcm_s16le','-threads','1',str(source)],check=True,timeout=30,capture_output=True)
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        app.register_file(ident,source)
        app.derivatives(ident)
        files=app.rows('SELECT * FROM files WHERE job_id=?',(ident,))
        self.assertEqual({f['role'] for f in files},{'video','mp3','preview'})
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
        for f in files:
            info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(app.MEDIA/f['path'])],text=True))
            if f['role']=='mp3':self.assertEqual(info['streams'][0]['codec_name'],'mp3')
            if f['role']=='preview':self.assertEqual({s['codec_name'] for s in info['streams']},{'h264','aac'})
        self.assertEqual(app.rows('SELECT derivative_status FROM jobs')[0]['derivative_status'],'ready')
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
                group=json.loads(request('/api/channels',{'url':'https://www.youtube.com/@second','name':'One creator'},headers)[1])['id']
                source=json.loads(request(f'/api/channels/{group}/sources',{'url':'https://twitch.tv/second'},headers)[1])['id']
                groups=json.loads(request('/api/channels',headers=headers)[1])
                joined=next(c for c in groups if c['id']==group)
                self.assertEqual({s['platform'] for s in joined['sources']},{'youtube','twitch'})
                self.assertEqual(joined['name'],'One creator')
                request('/api/download',{'url':'https://twitch.tv/videos/123','channel_id':group},headers)
                joined_jobs=json.loads(request(f'/api/jobs?channel={group}',headers=headers)[1])
                self.assertEqual(joined_jobs[0]['source_platform'],'twitch')
                request(f'/api/sources/{source}/toggle',{},headers)
                first=app.store_image(group,'avatar',b'old image','image/png')
                second=app.store_image(group,'avatar',b'new image','image/png')
                gallery=json.loads(request(f'/api/channels/{group}/images',headers=headers)[1])
                self.assertEqual(len(gallery),2)
                request(f'/api/channels/{group}/images/select',{'id':second},headers)
                self.assertEqual(app.rows('SELECT active_avatar FROM channels WHERE id=?',(group,))[0]['active_avatar'],second)
                self.assertEqual(request(f'/asset/image/{first}',headers=headers)[1],b'old image')
                with self.assertRaises(urllib.error.HTTPError) as e:request(f'/api/channels/{self.cid}/images/select',{'id':second},headers)
                self.assertEqual(e.exception.code,400)
                settings={'theme':'twitch','accent':'','background':'','surface':''}
                self.assertEqual({k:json.loads(request('/api/settings',settings,headers)[1])[k] for k in settings},settings)
                self.assertEqual({k:json.loads(request('/api/settings',headers=headers)[1])[k] for k in settings},settings)
                media=app.MEDIA/'sample.mp4';media.write_bytes(b'0123456789')
                job=app.rows('SELECT id FROM jobs')[0]['id'];app.register_file(job,media,'preview')
                fileid=app.rows('SELECT id FROM files')[0]['id']
                range_headers={'Authorization':auth,'Range':'bytes=2-5'}
                code,data=request(f'/stream/{fileid}',headers=range_headers)
                self.assertEqual(code,206);self.assertEqual(data,b'2345')
                self.assertEqual(request(f'/stream/{fileid}',headers={'Authorization':auth,'Range':'bytes=-3'})[1],b'789')
                with self.assertRaises(urllib.error.HTTPError) as e:request(f'/stream/{fileid}',headers={'Authorization':auth,'Range':'bytes=20-'})
                self.assertEqual(e.exception.code,416)
                req=urllib.request.Request(base+f'/stream/{fileid}',method='HEAD',headers={'Authorization':auth})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual(r.headers['Accept-Ranges'],'bytes');self.assertEqual(r.read(),b'')
                self.assertEqual(request(f'/file/{fileid}',headers=headers)[1],b'0123456789')
                with self.assertRaises(urllib.error.HTTPError) as e:request('/api/download',{'url':'https://x.com/u/status/123'},{'Authorization':auth})
                self.assertEqual(e.exception.code,415)
            finally:
                server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
