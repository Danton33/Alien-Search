"""Larger downloads, ZIP inspection, files you supply yourself, PDF text and link checks.

Standard library only (PyMuPDF is optional for PDF text). Nothing here bypasses a site's access
controls: blocked or robots-disallowed sites are reported, never worked around.
"""
import collections
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import secrets
import shutil
import urllib.parse
import zipfile

from common import VERSION, Problem, envelope, utc, text_status
from secure_http import TransportError, probe, download as stream_download
from storage import private_dir, write_json, load_json, store_object

SHA_RE = re.compile(r'[a-f0-9]{64}')
DEFAULT_DOWNLOAD_MB = 500
MAX_DOWNLOAD_MB = 20000
DEFAULT_EXTRACT_MB = 2000
SUSPICIOUS_RATIO = 100
FREE_SPACE_MARGIN = 200 * 1024 * 1024
PRIVATE_OPEN = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)  # never follow links; never let Windows translate line endings in files written byte for byte
MB = 1000 * 1000  # decimal megabytes, matching how sites and file managers usually report sizes


def size_text(n):
    """Readable decimal size: small files in kB, so a 242-byte file is not reported as 0.0 MB."""
    return '%.1f MB'%(n/MB) if n>=100*1000 else '%d kB'%max(1,round(n/1000)) if n>=1000 else '%d bytes'%n


# --- PDF text ---------------------------------------------------------------------------
def pdf_text(data, limit=20000):
    """Extract text with optional PyMuPDF. Locked PDFs are labelled, never cracked.

    Returns pages, text, text_complete, text_extraction and locked. Raises ValueError when the
    parser rejects a document that is not locked.
    """
    try:
        import fitz
    except ImportError:
        return {'pages':None,'text':'','text_complete':False,'locked':False,
                'text_extraction':'unavailable; optional PyMuPDF is not installed'}
    try:
        doc=fitz.open(stream=data,filetype='pdf')
    except Exception:
        raise ValueError('PDF parser rejected document') from None
    try:
        pages=len(doc)
        locked=bool(getattr(doc,'needs_pass',False))
        if locked and doc.authenticate(''):  # many "protected" PDFs open with an empty user password
            locked=False
        if locked:
            return {'pages':pages,'text':'','text_complete':False,'locked':True,
                    'text_extraction':'unavailable; the PDF is password-protected (only an empty password was tried)'}
        text=''; complete=True
        try:
            for i,page in enumerate(doc):
                text+='[PDF page %d] %s\n'%(i+1,page.get_text())
                if len(text)>limit: complete=False; break
        except Exception:
            if getattr(doc,'is_encrypted',False):
                return {'pages':pages,'text':'','text_complete':False,'locked':True,
                        'text_extraction':'unavailable; the PDF is encrypted and its text could not be read'}
            raise ValueError('PDF parser rejected document') from None
        return {'pages':pages,'text':text[:limit],'text_complete':complete,'locked':False,
                'text_extraction':'pymupdf; OCR not performed'}
    finally:
        doc.close()


# --- small private index files ---------------------------------------------------------------
def _append_index(path,entry):
    """Add one entry to a private index. A damaged index is set aside (NAME.corrupt-TIME), never silently dropped."""
    items=load_json(path,[],expect=list,keep_corrupt=True)
    items.append(entry); write_json(path,items,indent=1)


def _read_index(path):
    return load_json(path,[],expect=list)


def _store_object(records,src_path,digest):
    """Move a finished file into objects/<sha256> (a damaged stored copy is replaced); report it relative to the state folder."""
    dest=store_object(records.objects,digest,src=src_path)
    return dest,'objects/'+digest


def _head(path,n=5):
    with open(path,'rb') as f: return f.read(n)


# --- downloads ---------------------------------------------------------------------------------
def resolve(records,sid,link_id=None):
    s=records.source(sid)
    if not link_id: return s['url'],s
    result=records.read(sid,max_chars=0)
    if not result['ok']:
        raise Problem(result['class'],'The source page could not be read to find that link: '+str(result.get('reason','')),result.get('retry','no'))
    page=load_json(records.cache/(sid+'.json'),{},expect=dict)
    link=next((x for x in page.get('links',[]) if x['id']==link_id),None)
    if not link: raise Problem('input','Unknown link ID; list the page links with read first')
    return link['url'],s


def download(records,sid,link_id=None,confirm=False,max_mb=DEFAULT_DOWNLOAD_MB,via=None):
    """Report a file's size; download it only after the user confirms. Resumes interrupted downloads."""
    if not isinstance(max_mb,int) or not 1<=max_mb<=MAX_DOWNLOAD_MB:
        raise Problem('input','max-mb must be 1..%d'%MAX_DOWNLOAD_MB)
    extra=None
    if via=='govinfo':
        import official_apis
        url,hosts,extra=official_apis.govinfo_download_target(records,sid)
        records.source(sid)
        follow=lambda u: records.pace(urllib.parse.urlsplit(u).hostname)  # API route: pacing, under the API's own terms
    elif via is None:
        url,_=resolve(records,sid,link_id); hosts=records.hosts
        records.validate_url(url); records.check_robots(url)
        follow=records.follow  # robots.txt and pacing for every redirect destination too
    else:
        raise Problem('input','via must be govinfo or omitted')
    host=urllib.parse.urlsplit(url).hostname
    records.pace(host)
    try: info=probe(url,hosts,extra_headers=extra,on_redirect=follow)
    except TransportError as e: raise records.transport_problem(e) from None
    size=info['content_length']; limit=max_mb*MB
    summary=dict(source_id=sid,link_id=link_id,via=via,url=url,final_url=info['final_url'],content_type=info['content_type'],
                 size_bytes=size,size_mb=round(size/MB,1) if size is not None else None,max_mb=max_mb)
    if size is not None and size>limit:
        need=-(-size//MB)
        return envelope('limit',**summary,retry='after_fix',
                        reason='The file is %.1f MB, above the %d MB limit.'%(size/MB,max_mb),
                        next_steps=['Tell the user the size. Only if they want it: python api.py download %s%s --max-mb %d --confirm'%(sid,' '+link_id if link_id else '',need)])
    if not confirm:
        told=('The server reports %s.'%size_text(size) if size is not None else 'The server did not report a size; the --max-mb limit still applies.')
        return envelope(**summary,confirm_required=True,evidence_status='candidate',
                        note='Nothing was downloaded. '+told,
                        next_steps=['Ask the user whether to download it (%s).'%told.rstrip('.'),
                                    'Only if they agree: python api.py download %s%s --confirm'%(sid,' '+link_id if link_id else '')])
    free=shutil.disk_usage(records.objects).free
    if size is not None and free<size+FREE_SPACE_MARGIN:
        raise Problem('limit','Not enough free disk space for this download')
    parts=private_dir(records.state/'downloads')
    part=parts/(hashlib.sha256(url.encode()).hexdigest()[:32]+'.part')
    records.pace(host)
    try: got=stream_download(url,hosts,part,limit,extra_headers=extra,on_redirect=follow)
    except TransportError as e: raise records.transport_problem(e) from None
    dest,stored=_store_object(records,part,got['sha256'])
    head=_head(dest)
    entry={'source_id':sid,'link_id':link_id,'via':via,'url':url,'final_url':got['final_url'],'sha256':got['sha256'],
           'bytes':got['bytes'],'content_type':got['content_type'],'last_modified':got['last_modified'],'fetched_at':utc(),
           'copy_kind':'official-api-download' if via else 'official-download'}
    _append_index(parts/'index.json',entry)
    return envelope(**entry,resumed=got['resumed'],stored_as=stored,is_pdf=head.startswith(b'%PDF'),is_zip=zipfile.is_zipfile(dest),
                    evidence_status='candidate')


# --- ZIP archives ----------------------------------------------------------------------------------
def _object(records,sha):
    if not isinstance(sha,str) or not SHA_RE.fullmatch(sha):
        raise Problem('input','Use the 64-character SHA-256 shown by download or import-file')
    path=records.objects/sha
    if path.is_symlink() or not path.is_file(): raise Problem('input','No stored file with that SHA-256')
    return path


def _member_problem(info):
    name=info.filename
    parts=pathlib.PurePosixPath(name.replace('\\','/')).parts
    if name.startswith(('/','\\')) or re.match(r'^[A-Za-z]:',name) or '..' in parts or not parts:
        return 'unsafe path'
    if (info.external_attr>>16)&0o170000==0o120000: return 'symbolic link'
    if info.flag_bits&0x1: return 'encrypted'
    if info.file_size>10*MB and info.file_size/max(1,info.compress_size)>SUSPICIOUS_RATIO:
        return 'suspicious compression ratio'
    return None


def _kind(name):
    lower=name.lower()
    return 'pdf' if lower.endswith('.pdf') else 'media' if lower.endswith(('.mp4','.mov','.m4v','.webm','.mp3','.wav')) else 'image' if lower.endswith(('.jpg','.jpeg','.png','.tif','.tiff','.gif')) else 'other'


def zip_list(records,sha,limit=500):
    path=_object(records,sha)
    if not 1<=limit<=5000: raise Problem('input','limit must be 1..5000')
    if not zipfile.is_zipfile(path): raise Problem('input','That stored file is not a ZIP archive')
    try:
        with zipfile.ZipFile(path) as z: infos=[i for i in z.infolist() if not i.is_dir()]
    except zipfile.BadZipFile: raise Problem('drift','The ZIP archive is damaged') from None
    rows=[{'name':i.filename,'size_bytes':i.file_size,'compressed_bytes':i.compress_size,'kind':_kind(i.filename),'problem':_member_problem(i)} for i in infos]
    return envelope(sha256=sha,members=rows[:limit],member_count=len(rows),returned=min(limit,len(rows)),has_more=len(rows)>limit,
                    total_unpacked_bytes=sum(i.file_size for i in infos),flagged=sum(1 for r in rows if r['problem']),
                    next_steps=['Extract only what you need: python api.py zip-extract %s NAME [NAME ...] or --pdfs'%sha])


def zip_extract(records,sha,members=(),pdfs=False,max_mb=DEFAULT_EXTRACT_MB):
    path=_object(records,sha)
    if not isinstance(max_mb,int) or not 1<=max_mb<=MAX_DOWNLOAD_MB: raise Problem('input','max-mb must be 1..%d'%MAX_DOWNLOAD_MB)
    budget=max_mb*MB
    try: z=zipfile.ZipFile(path)
    except zipfile.BadZipFile: raise Problem('drift','The ZIP archive is damaged') from None
    with z:
        infos={i.filename:i for i in z.infolist() if not i.is_dir()}
        chosen=list(dict.fromkeys(list(members)+([n for n in infos if n.lower().endswith('.pdf')] if pdfs else [])))
        if not chosen: raise Problem('input','Name the files to extract (see zip-list), or use --pdfs')
        unknown=[n for n in chosen if n not in infos]
        if unknown: raise Problem('input','Not in this archive: '+', '.join(unknown[:5]))
        declared=sum(infos[n].file_size for n in chosen if not _member_problem(infos[n]))
        if declared>budget:
            return envelope('limit',sha256=sha,reason='Those files unpack to %.1f MB, above your %d MB limit. Choose fewer files or raise --max-mb.'%(declared/MB,max_mb),retry='after_fix')
        extracted=private_dir(records.state/'extracted')
        out_root=private_dir(extracted/sha)
        if shutil.disk_usage(out_root).free<declared+FREE_SPACE_MARGIN: raise Problem('limit','Not enough free disk space to extract these files')
        results=[]; used=0
        for name in chosen:
            info=infos[name]; problem=_member_problem(info)
            if problem:
                results.append({'name':name,'extracted':False,'problem':problem}); continue
            rel=pathlib.PurePosixPath(name.replace('\\','/'))
            dest=out_root.joinpath(*rel.parts)
            dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            if not dest.parent.resolve().is_relative_to(out_root.resolve()) or dest.is_symlink():
                results.append({'name':name,'extracted':False,'problem':'unsafe path'}); continue
            digest=hashlib.sha256(); written=0
            try:
                fd=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|PRIVATE_OPEN,0o600)
                with z.open(info) as src, os.fdopen(fd,'wb') as out:
                    for block in iter(lambda: src.read(1<<16),b''):
                        written+=len(block); used+=len(block)
                        if used>budget: raise OverflowError
                        out.write(block); digest.update(block)
            except OverflowError:
                dest.unlink(missing_ok=True)
                raise Problem('limit','Extraction stopped: the archive held more data than it declared') from None
            except (zipfile.BadZipFile,RuntimeError,OSError):
                dest.unlink(missing_ok=True)
                results.append({'name':name,'extracted':False,'problem':'damaged or unreadable'}); continue
            results.append({'name':name,'extracted':True,'stored_as':'extracted/%s/%s'%(sha,rel.as_posix()),'bytes':written,'sha256':digest.hexdigest()})
    parent=_parent(records,sha)
    # The manifest sits beside the folder, so an archive member named manifest.json cannot collide with it.
    _append_index(extracted/(sha+'.json'),{'extracted_at':utc(),'from_sha256':sha,'parent':parent,'files':results})
    return envelope(sha256=sha,parent=parent,files=results,extracted=sum(1 for r in results if r['extracted']),skipped=sum(1 for r in results if not r['extracted']),
                    evidence_status='candidate')


def _parent(records,sha):
    """Where a ZIP came from, so the files extracted from it keep that provenance."""
    for entry in _read_index(records.state/'downloads'/'index.json'):
        if entry.get('sha256')==sha:
            return {'source_id':entry.get('source_id'),'copy_kind':entry.get('copy_kind','official-download'),'url':entry.get('final_url')}
    for entry in _read_index(records.state/'imports'/'index.json'):
        if entry.get('sha256')==sha:
            return {'source_id':entry.get('source_id'),'copy_kind':'user-supplied','evidence_status':entry.get('evidence_status')}
    return {'copy_kind':'unknown'}


# --- files the user supplies -------------------------------------------------------------------------
def _known_hashes(records):
    """Hashes this installation itself obtained: live reads, downloads, archived captures and files
    extracted from those downloads. Each match names the source it came from."""
    known=collections.defaultdict(list)
    for sid in records.byid:
        page=load_json(records.cache/(sid+'.json'),{},expect=dict)
        if page.get('ok') and page.get('sha256') and page.get('source_id'):
            known[page['sha256']].append({'kind':'official-fetch','source_id':page['source_id'],'url':page.get('final_url') or page.get('source_url'),'fetched_at':page.get('fetched_at')})
    for entry in _read_index(records.state/'downloads'/'index.json'):
        if entry.get('sha256'):
            known[entry['sha256']].append({'kind':entry.get('copy_kind','official-download'),'source_id':entry.get('source_id'),'url':entry.get('final_url'),'fetched_at':entry.get('fetched_at')})
    for snap in records.cache.glob('snapshot-*.json'):
        data=load_json(snap,{},expect=dict)
        if data.get('sha256') and data.get('source_id'):
            known[data['sha256']].append({'kind':'archived-capture','source_id':data['source_id'],'capture_timestamp':data.get('capture_timestamp'),'replay_url':data.get('replay_url')})
    extracted=records.state/'extracted'
    for manifest in list(extracted.glob('*.json'))+list(extracted.glob('*/manifest.json')):  # second form: 3.2 and earlier
        for run in load_json(manifest,[],expect=list):
            parents=known.get(run.get('from_sha256')) if isinstance(run,dict) else None
            if not parents: continue  # only ZIPs this installation obtained itself pass on their provenance
            for f in run.get('files',[]):
                if isinstance(f,dict) and f.get('extracted') and f.get('sha256'):
                    for parent in parents:
                        known[f['sha256']].append({'kind':'extracted-from-'+parent['kind'],'source_id':parent.get('source_id'),'from_zip_sha256':run['from_sha256'],'member':f.get('name')})
    return known


SECRET_WORDS={'token','accesstoken','authtoken','apitoken','key','apikey','sig','signature','auth','authorization','password','passwd',
              'pwd','secret','session','sessionid','sid','credential','credentials','code','jwt','otp'}


def _secret_param(name):
    return any(part in SECRET_WORDS for part in re.split(r'[-_.\[\]]+',name.lower()))


def _redact_url(url):
    """Hide values of query parameters that look like credentials (access_token, X-Amz-Signature, key ...) before a URL is stored or shown."""
    p=urllib.parse.urlsplit(url)
    if not p.query: return url
    pairs=urllib.parse.parse_qsl(p.query,keep_blank_values=True)
    if not any(_secret_param(k) for k,_ in pairs): return url
    query=urllib.parse.urlencode([(k,'REDACTED' if _secret_param(k) else v) for k,v in pairs])
    return urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,query,''))


def _clean_from_url(url):
    if url is None: return None
    p=urllib.parse.urlsplit(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password or len(url)>2000 or any(ord(c)<33 for c in url):
        raise Problem('input','--from-url must be a plain http(s) URL without credentials')
    return _redact_url(url)


def import_file(records,sid,path,from_url=None,verify_archive=False,max_chars=2000):
    """Copy a file the user obtained themselves into private storage, labelled user-supplied.

    It is only reported as matching the official record when its SHA-256 equals bytes this
    installation fetched from the official site or from an archived capture.
    """
    s=records.source(sid)
    from_url=_clean_from_url(from_url)
    if not 0<=max_chars<=20000: raise Problem('input','max-chars must be 0..20000')
    src=pathlib.Path(path).expanduser()
    if not src.is_file(): raise Problem('input','File not found')
    if src.stat().st_size>MAX_DOWNLOAD_MB*MB: raise Problem('limit','File is larger than %d MB'%MAX_DOWNLOAD_MB)
    tmp=records.objects/('import-'+secrets.token_hex(8)+'.tmp')
    digest=hashlib.sha256(); size=0
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|PRIVATE_OPEN,0o600)
    with open(src,'rb') as inp, os.fdopen(fd,'wb') as out:
        for block in iter(lambda: inp.read(1<<16),b''):
            size+=len(block); digest.update(block); out.write(block)
    sha=digest.hexdigest()
    dest,stored=_store_object(records,tmp,sha)
    head=_head(dest)
    links={x['url'] for x in load_json(records.cache/(sid+'.json'),{},expect=dict).get('links',[])}
    registered=from_url is not None and (from_url==s['url'] or from_url in links)
    archive_check=None
    if verify_archive:
        found=records.closest(sid)
        ts=(((found.get('archive_result') or {}).get('data') or {}).get('timestamp')) if found.get('ok') else None
        if ts:
            snap=records.snapshot(sid,ts,0)
            archive_check={'capture_timestamp':ts,'ok':snap['ok'],'sha256':snap.get('sha256'),'reason':snap.get('reason')}
        else:
            archive_check={'ok':False,'reason':found.get('reason') or 'No archived capture found'}
    matches=list(_known_hashes(records).get(sha,[]))
    status='user-supplied'
    if matches:
        status='user-supplied; bytes match '+' and '.join(sorted({_match_phrase(m) for m in matches}))
    text=None; pdf=None
    if head.startswith(b'%PDF') and size<=200*MB:
        try: pdf=pdf_text(dest.read_bytes(),limit=max(max_chars,1))
        except ValueError: pdf={'text':'','text_extraction':'PDF parser rejected document','pages':None,'locked':False}
        text=pdf['text'][:max_chars]
    entry={'source_id':sid,'sha256':sha,'bytes':size,'original_name':src.name,'from_url':from_url,'from_url_is_registered':registered,
           'imported_at':utc(),'evidence_status':status,'matches':matches}
    _append_index(private_dir(records.state/'imports')/'index.json',entry)
    return envelope(**entry,stored_as=stored,is_pdf=head.startswith(b'%PDF'),archive_check=archive_check,
                    pdf_pages=pdf['pages'] if pdf else None,text_extraction=pdf['text_extraction'] if pdf else None,
                    text_status=text_status('pdf',pdf) if pdf else None,text_excerpt=text)


def _match_phrase(match):
    """Plain words for one fingerprint match, naming the source it came from."""
    kind=match['kind']; where=' (%s)'%match['source_id'] if match.get('source_id') else ''
    if kind.startswith('extracted-from-'):
        origin='an archived capture' if 'archived' in kind else 'an official download'
        return 'a file extracted from '+origin+where
    return ('an archived capture' if kind=='archived-capture' else 'an official download')+where


# --- link checks ------------------------------------------------------------------------------------
def verify_sources(records,ids=None):
    """Politely check each registered link (headers only) and write a dated private report."""
    chosen=[records.source(i) for i in ids] if ids else [records.source(s['id']) for s in records.sources]
    rows=[]
    for s in chosen:
        row={'id':s['id'],'url':s['url']}
        try:
            records.validate_url(s['url']); records.check_robots(s['url'])
            records.pace(urllib.parse.urlsplit(s['url']).hostname)
            info=probe(s['url'],records.hosts,on_redirect=records.follow)
            row.update(ok=True,**{'class':'ok'},final_url=info['final_url'],content_type=info['content_type'],size_bytes=info['content_length'])
            if urllib.parse.urlsplit(s['url']).path.lower().endswith('.pdf') and info['content_type'] not in (None,'application/pdf','application/octet-stream'):
                row.update(ok=False,**{'class':'drift'},reason='A PDF link now answers with '+str(info['content_type']))
        except Problem as e:
            row.update(ok=False,**{'class':e.kind},reason=str(e))
        except TransportError as e:
            problem=records.transport_problem(e); row.update(ok=False,**{'class':problem.kind},reason=str(problem))
        rows.append(row)
    counts=dict(collections.Counter(r['class'] for r in rows))
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    reports=private_dir(records.state/'reports')
    write_json(reports/('verify-'+stamp+'.json'),{'checked_at':utc(),'tool_version':VERSION,'counts':counts,'results':rows},indent=1)
    return envelope(checked_at=utc(),counts=counts,results=rows,report='reports/verify-'+stamp+'.json')
