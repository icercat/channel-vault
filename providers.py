"""URL normalization and official X API discovery (download still uses yt-dlp)."""
import json
import re
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

def platform_for(url):
    host=(urlsplit(url).hostname or '').lower()
    if host in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be'):return 'youtube'
    if host in ('twitch.tv','www.twitch.tv','clips.twitch.tv','m.twitch.tv'):return 'twitch'
    if host in ('x.com','www.x.com','twitter.com','www.twitter.com'):return 'twitter'
    raise ValueError('支援 YouTube、Twitch、Twitter / X')

def source_url(value):
    u=urlsplit(value.strip())
    if u.scheme!='https' or u.username or u.password or u.port not in (None,443):
        raise ValueError('請使用沒有自訂 port 的 HTTPS 網址')
    platform=platform_for(value)
    p=u.path.rstrip('/')
    if platform=='youtube':
        p=re.sub(r'/(videos|shorts|streams|featured|live)$','',p)
        if not re.fullmatch(r'/(@[\w.%-]+|channel/UC[\w-]+|c/[\w.%-]+|user/[\w.%-]+)',p):
            raise ValueError('請提供 YouTube 頻道網址')
        return platform,'https://www.youtube.com'+p
    if platform=='twitch':
        p=re.sub(r'/(videos|clips|about|schedule)$','',p)
        if not re.fullmatch(r'/[a-zA-Z0-9_]{2,25}',p) or p.lower() in ('/videos','/directory','/downloads','/settings'):
            raise ValueError('請提供 Twitch 頻道網址')
        return platform,'https://www.twitch.tv'+p.lower()
    p=re.sub(r'/(media|with_replies)$','',p)
    if not re.fullmatch(r'/[a-zA-Z0-9_]{1,15}',p) or p.lower() in ('/home','/i','/explore','/settings'):
        raise ValueError('請提供 Twitter / X 帳號首頁網址')
    return platform,'https://x.com'+p.lower()

def x_request(path,token,params=None):
    url='https://api.x.com/2/'+path
    if params:url+='?'+urlencode(params)
    try:
        with urlopen(Request(url,headers={'Authorization':'Bearer '+token,'User-Agent':'ChannelVault/3'}),timeout=30) as r:
            return json.load(r)
    except HTTPError as exc:
        # Never put credentials or request headers in logs.
        raise RuntimeError(f'X API HTTP {exc.code}；請檢查 token、API 存取權限／額度與限速') from None

def x_posts(username,token,user_id=None,since_id=None,history=False):
    if not token:raise RuntimeError('Twitter 帳號追蹤需要在設定頁提供官方 X API Bearer Token；cookies 供貼文影片下載使用')
    if not user_id:
        response=x_request('users/by/username/'+username,token)
        user_id=response.get('data',{}).get('id')
    if not user_id:raise RuntimeError('X API 找不到此帳號')
    params={'max_results':100,'tweet.fields':'attachments,created_at,entities','expansions':'attachments.media_keys','media.fields':'type,preview_image_url'}
    if since_id:params['since_id']=since_id
    seen=set()
    while True:
        result=x_request('users/'+user_id+'/tweets',token,params)
        if result.get('errors') and not result.get('data'):
            raise RuntimeError('X API 未回傳貼文，請檢查 API 存取權限')
        media={m['media_key']:m for m in result.get('includes',{}).get('media',[])}
        posts=[]
        for tweet in result.get('data') or []:
            attached=[media.get(k,{}) for k in tweet.get('attachments',{}).get('media_keys',[])]
            if any(m.get('type') in ('video','animated_gif') for m in attached):
                posts.append({'id':tweet['id'],'url':f"https://x.com/{username}/status/{tweet['id']}",'title':tweet.get('text','X 影片'),'thumbnail':next((m.get('preview_image_url') for m in attached if m.get('preview_image_url')),None)})
            for link in tweet.get('entities',{}).get('urls',[]):
                url=link.get('expanded_url','')
                if re.fullmatch(r'https://(?:www\.)?(?:x|twitter)\.com/i/broadcasts/[\w]+',url):
                    posts.append({'id':url.rsplit('/',1)[1],'url':url,'title':tweet.get('text','X 直播'),'broadcast':True})
        newest=result.get('meta',{}).get('newest_id')
        yield {'user_id':user_id,'newest_id':newest,'entries':posts}
        cursor=result.get('meta',{}).get('next_token')
        if not history or not cursor:break
        if cursor in seen:raise RuntimeError('X API 分頁 cursor 重複')
        seen.add(cursor);params['pagination_token']=cursor


def gallery_entries(messages, username, since_id=None):
    """Normalize gallery-dl URL messages into one job per video post."""
    seen=set()
    for message in messages:
        if not isinstance(message,list) or len(message)<3 or message[0]!=3:
            continue
        url,meta=message[1:3]
        if not isinstance(meta,dict):continue
        ident=str(meta.get('tweet_id') or '')
        if not ident.isdigit() or ident in seen:continue
        if since_id and int(ident)<=int(since_id):continue
        if meta.get('extension') not in ('mp4','webm','m3u8') and not ('video.twimg.com/' in url):continue
        seen.add(ident)
        yield {'id':ident,'url':f'https://x.com/{username}/status/{ident}',
               'title':meta.get('content') or meta.get('description') or 'X 影片',
               'thumbnail':meta.get('thumbnail')}
