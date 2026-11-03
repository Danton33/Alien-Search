"""Optional official APIs: GovInfo (U.S. Government Publishing Office) and the National Archives Catalog.

Nothing here is required. Each person uses their own free key, stored in their private state folder
(or an environment variable) and never printed, cached, written to the repository or sent to any
other host: secure_http drops the key header if a request is redirected away from the API host.
"""
import datetime as dt
import json
import os
import re
import stat
import urllib.parse

from common import Problem, envelope
from secure_http import TransportError, fetch
from storage import atomic_write, load_json, write_json

PRIVATE_OPEN = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)  # never follow links; never let Windows translate line endings in files written byte for byte
NARA_NOTICE = 'This product uses the National Archives Catalog API but is not endorsed or certified by the National Archives and Records Administration.'
SERVICES = {
    'govinfo': {'env':'ALIEN_SEARCH_GOVINFO_KEY','pattern':r'[A-Za-z0-9]{40}','header':'X-Api-Key','host':'api.govinfo.gov',
                'how':'Get a free api.data.gov key from GovInfo, then run: python api.py set-key govinfo'},
    'nara': {'env':'ALIEN_SEARCH_NARA_KEY','pattern':r'[A-Za-z0-9_-]{8,128}','header':'x-api-key','host':'catalog.archives.gov',
             'how':'Request a Catalog API key by email from the National Archives (see docs/API.md), then run: python api.py set-key nara'},
}
PACKAGE_RE = re.compile(r'[A-Za-z0-9._-]{3,120}')


def _key_path(records,service):
    return records.runtime/(service+'-api-key.txt')


def set_key(records,service,value):
    spec=SERVICES[service]
    value=(value or '').strip()
    if not re.fullmatch(spec['pattern'],value):
        raise Problem('input','That does not look like a '+service+' API key; nothing was saved')
    path=_key_path(records,service)
    atomic_write(path,(value+'\n').encode('ascii'))
    return envelope(service=service,stored='runtime/'+path.name,note='Saved with owner-only permissions. The key is never printed. Delete that file to remove it.')


def load_key(records,service):
    spec=SERVICES[service]
    value=os.environ.get(spec['env'],'').strip()
    if not value:
        path=_key_path(records,service)
        if not path.exists(): return None
        fd=os.open(path,os.O_RDONLY|PRIVATE_OPEN)
        with os.fdopen(fd,'r',encoding='ascii',errors='replace') as f:
            st=os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_size>512: raise Problem('input','Invalid '+service+' key file')
            if os.name=='posix' and (st.st_uid!=os.getuid() or st.st_mode&0o077): raise Problem('input','The '+service+' key file must be owner-only')
            value=f.read().strip()
    if not re.fullmatch(spec['pattern'],value): raise Problem('input','The stored '+service+' key has an unexpected format')
    return value


def _require_key(records,service):
    key=load_key(records,service)
    if not key: raise Problem('auth','No '+service+' key is set up. This is optional. '+SERVICES[service]['how']+' (the key is read from standard input, not the command line).')
    return key


def _call(records,service,url,max_bytes):
    spec=SERVICES[service]; key=_require_key(records,service)
    records.pace(spec['host'])
    try:
        body,_,_=fetch(url,{spec['host']},max_bytes,extra_headers={spec['header']:key})
    except TransportError as e:
        if e.status in (401,403): raise Problem('auth','The '+service+' API rejected the key',"after_fix") from None
        raise records.transport_problem(e) from None
    try: return json.loads(body)
    except ValueError: raise Problem('drift','The '+service+' API did not return JSON') from None


def _strip_key(link):
    p=urllib.parse.urlsplit(link)
    query=urllib.parse.urlencode([(k,v) for k,v in urllib.parse.parse_qsl(p.query) if k.lower()!='api_key'])
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,query,''))


def _package(records,sid):
    pkg=records.source(sid).get('govinfo_package')
    if not pkg or not PACKAGE_RE.fullmatch(pkg): raise Problem('input','This source has no GovInfo package ID')
    return pkg


def govinfo_summary(records,sid):
    pkg=_package(records,sid)
    data=_call(records,'govinfo','https://api.govinfo.gov/packages/'+urllib.parse.quote(pkg)+'/summary',2*1024*1024)
    downloads=data.get('download') if isinstance(data,dict) else None
    links={k:_strip_key(v) for k,v in (downloads or {}).items() if isinstance(v,str) and v.startswith('https://')}
    return envelope(source_id=sid,package_id=pkg,title=data.get('title'),date_issued=data.get('dateIssued'),
                    collection=data.get('collectionCode'),download_links=links,evidence_status='candidate',
                    note='Official Government Publishing Office copy located through the GovInfo API. Save the PDF with: download '+sid+' --via govinfo')


def govinfo_download_target(records,sid):
    pkg=_package(records,sid); key=_require_key(records,'govinfo')
    return ('https://api.govinfo.gov/packages/'+urllib.parse.quote(pkg)+'/pdf',
            {'api.govinfo.gov','www.govinfo.gov','govinfo.gov'},{'X-Api-Key':key})


def _nara_budget(records):
    limit=int(os.environ.get('ALIEN_SEARCH_NARA_MONTHLY_LIMIT','10000'))
    month=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')
    path=records.runtime/'nara-usage.json'
    usage=load_json(path,{},expect=dict)
    used=usage.get(month,0) if isinstance(usage.get(month,0),int) else 0
    if used>=limit:
        raise Problem('rate','This installation has used its %d National Archives queries for %s. The count resets next month.'%(limit,month),'after_wait')
    write_json(path,{month:used+1})  # counted per data folder; see docs/API.md about running several copies at once
    return used+1,limit


def _hits(data):
    node=data.get('body',data) if isinstance(data,dict) else {}
    node=node.get('hits',{}) if isinstance(node,dict) else {}
    node=node.get('hits',[]) if isinstance(node,dict) else node
    return node if isinstance(node,list) else []


def nara_search(records,query,limit=20):
    if not isinstance(query,str) or not query.strip() or len(query)>200: raise Problem('input','Use a search of 1..200 characters')
    if not 1<=limit<=100: raise Problem('input','limit must be 1..100')
    _require_key(records,'nara')
    used,monthly=_nara_budget(records)
    data=_call(records,'nara','https://catalog.archives.gov/api/v2/records/search?'+urllib.parse.urlencode({'q':query.strip()}),5*1024*1024)
    rows=[]
    for hit in _hits(data)[:limit]:
        rec=((hit.get('_source') or {}).get('record') or hit) if isinstance(hit,dict) else {}
        na=rec.get('naId')
        rows.append({'naId':na,'title':rec.get('title'),'level':rec.get('levelOfDescription'),
                     'catalog_url':'https://catalog.archives.gov/id/'+str(na) if str(na).isdigit() else None})
    result=envelope(query=query.strip(),data=rows,returned=len(rows),notice=NARA_NOTICE,queries_used_this_month=used,monthly_limit=monthly,
                    evidence_status='candidate',note='Catalog descriptions are finding aids, not findings. If the API changes its response format, some fields may be missing.')
    if not rows and isinstance(data,dict): result['response_keys']=sorted(data)[:20]
    return result
