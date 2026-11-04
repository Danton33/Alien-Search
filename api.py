#!/usr/bin/env python3
"""Alien Search: an unofficial, read-only reader for a fixed register of public government records.
Not affiliated with or endorsed by any government agency."""
from __future__ import annotations
import argparse, copy, datetime as dt, getpass, hashlib, hmac, ipaddress, json, os, pathlib, socket, sys
import difflib
from common import UNOFFICIAL, Problem, envelope, problem_envelope, utc, decode_text, text_status, TEXT_TYPES, retry_delay, retry_fields, MAX_RETRY_AFTER
from secure_http import fetch, TransportError
from storage import private_dir, load_or_create_token, default_data_dir, print_json, write_json, load_json, store_object, update_json
import acquire, robots
import re, threading, time, urllib.parse
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
MAX_BYTES = 20 * 1024 * 1024
TTL = 86400            # successful source reads and snapshots
FAIL_TTL = 900         # blocked/missing/drift/robots results are reused for 15 minutes unless refresh is requested
TRANSIENT = {'network', 'error'}  # transport, DNS and server errors: never served from cache
GOV_INTERVAL = 2.0     # seconds between requests to the same government host
ARCHIVE_INTERVAL = 5.0 # seconds between Internet Archive requests
ARCHIVE_HOSTS = {'web.archive.org', 'archive.org'}
EXTRA_HOSTS = {'archive.dni.gov','media.defense.gov','cdn.nationalarchives.gov.uk','geipan.fr','www.cnes-geipan.fr','war.gov','defense.gov'}
FALLBACK_CLASSES = {'blocked','missing','drift','robots'}
CACHE_FORMAT = 3       # bump when parsing changes, so cached reads are redone with the new rules
KINDS = ('all','pdf','archive','media','metadata','catalog','page')

def site_key(host):
    """One pause per site: www.example.gov and example.gov share a rate-limit cooldown."""
    host=(host or '').lower().rstrip('.')
    return host[4:] if host.startswith('www.') else host

def transport_problem(e):
    """Plain Problem for a transport error. Records.transport_problem also pauses a rate-limited site."""
    kind={401:'blocked',403:'blocked',404:'missing',410:'missing',429:'rate'}.get(e.status,e.kind)
    # Rate limits, dropped connections and interrupted or discarded downloads are worth trying again later.
    later=e.status==429 or kind in TRANSIENT or getattr(e,'retry_later',False)
    return Problem(kind,str(e),'after_wait' if later else 'no',e.retry_after)

def pdf_coverage(info):
    """Page coverage fields for a result: how many pages yielded text and which (scans) did not."""
    out={}
    if info and 'pages_with_text' in info:
        out['pages_with_text']=info['pages_with_text']
        if info.get('pages_without_text'): out['pages_without_text']=info['pages_without_text']
    return out

def with_variants(hosts):
    """Treat example.gov and www.example.gov as the same site (PURSUE files live on bare war.gov)."""
    out=set()
    for host in hosts:
        if not host: continue
        out.add(host)
        if host.startswith('www.'): out.add(host[4:])
        elif host.count('.')==1: out.add('www.'+host)
    return out

def copy_alternates(source):
    return [dict(a) for a in source.get('alternates',[])]

def link_kind(host, path):
    if path.endswith('.pdf'): return 'pdf'
    if path.endswith('.zip'): return 'archive'
    if path.endswith(('.mp4','.mov','.m4v','.webm','.mp3','.wav')): return 'media'
    if path.endswith(('.csv','.json','.xlsx','.xml')): return 'metadata'
    if host=='catalog.archives.gov': return 'catalog'
    return 'page'

class Page(HTMLParser):
    def __init__(self, base):
        super().__init__(convert_charrefs=True); self.base=base; self.skip=0; self.title_on=False
        self.title=[]; self.text=[]; self.links=[]; self.link=None; self.dates=[]
    def handle_starttag(self, tag, attrs):
        a=dict(attrs)
        if tag in ('script','style','noscript','template'): self.skip+=1
        if tag=='title': self.title_on=True
        if tag=='meta' and (a.get('property') or a.get('name','')).lower() in ('article:published_time','article:modified_time','dcterms.modified','dcterms.created','date'):
            self.dates.append({'field':a.get('property') or a.get('name'),'value':a.get('content')})
        if not self.skip and tag=='a' and a.get('href'): self.link={'url':urllib.parse.urljoin(self.base,a['href']),'label':''}
    def handle_endtag(self, tag):
        if tag in ('script','style','noscript','template') and self.skip: self.skip-=1
        if tag=='title': self.title_on=False
        if tag=='a' and self.link:
            self.links.append(self.link); self.link=None
    def handle_data(self, data):
        if self.skip: return
        data=' '.join(data.split())
        if data:
            self.text.append(data)
            if self.title_on: self.title.append(data)
            if self.link is not None: self.link['label']+=data+' '

class Records:
    def __init__(self, root=ROOT, data_dir=None):
        self.root=pathlib.Path(root)
        register=json.loads((self.root/'sources.json').read_text(encoding='utf-8'))
        self.sources=register['sources']; self.known_gaps=register.get('known_gaps',[])
        self.byid={s['id']:s for s in self.sources}
        self.hosts=with_variants({urllib.parse.urlsplit(s['url']).hostname for s in self.sources}|EXTRA_HOSTS)
        chosen = data_dir or os.environ.get('ALIEN_SEARCH_DATA_DIR')
        state = chosen or (default_data_dir() if self.root == ROOT else self.root/'.state')
        # A user-chosen existing folder keeps its permissions; files and subfolders created below are owner-only.
        self.state=private_dir(state,tighten_existing=not chosen)
        self.cache=private_dir(self.state/'cache')
        self.runtime=private_dir(self.state/'runtime')
        self.objects=private_dir(self.state/'objects')
        self.cooldowns=self.state/'site-pauses.json'
        self.io_lock=threading.RLock(); self.lock=threading.Lock(); self.last={}; self.archive_lock=threading.Lock()
    def source(self, sid):
        if sid not in self.byid:
            raise Problem('input','Unknown source ID: '+str(sid)[:20],next_steps=['List registered IDs with: python api.py sources (or sources --query WORD).'])
        return copy.deepcopy(self.byid[sid])
    SUMMARY_FIELDS = ('id','title','agency','url')
    def list(self, query='', country='', limit=50, full=False):
        if not 1<=limit<=100: raise Problem('input','limit must be 1..100')
        text=lambda s: ' '.join(v if isinstance(v,str) else json.dumps(v,ensure_ascii=False) for v in s.values())
        matches=[s for s in self.sources if query.casefold() in text(s).casefold() and (not country or s['country'].casefold()==country.casefold())]
        data=matches[:limit]
        summary=not full and len(data)>5
        if summary: data=[{k:s[k] for k in self.SUMMARY_FIELDS} for s in data]
        result=envelope(data=data,total=len(matches),returned=len(data),has_more=len(matches)>limit,notice=UNOFFICIAL)
        if summary:
            result['detail']='summary'
            result['next_steps']=['Full entries (notes, aliases, alternates): narrow the query to 5 or fewer matches, or add --full.']
        if not matches and query:
            words=[w for w in re.split(r'\W+',query.casefold()) if len(w)>2]
            gaps=[g for g in self.known_gaps if any(w in json.dumps(g).casefold() for w in words)]
            if gaps: result['known_gaps']=gaps
            result['next_steps']=['Nothing in the register matches. Search official government sites directly and label anything you find NEW-1, NEW-2 and so on, with its full URL and agency. Do not invent register IDs.']
        return result
    def validate_url(self,url):
        p=urllib.parse.urlsplit(url)
        if p.scheme!='https' or p.hostname not in self.hosts or p.username or p.password or p.port not in (None,443):
            raise Problem('input','Destination is outside the exact government host allowlist',next_steps=['This tool only fetches the registered government and archive sites. For other sites, use your own web tools or a browser. Do not edit the allowlist to reach them.'])
        try: addresses=socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM)
        except OSError: raise Problem('error','Destination DNS lookup failed','no')
        for info in addresses:
            if not ipaddress.ip_address(info[4][0]).is_global: raise Problem('input','Non-public destination address refused')
        return url
    # --- site pauses (HTTP 429) ----------------------------------------------------
    # A rate limit pauses the whole site, not just the source that hit it, and the pause is kept in
    # the state folder so separate CLI runs and the local server all honour it. Every request this
    # tool makes (reads, redirects, robots.txt, size checks, downloads, official APIs, link checks,
    # archive calls) passes through pace(), so that is where the pause is enforced.
    def note_rate(self,host,retry_after):
        """Record a site's pause; an existing longer pause is never shortened. Returns its end (epoch seconds)."""
        now=time.time(); until=now+retry_delay(retry_after,now); key=site_key(host)
        def change(pauses):
            pauses={k:v for k,v in pauses.items() if isinstance(v,(int,float)) and v>now}  # drop expired pauses
            pauses[key]=max(pauses.get(key,0),until)
            return pauses
        return update_json(self.cooldowns,change,default={},expect=dict)[key]
    def paused_until(self,host):
        until=load_json(self.cooldowns,{},expect=dict).get(site_key(host))
        return until if isinstance(until,(int,float)) and until>time.time() else None
    def pace(self,host,interval=None):
        until=self.paused_until(host)
        if until:
            raise Problem('rate',str(host)+' asked automated tools to pause; no request was sent. Other sources on this site wait too.','after_wait',not_before=until)
        interval=interval if interval is not None else (ARCHIVE_INTERVAL if host in ARCHIVE_HOSTS else GOV_INTERVAL)
        with self.lock:
            gap=interval-(time.monotonic()-self.last.get(host,-1e9))
            if gap>0: time.sleep(gap)
            self.last[host]=time.monotonic()
    def transport_problem(self,e,url=None):
        """Problem for a transport error; an HTTP 429 also pauses that whole site (see note_rate)."""
        problem=transport_problem(e)
        if problem.kind=='rate':
            host=getattr(e,'host',None) or (urllib.parse.urlsplit(url).hostname if url else None)
            if host: problem.not_before=self.note_rate(host,e.retry_after)
        return problem
    # --- robots.txt (RFC 9309) -------------------------------------------------
    def _robots_entry(self,host):
        path=self.cache/('robots-'+re.sub(r'[^a-z0-9.-]','_',host.lower())+'.json'); now=time.time()
        entry=load_json(path,None,expect=dict)
        if entry and now<entry.get('expires',0): return entry
        self.pace(host)
        try:
            body,_,_=fetch('https://'+host+'/robots.txt',self.hosts,512*1024)
            entry={'state':'ok','text':body.decode('utf-8','replace'),'expires':now+TTL}
        except TransportError as e:
            if e.status==429: self.note_rate(getattr(e,'host',None) or host,e.retry_after)
            if e.status and 400<=e.status<500 and e.status!=429:
                entry={'state':'unavailable','expires':now+TTL}  # RFC 9309: no usable robots.txt; access allowed
            else:
                entry={'state':'unreachable','reason':('HTTP %s'%e.status) if e.status else str(e),'expires':now+600}
        write_json(path,entry)
        return entry
    def check_robots(self,url):
        host=urllib.parse.urlsplit(url).hostname
        if host in ARCHIVE_HOSTS: return
        entry=self._robots_entry(host)
        if entry['state']=='unreachable':
            raise Problem('robots',host+"'s robots.txt could not be read ("+entry.get('reason','')+'); RFC 9309 treats that as "do not fetch". Try later, use an archived or alternate copy, or download it yourself and use import-file.','after_wait')
        if entry['state']=='ok' and not robots.allowed(entry['text'],url):
            raise Problem('robots',host+' asks automated tools not to fetch this path (robots.txt). Use an archived or alternate copy, or download it yourself and use import-file.','no')
    # --- live reads --------------------------------------------------------------
    def follow(self,url):
        """Site rules for a redirect destination: the same robots.txt check and pause as the first request."""
        self.check_robots(url); self.pace(urllib.parse.urlsplit(url).hostname)
    def get_bytes(self,url):
        self.validate_url(url); self.check_robots(url); self.pace(urllib.parse.urlsplit(url).hostname)
        try:
            body,final,headers=fetch(url,self.hosts,MAX_BYTES,on_redirect=self.follow)
            store_object(self.objects,hashlib.sha256(body).hexdigest(),data=body)
            return body,final,headers
        except TransportError as e:
            raise self.transport_problem(e,url) from None
    def _read(self,sid,refresh=False,query='',kind='all',limit=100,max_chars=4000):
        s=self.source(sid)
        if kind not in KINDS: raise Problem('input','kind must be one of: '+', '.join(KINDS))
        if not 1<=limit<=250 or not 0<=max_chars<=20000: raise Problem('input','limit must be 1..250; max_chars must be 0..20000')
        cache=self.cache/(sid+'.json'); now=time.time(); base=None
        cached=load_json(cache,None,expect=dict)
        # A cached read is reused only for the same address and the same parsing rules. A site's
        # request to pause (rate) is always honoured, even across an upgrade or a register change.
        if cached and (cached.get('class')=='rate' or (cached.get('source_url')==s['url'] and cached.get('cache_format')==CACHE_FORMAT)):
            age=now-cached.get('cache_created',0)
            if cached.get('class')=='rate':
                fresh=now<cached.get('retry_not_before',0)  # honour Retry-After even with refresh
            elif cached.get('ok'):
                fresh=age<TTL and not refresh
            elif cached.get('class') in TRANSIENT:
                fresh=False  # network/DNS/server errors are retried on the next call
            else:
                fresh=age<FAIL_TTL and not refresh
            if fresh: base=cached; base['cached']=True
        if base is None:
            base=envelope(source_id=sid,source_url=s['url'],agency=s['agency'],fetched_at=utc(),register_date_note=s.get('date'),evidence_status='candidate',cache_created=now,cache_format=CACHE_FORMAT,cached=False)
            try:
                b,final,headers=self.get_bytes(s['url'])
                ct=next((v for k,v in headers.items() if k.lower()=='content-type'),'application/octet-stream').split(';')[0].strip().lower()
                base.update(final_url=final,content_type=ct,bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),server_last_modified=next((v for k,v in headers.items() if k.lower()=='last-modified'),None))
                links=[]; text=''; info=None
                content_type=next((v for k,v in headers.items() if k.lower()=='content-type'),'')
                if (urllib.parse.urlsplit(s['url']).path.lower().endswith('.pdf') or ct=='application/pdf') and not b.startswith(b'%PDF'):
                    raise Problem('drift','Expected PDF bytes; source returned another format','no')
                if b.startswith(b'%PDF'):
                    base['format']='pdf'
                    try: info=acquire.pdf_text(b)
                    except ValueError: raise Problem('drift','PDF parser rejected document','no')
                    text=info['text']; base['text_extraction']=info['text_extraction']
                    if info['pages'] is not None: base['pages']=info['pages']
                    if info['locked']: base['pdf_locked']=True
                    base.update(pdf_coverage(info))
                elif ct in ('text/html','application/xhtml+xml'):
                    base['format']='html'; page=Page(final); page.feed(decode_text(b,content_type,html=True)); page.close()
                    base['title']=' '.join(page.title); base['source_dates']=page.dates
                    text='\n'.join(page.text); links=page.links
                    # Access-challenge responses are failures, even if the host returned HTTP 200.
                    if re.search(r'^(just a moment|access denied|attention required|verify you are human)',base['title'],re.I):
                        raise Problem('blocked','Source returned an access-challenge page','no')
                elif ct in TEXT_TYPES:
                    base['format']='text'; text=decode_text(b,content_type)  # kept literally: <REDACTED> stays visible
                else:
                    base['format']='other'
                base['text_status']=text_status(base['format'],info)
                seen=set(); ids=set(); out=[]
                for a in links:
                    url=urllib.parse.urldefrag(a.get('url',''))[0]; p=urllib.parse.urlsplit(url)
                    if p.scheme!='https' or p.hostname not in self.hosts or url in seen: continue
                    seen.add(url); path=urllib.parse.unquote(p.path).lower()
                    full=hashlib.sha256(url.encode()).hexdigest(); lid=full[:12]
                    if lid in ids: lid=full[:24]  # practically never needed; keeps every ID on a page unique
                    ids.add(lid)
                    out.append({'id':lid,'label':a.get('label','').strip(),'url':url,'kind':link_kind(p.hostname,path)})
                base.update(links=out,text=text,link_count=len(out))
            except Problem as e:
                base.update(ok=False,**{'class':e.kind},reason=str(e),retry=e.retry,retry_after=e.retry_after,evidence_status='quarantined',links=[],text='')
                if e.kind=='rate':
                    base['retry_not_before']=getattr(e,'not_before',None) or now+retry_delay(e.retry_after,now)
            write_json(cache,base)
        result=copy.deepcopy(base); all_links=result.pop('links',[])
        rows=[a for a in all_links if (kind=='all' or a['kind']==kind) and query.casefold() in (a['label']+' '+a['url']).casefold()]
        result['data']=rows[:limit]; result['returned']=len(result['data']); result['filtered_total']=len(rows); result['has_more']=len(rows)>limit
        text=result.pop('text',''); result['text_excerpt']=text[:max_chars]; result['text_truncated']=len(text)>max_chars
        result.pop('cache_created',None); result.pop('cache_format',None)
        if result.get('class')=='rate' and result.get('retry_not_before'):
            result.update(retry_fields(result.get('retry_after'),not_before=result['retry_not_before']))
        return result
    def read(self,sid,refresh=False,query='',kind='all',limit=100,max_chars=4000,archive_fallback=False):
        """Read a registered source. If the live page fails, follow the framework's order:
        an official alternate copy first (GovInfo, when the user has saved a key), then an archived
        copy (with archive_fallback), then the user's own download. Every copy keeps its label."""
        with self.io_lock:
            result=self._read(sid,refresh,query,kind,limit,max_chars)
            if not result['ok'] and result['class'] in FALLBACK_CLASSES:
                official=self._official_alternate(sid)
                if official: result['official_alternate']=official
                if archive_fallback and not (official or {}).get('available'):
                    result['archived_copy']=self._archived_copy(sid,max_chars)
            self._guide(sid,result)
            return result
    def _official_alternate(self,sid):
        """The official GPO copy through GovInfo, used only when this source has one and the user saved a key."""
        import official_apis
        if not self.byid[sid].get('govinfo_package'): return None
        try:
            if not official_apis.load_key(self,'govinfo'): return None
            found=official_apis.govinfo_summary(self,sid)
        except Problem as e:
            return {'available':False,'route':'GovInfo API','class':e.kind,'reason':str(e)}
        return {'available':True,'route':'GovInfo API','copy_kind':'official-alternate','title':found.get('title'),
                'date_issued':found.get('date_issued'),'download_links':found.get('download_links')}
    def _guide(self,sid,result):
        """Add plain next steps (and the source's alternate copies on failure) for assistants and agents."""
        s=self.byid[sid]; steps=[]
        if result['ok']:
            if result.get('data'):
                steps.append('To save a listed file: python api.py download %s LINK_ID. It shows the size first; add --confirm only after the user agrees.'%sid)
            if result.get('pdf_locked'):
                steps.append('This PDF is password-protected; ask the user to open it in their own PDF viewer.')
            if result.get('text_truncated'):
                steps.append('More text is available: add --max-chars (up to 20000).')
            status=result.get('text_status')
            if result.get('pages_without_text'):
                steps.append('PDF pages %s have no text layer (probably scanned images), so nothing on them was read. Do not describe those pages; open them or ask the user to.'%result['pages_without_text'])
            if status=='partial':
                steps.append('Only part of this document\'s text was extracted. Quote only what you can see and say the rest was not read.')
            elif status=='none' and not result.get('pdf_locked'):
                steps.append('No text was extracted (%s). Do not describe the contents; download it or ask the user to open it.'%(
                    result.get('text_extraction') or 'this format is not read as text'))
        else:
            kind=result['class']
            if kind in FALLBACK_CLASSES:
                # Same order as the framework: official alternates, then archives, then the user's own copy.
                official=result.get('official_alternate') or {}
                if official.get('available'):
                    steps.append('Use official_alternate (the official GPO copy via GovInfo). Save the PDF with: python api.py download %s --via govinfo (it shows the size first).'%sid)
                alternates=copy_alternates(s)
                if not official.get('available') and any(a.get('kind','').startswith('official') for a in alternates):
                    steps.append('Try the official alternates in alternates before any archive; routes marked needs_key work only if the user set up that key.')
                if any(a.get('kind')=='government-web-archive' and a.get('url') for a in alternates):
                    steps.append('A government web archive copy is listed in alternates; prefer it to the Internet Archive and label it an archived copy.')
                copy=result.get('archived_copy') or {}
                if copy.get('available'):
                    steps.append(('Otherwise use' if steps else 'Use')+' archived_copy, labelled as an archived copy captured %s; it is not the live page.'%copy.get('capture_timestamp'))
                steps.append('Or ask the user to download it in their own browser, then run: python api.py import-file %s PATH --verify-archive'%sid)
                if alternates: result['alternates']=alternates
            elif kind=='rate':
                steps.append('The site asked for a pause. Wait until retry_after_seconds have passed (retry_at); do not retry sooner. Other sources on the same site are paused too.')
            elif kind in TRANSIENT:
                steps.append('Try again later. If this environment cannot reach government sites at all, use your own web tools instead.')
        if steps: result['next_steps']=steps
    def _archived_copy(self,sid,max_chars):
        """Find the nearest archived capture for a source whose live page could not be read. Clearly labelled."""
        memo=self.cache/('fallback-'+sid+'.json'); ts=None
        saved=load_json(memo,None,expect=dict)
        known=bool(saved) and saved.get('source_url')==self.byid[sid]['url'] and time.time()-saved.get('saved',0)<TTL
        if known: ts=saved.get('timestamp')
        else:
            found=self.closest(sid)
            if not found['ok']:
                return {'available':False,'class':found['class'],'reason':found.get('reason')}
            snap=(found.get('archive_result') or {}).get('data') or {}
            ts=snap.get('timestamp')
            write_json(memo,{'timestamp':ts,'source_url':self.byid[sid]['url'],'saved':time.time()})
        if not ts:
            return {'available':False,'reason':'The Internet Archive has no capture of this page.'}
        snap=self.snapshot(sid,ts,max_chars)
        if not snap['ok']:
            return {'available':False,'class':snap['class'],'reason':snap.get('reason'),'capture_timestamp':ts}
        return {'available':True,'evidence_status':'archived-copy','label':'ARCHIVED COPY - not the live official page',
                'capture_timestamp':ts,'capture_utc':snap.get('capture_utc'),'replay_url':snap.get('replay_url'),
                'format':snap.get('format'),'title':snap.get('title'),'text_status':snap.get('text_status'),'text_excerpt':snap.get('text_excerpt',''),'text_truncated':snap.get('text_truncated'),
                'links':(snap.get('links') or [])[:100],'sha256':snap.get('sha256'),
                'note':'The live page could not be read. This is what the Internet Archive captured at the time shown; it is not evidence of what the page says today.'}
    # --- archive operations ------------------------------------------------------
    def _archive(self,sid,operation):
        from wayback_api import APIError
        s=self.source(sid)
        with self.archive_lock:
            gate=self.cache/'archive-retry.json'
            state=load_json(gate,None,expect=dict)
            if state:
                if time.time()<state.get('not_before',0):
                    return envelope('rate',source_id=sid,reason='Archive Retry-After cooldown is active',retry='after_wait',**retry_fields(not_before=state['not_before']))
            try: self.pace('web.archive.org')
            except Problem as e:
                if e.kind!='rate': raise
                return envelope('rate',source_id=sid,reason=str(e),retry='after_wait',**retry_fields(not_before=e.not_before))
            try:
                return operation(s)
            except APIError as e:
                reason=str(e); wait={'retry_after':e.retry_after} if e.retry_after is not None else {}
                if e.category=='rate':
                    now=time.time(); until=now+retry_delay(e.retry_after,now)
                    write_json(gate,{'not_before':until})
                    self.note_rate('web.archive.org',e.retry_after)
                    wait=retry_fields(e.retry_after,not_before=until,now=now)
                    reason+=' The Internet Archive asks people who think they were blocked in error to email info@archive.org.'
                return envelope(e.category,source_id=sid,source_url=s['url'],agency=s['agency'],reason=reason,retry=e.retry,**wait,evidence_status='quarantined')
    def _archive_cached(self,key,ttl,compute):
        path=self.cache/('archive-'+hashlib.sha256(json.dumps(key).encode()).hexdigest()[:32]+'.json')
        if path.exists() and time.time()-path.stat().st_mtime<ttl:
            result=load_json(path,None,expect=dict)
            if result is not None: result['cached']=True; return result
        result=compute()
        if result.get('ok'): write_json(path,result)
        return result
    def captures(self,sid,start=None,end=None,limit=5,cursor=None,mimetype='all'):
        from wayback_api import WaybackClient,date_bound,APIError
        s=self.source(sid)
        if not 1<=limit<=20: raise Problem('input','Archive limit must be 1..20')
        if mimetype not in ('all','text/html','application/pdf'): raise Problem('input','Invalid archive MIME filter')
        def run(s):
            r=WaybackClient().captures(s['url'],start=start,end=end,limit=limit,resume_key=cursor,mimetype=None if mimetype=='all' else mimetype)
            return envelope(source_id=sid,source_url=s['url'],agency=s['agency'],evidence_status='candidate',archive_result=r)
        try: past=bool(end) and date_bound(end,upper=True)<dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d%H%M%S')
        except APIError: past=False
        # Answers about periods that are already over rarely change; reuse them for a week.
        return self._archive_cached(['captures',s['url'],start,end,limit,cursor,mimetype],7*TTL if past else 3600,lambda: self._archive(sid,run))
    def closest(self,sid,timestamp=None):
        from wayback_api import WaybackClient,date_bound
        s=self.source(sid)
        def run(s):
            r=WaybackClient().closest(s['url'],timestamp)
            actual=r.get('source_as_of')
            return envelope(source_id=sid,source_url=s['url'],agency=s['agency'],evidence_status='candidate',archive_result=r,capture_after_requested_time=bool(actual>date_bound(timestamp)) if actual and timestamp else None)
        return self._archive_cached(['closest',s['url'],timestamp],TTL,lambda: self._archive(sid,run))
    @staticmethod
    def _capture_identity(original,timestamp,headers,final):
        from wayback_api import capture_identity
        return capture_identity(original,timestamp,headers,final)
    def _snapshot(self,sid,timestamp,max_chars=4000):
        from wayback_api import WaybackClient,APIError,date_bound,replay_url
        s=self.source(sid)
        if not isinstance(timestamp,str) or not re.fullmatch(r'\d{14}',timestamp): raise Problem('input','Use an exact 14-digit capture timestamp from captures or closest')
        try: date_bound(timestamp)
        except APIError as e: raise Problem('input',str(e))
        if not 0<=max_chars<=20000: raise Problem('input','max_chars must be 0..20000')
        key=hashlib.sha256((s['url']+'|'+timestamp).encode()).hexdigest()
        cache=self.cache/('snapshot-'+key+'.json')
        base=load_json(cache,None,expect=dict) if cache.exists() and time.time()-cache.stat().st_mtime<TTL else None
        if base and base.get('cache_format')!=CACHE_FORMAT: base=None
        if base: base['cached']=True
        else:
            def run(s):
                class ReplayClient(WaybackClient):
                    # Fetch the replay once; page_text below reuses the same response.
                    def _get(self,url):
                        if not hasattr(self,'saved'): self.saved=super()._get(url)
                        return self.saved
                client=ReplayClient(max_bytes=MAX_BYTES)
                replay=replay_url(s['url'],timestamp,raw=True)
                body,headers,final=client._get(replay)
                memento=self._capture_identity(s['url'],timestamp,headers,final)
                ctype=next((v for k,v in headers.items() if k.lower()=='content-type'),'').lower()
                if (urllib.parse.urlsplit(s['url']).path.lower().endswith('.pdf') or 'application/pdf' in ctype) and not body.startswith(b'%PDF'):
                    raise APIError('drift','Expected archived PDF bytes; replay returned another format')
                digest=hashlib.sha256(body).hexdigest()
                store_object(self.objects,digest,data=body)
                base=envelope(cache_format=CACHE_FORMAT,source_id=sid,source_url=s['url'],agency=s['agency'],evidence_status='candidate',capture_timestamp=timestamp,capture_utc=dt.datetime.strptime(timestamp,'%Y%m%d%H%M%S').replace(tzinfo=dt.timezone.utc).isoformat(),fetched_at=utc(),replay_url=replay,final_replay_url=final,memento_datetime=memento,content_type=ctype,bytes=len(body),sha256=digest,cached=False,publication_date=None)
                if body.startswith(b'%PDF'):
                    try: info=acquire.pdf_text(body)
                    except ValueError: raise APIError('drift','Archived PDF could not be parsed')
                    base.update(format='pdf',links=[],text=info['text'],text_status=text_status('pdf',info),text_extraction=info['text_extraction'],**pdf_coverage(info))
                    if info['pages'] is not None: base['pages']=info['pages']
                    if info['locked']: base['pdf_locked']=True
                elif any(x in ctype for x in ('text/html','application/xhtml+xml')+TEXT_TYPES):
                    r=client.page_text(s['url'],timestamp,max_chars=20000)
                    fmt='text' if any(x in ctype for x in TEXT_TYPES) else 'html'
                    base.update(format=fmt,title=r['data']['title'],text=r['data']['text'],links=r['data']['links'],text_status=text_status(fmt,partial=r.get('text_cut',False)))
                else: raise APIError('input','Snapshot is not supported HTML/text or PDF content')
                return base
            base=self._archive(sid,run)
            if not base['ok']: return base
            write_json(cache,base)
        result=copy.deepcopy(base); text=result.pop('text',''); result['text_excerpt']=text[:max_chars]
        result['text_truncated']=len(text)>max_chars; result.pop('cache_format',None)
        return result
    def snapshot(self,*args,**kwargs):
        with self.io_lock:
            return self._snapshot(*args,**kwargs)
    def compare_snapshots(self,sid,before,after,max_chars=4000):
        if not 1<=max_chars<=20000: raise Problem('input','Comparison max_chars must be 1..20000')
        if not re.fullmatch(r'\d{14}',before or '') or not re.fullmatch(r'\d{14}',after or '') or before>=after:
            raise Problem('input','Use two exact timestamps with before strictly earlier than after')
        older=self.snapshot(sid,before,20000)
        if not older['ok']: return older
        newer=self.snapshot(sid,after,20000)
        if not newer['ok']: return newer
        if older.get('format')!=newer.get('format'): raise Problem('input','Snapshot formats differ; inspect the snapshots separately')
        diff='\n'.join(difflib.unified_diff(older['text_excerpt'].splitlines(),newer['text_excerpt'].splitlines(),fromfile=before,tofile=after,lineterm=''))
        texts_available=all(x.get('text_status') in ('complete','partial') for x in (older,newer))
        return envelope(source_id=sid,source_url=older['source_url'],agency=older['agency'],evidence_status='candidate',before_capture=before,after_capture=after,before_replay_url=older['replay_url'],after_replay_url=newer['replay_url'],before_sha256=older['sha256'],after_sha256=newer['sha256'],bytes_identical=older['sha256']==newer['sha256'] if older.get('sha256') and newer.get('sha256') else None,text_comparison_available=texts_available,text_identical=older['text_excerpt']==newer['text_excerpt'] if texts_available else None,diff_text=diff[:max_chars] if texts_available else None,diff_truncated=len(diff)>max_chars or any(x['text_truncated'] or x.get('text_status')=='partial' for x in (older,newer)),fetched_at=utc())
    def inspect_link(self,sid,link_id):
        result=self.read(sid,max_chars=0)
        if not result['ok']: return result
        base=load_json(self.cache/(sid+'.json'),{},expect=dict)
        link=next((x for x in base.get('links',[]) if x['id']==link_id),None)
        if not link: raise Problem('input','Unknown link ID; discover links from this source first')
        b,final,headers=self.get_bytes(link['url'])
        return envelope(source_id=sid,source_url=link['url'],final_url=final,fetched_at=utc(),bytes=len(b),sha256=hashlib.sha256(b).hexdigest(),is_pdf=b.startswith(b'%PDF'),evidence_status='candidate')

def make_server(records,port,token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def send(self,obj,status=200):
            data=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode(); self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(data)))
            self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if self.headers.get('Host','').split(':')[0] not in ('127.0.0.1','localhost'):
                return self.send(envelope('input',reason='Invalid local Host'),400)
            if self.headers.get('Origin'):
                return self.send(envelope('input',reason='Browser origins are not supported'),403)
            p=urllib.parse.urlsplit(self.path); q=urllib.parse.parse_qs(p.query)
            if p.path=='/health': return self.send(envelope(service='Alien Search local API',local_only=True,notice=UNOFFICIAL))
            # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would drop the connection.
            supplied=self.headers.get('Authorization','').encode('latin-1','replace')
            if not hmac.compare_digest(supplied,('Bearer '+token).encode('ascii')):
                return self.send(envelope('auth',reason='Bearer token required'),401)
            try:
                one=lambda key,default='':q.get(key,[default])[0]
                if p.path=='/openapi.json': return self.send(json.loads((ROOT/'openapi.json').read_text(encoding='utf-8')))
                if p.path=='/v1/sources': return self.send(records.list(one('q'),one('country'),int(one('limit','50')),full=one('full','0')=='1'))
                parts=p.path.strip('/').split('/')
                if len(parts)>=3 and parts[:2]==['v1','sources']:
                    sid=parts[2]
                    if len(parts)==3: return self.send(envelope(data=records.source(sid)))
                    if len(parts)==4 and parts[3] in ('read','links'):
                        return self.send(records.read(sid,refresh=one('refresh','0')=='1',query=one('q'),kind=one('kind','all'),limit=int(one('limit','100')),max_chars=int(one('max_chars','4000')),archive_fallback=one('archive_fallback','1')=='1'))
                    if len(parts)==4 and parts[3]=='captures':
                        return self.send(records.captures(sid,one('start') or None,one('end') or None,int(one('limit','5')),one('cursor') or None,one('mimetype','all')))
                    if len(parts)==4 and parts[3]=='closest': return self.send(records.closest(sid,one('timestamp') or None))
                    if len(parts)==5 and parts[3]=='snapshots': return self.send(records.snapshot(sid,parts[4],int(one('max_chars','4000'))))
                    if len(parts)==4 and parts[3]=='compare': return self.send(records.compare_snapshots(sid,one('before'),one('after'),int(one('max_chars','4000'))))
                    if len(parts)==5 and parts[3]=='documents': return self.send(records.inspect_link(sid,parts[4]))
                self.send(envelope('input',reason='Unknown endpoint'),404)
            except (ValueError,Problem) as e:
                self.send(problem_envelope(e) if isinstance(e,Problem) else envelope('input',reason=str(e),retry='after_fix'),400)
            except Exception:
                self.send(envelope('error',reason='Local operation failed; inspect private state locally'),500)
        def version_string(self): return 'AlienSearch'
        def do_POST(self): self.send(envelope('input',reason='Read-only API'),405)
        do_PUT=do_DELETE=do_PATCH=do_POST
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)

def read_secret_from_stdin(service):
    if sys.stdin.isatty(): return getpass.getpass('Paste your '+service+' API key (input hidden): ')
    return sys.stdin.readline().strip()

def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--data-dir',help='Private state directory on user-owned storage'); sub=p.add_subparsers(dest='cmd',required=True)
    l=sub.add_parser('sources',help='Search the saved source register'); l.add_argument('--query',default=''); l.add_argument('--country',default=''); l.add_argument('--full',action='store_true',help='Full entries even when more than 5 sources match')
    r=sub.add_parser('read',help='Read one registered source page or PDF'); r.add_argument('source_id'); r.add_argument('--refresh',action='store_true'); r.add_argument('--query',default=''); r.add_argument('--kind',default='all',choices=KINDS); r.add_argument('--limit',type=int,default=100); r.add_argument('--max-chars',type=int,default=4000)
    r.add_argument('--no-archive-fallback',action='store_true',help='Do not look up an archived copy when the live page is blocked or missing')
    c=sub.add_parser('captures',help='List archive captures'); c.add_argument('source_id'); c.add_argument('--start'); c.add_argument('--end'); c.add_argument('--limit',type=int,default=5); c.add_argument('--cursor'); c.add_argument('--mimetype',default='all')
    c=sub.add_parser('closest',help='Find the nearest archive capture'); c.add_argument('source_id'); c.add_argument('--timestamp')
    c=sub.add_parser('snapshot',help='Read one exact archive capture'); c.add_argument('source_id'); c.add_argument('timestamp'); c.add_argument('--max-chars',type=int,default=4000)
    c=sub.add_parser('compare',help='Compare two archive captures'); c.add_argument('source_id'); c.add_argument('before'); c.add_argument('after'); c.add_argument('--max-chars',type=int,default=4000)
    d=sub.add_parser('download',help='Check a file size, then download it after you confirm'); d.add_argument('source_id'); d.add_argument('link_id',nargs='?'); d.add_argument('--confirm',action='store_true'); d.add_argument('--max-mb',type=int,default=acquire.DEFAULT_DOWNLOAD_MB); d.add_argument('--via',choices=['govinfo'])
    z=sub.add_parser('zip-list',help='List a downloaded ZIP without extracting'); z.add_argument('sha256'); z.add_argument('--limit',type=int,default=500)
    z=sub.add_parser('zip-extract',help='Extract chosen files from a downloaded ZIP'); z.add_argument('sha256'); z.add_argument('members',nargs='*'); z.add_argument('--pdfs',action='store_true'); z.add_argument('--max-mb',type=int,default=acquire.DEFAULT_EXTRACT_MB)
    i=sub.add_parser('import-file',help='Add a file you downloaded yourself, labelled user-supplied'); i.add_argument('source_id'); i.add_argument('path'); i.add_argument('--from-url'); i.add_argument('--verify-archive',action='store_true'); i.add_argument('--max-chars',type=int,default=2000)
    v=sub.add_parser('verify-sources',help='Check that registered links still respond (headers only)'); v.add_argument('--ids',help='Comma-separated source IDs; default all')
    k=sub.add_parser('set-key',help='Store your own optional API key (read from standard input)'); k.add_argument('service',choices=['govinfo','nara'])
    g=sub.add_parser('govinfo',help='Optional: look up the official GovInfo copy of a source (needs your own free key)'); g.add_argument('source_id')
    n=sub.add_parser('nara-search',help='Optional: search the National Archives Catalog API (needs your own key)'); n.add_argument('query'); n.add_argument('--limit',type=int,default=20)
    s=sub.add_parser('serve',help='Run the local read-only API'); s.add_argument('--port',type=int,default=8765)
    a=p.parse_args(); records=None
    try:
        records=Records(data_dir=a.data_dir)
        if a.cmd=='sources': result=records.list(a.query,a.country,full=a.full)
        elif a.cmd=='read': result=records.read(a.source_id,a.refresh,a.query,a.kind,a.limit,a.max_chars,archive_fallback=not a.no_archive_fallback)
        elif a.cmd=='captures': result=records.captures(a.source_id,a.start,a.end,a.limit,a.cursor,a.mimetype)
        elif a.cmd=='closest': result=records.closest(a.source_id,a.timestamp)
        elif a.cmd=='snapshot': result=records.snapshot(a.source_id,a.timestamp,a.max_chars)
        elif a.cmd=='compare': result=records.compare_snapshots(a.source_id,a.before,a.after,a.max_chars)
        elif a.cmd=='download': result=acquire.download(records,a.source_id,a.link_id,confirm=a.confirm,max_mb=a.max_mb,via=a.via)
        elif a.cmd=='zip-list': result=acquire.zip_list(records,a.sha256,a.limit)
        elif a.cmd=='zip-extract': result=acquire.zip_extract(records,a.sha256,a.members,pdfs=a.pdfs,max_mb=a.max_mb)
        elif a.cmd=='import-file': result=acquire.import_file(records,a.source_id,a.path,from_url=a.from_url,verify_archive=a.verify_archive,max_chars=a.max_chars)
        elif a.cmd=='verify-sources': result=acquire.verify_sources(records,[x.strip() for x in a.ids.split(',')] if a.ids else None)
        elif a.cmd in ('set-key','govinfo','nara-search'):
            import official_apis
            if a.cmd=='set-key': result=official_apis.set_key(records,a.service,read_secret_from_stdin(a.service))
            elif a.cmd=='govinfo': result=official_apis.govinfo_summary(records,a.source_id)
            else: result=official_apis.nara_search(records,a.query,a.limit)
        else:
            if not 1024<=a.port<=65535: raise Problem('input','port must be 1024..65535')
            token=load_or_create_token(records.runtime/'api-token.txt')
            print(f'Listening on loopback port {a.port}; token is in runtime/api-token.txt in your private state directory',flush=True)
            make_server(records,a.port,token).serve_forever(); return
        print_json(result)
        if isinstance(result,dict) and not result.get('ok',True): raise SystemExit(1)
    except Problem as e: print_json(problem_envelope(e)); raise SystemExit(1)
    except TransportError as e:
        problem=records.transport_problem(e) if records else transport_problem(e)
        print_json(problem_envelope(problem)); raise SystemExit(1)
    except ValueError as e:
        # Configuration problems (state directory, token file) are reported, not dumped as tracebacks.
        print_json(envelope('input',reason=str(e),retry='after_fix')); raise SystemExit(1)

if __name__=='__main__': main()
