"""Bounded HTTPS with an exact host allowlist and DNS-pinned public sockets.
No cookies, browser sessions, environment proxies, ambient credentials, or telemetry.
"""
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import urllib.parse

from storage import atomic_write, load_json

PRODUCT = 'AlienSearch/3.3'
ACCEPT = 'application/pdf,text/html,application/json,text/plain,application/zip,*/*;q=0.1'
REDIRECTS = (301, 302, 303, 307, 308)
PRIVATE_OPEN = getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)  # never follow links; never let Windows translate line endings in files written byte for byte


class TransportError(Exception):
    def __init__(self,message,kind='network',status=None,retry_after=None,retry_later=False,host=None):
        super().__init__(message)
        self.kind,self.status,self.retry_after,self.retry_later=kind,status,retry_after,retry_later
        self.host=host  # the host that answered (after redirects), so a rate limit can pause that whole site


def user_agent():
    """Identify the tool honestly. An info link is opt-in because it can identify whoever publishes it."""
    info=os.environ.get('ALIEN_SEARCH_INFO_URL','').strip()
    if not info:
        return PRODUCT+' (unofficial public-records research)'
    p=urllib.parse.urlsplit(info)
    if (p.scheme!='https' or not p.hostname or p.username or p.password or len(info)>200
            or any(not 32<ord(c)<127 for c in info)):
        raise TransportError('ALIEN_SEARCH_INFO_URL must be a plain https URL without credentials',kind='input')
    return PRODUCT+' (+'+info+'; unofficial public-records research)'


def target(url,hosts):
    try:
        p=urllib.parse.urlsplit(url)
        if (p.scheme!='https' or p.hostname not in hosts or p.username or p.password
            or p.port not in (None,443) or any(ord(c)<33 for c in url)):
            raise ValueError()
    except (ValueError,TypeError):
        raise TransportError('Destination rejected',kind='input') from None
    return p


def public_addresses(host):
    try: rows=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    except OSError: raise TransportError('Destination DNS lookup failed') from None
    if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
        raise TransportError('Non-public destination address refused',kind='input')
    return rows


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        rows=public_addresses(self.host)
        # Use the validated sockaddr directly; never resolve hostname again.
        last=None
        for family,kind,proto,_,address in rows:
            sock=socket.socket(family,kind,proto)
            sock.settimeout(self.timeout)
            try:
                sock.connect(address)
                self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
                return
            except (OSError,ssl.SSLError) as error:
                last=error; sock.close()
        raise TransportError('HTTPS connection failed') from last


def _request(url,hosts,timeout,extra_headers=None,range_from=None,if_range=None,on_redirect=None):
    """Open a GET, following at most five allowlisted redirects.

    extra_headers (an API key, for example) are sent only to the host of the first URL and are
    dropped if a redirect moves to any other host. on_redirect(url) runs before each redirect is
    followed, so the caller can apply its site rules (robots.txt, pacing) to every destination,
    not just the first. if_range makes a resumed request conditional on the file being unchanged.
    Returns (connection, response, final_url).
    """
    origin=target(url,hosts).hostname
    for hop in range(6):
        p=target(url,hosts)
        connection=PinnedHTTPS(p.hostname,timeout=timeout,context=ssl.create_default_context())
        try:
            path=urllib.parse.urlunsplit(('', '',p.path or '/',p.query,''))
            headers={'User-Agent':user_agent(),'Accept':ACCEPT,'Accept-Encoding':'identity'}
            if extra_headers and p.hostname==origin: headers.update(extra_headers)
            if range_from:
                headers['Range']='bytes=%d-'%range_from
                if if_range: headers['If-Range']=if_range
            connection.request('GET',path,headers=headers)
            response=connection.getresponse()
            if response.status in REDIRECTS:
                location=response.getheader('Location')
                if not location: raise TransportError('Redirect lacks destination',kind='drift')
                url=urllib.parse.urljoin(url,location); target(url,hosts)
                connection.close()
                if on_redirect: on_redirect(url)
                continue
            if response.status not in (200,206) or (response.status==206 and not range_from):
                raise TransportError('Public source returned HTTP '+str(response.status),kind='error',status=response.status,retry_after=response.getheader('Retry-After'),host=p.hostname)
            if (response.getheader('Content-Encoding','identity') or 'identity').lower()!='identity':
                raise TransportError('Unexpected encoded response',kind='drift')
            return connection,response,url
        except TransportError:
            connection.close(); raise
        except (OSError,ValueError,http.client.HTTPException):
            connection.close(); raise TransportError('Public source transport failed') from None
        except BaseException:
            connection.close(); raise
    raise TransportError('Too many redirects',kind='blocked')


def fetch(url,hosts,max_bytes,timeout=30,extra_headers=None,on_redirect=None):
    """Return (body, final_url, headers) for a complete response no larger than max_bytes.

    A body shorter than its declared Content-Length is an error, not a smaller document.
    """
    connection,response,final=_request(url,hosts,timeout,extra_headers,on_redirect=on_redirect)
    try:
        headers=dict(response.getheaders())
        size=response.getheader('Content-Length')
        if size and (not size.isdigit() or int(size)>max_bytes):
            raise TransportError('Response exceeds configured size or has invalid length',kind='limit')
        body=response.read(max_bytes+1)
        if len(body)>max_bytes: raise TransportError('Response exceeds configured size',kind='limit')
        if size and len(body)!=int(size):
            raise TransportError('Response ended early (%d of %s bytes); try again'%(len(body),size))
        return body,final,headers
    except TransportError: raise
    except (OSError,ValueError,http.client.HTTPException):
        raise TransportError('Public source transport failed') from None
    finally: connection.close()


def probe(url,hosts,timeout=30,extra_headers=None,on_redirect=None):
    """Read only the response headers (size, type) and close without downloading the body."""
    connection,response,final=_request(url,hosts,timeout,extra_headers,on_redirect=on_redirect)
    try:
        length=response.getheader('Content-Length')
        return {'final_url':final,'status':response.status,
                'content_type':(response.getheader('Content-Type') or '').split(';')[0].strip() or None,
                'content_length':int(length) if length and length.isdigit() else None,
                'accept_ranges':(response.getheader('Accept-Ranges') or '').lower()=='bytes',
                'last_modified':response.getheader('Last-Modified')}
    finally: connection.close()


CONTENT_RANGE=re.compile(r'bytes (\d+)-(\d+)/(\d+|\*)$')


def _validator(headers):
    """The value to send in If-Range: a strong ETag, or else Last-Modified. Weak ETags cannot be used."""
    etag=(headers.get('etag') or '').strip()
    if etag and not etag.startswith('W/'): return etag
    return (headers.get('last-modified') or '').strip() or None


def download(url,hosts,part_path,max_bytes,timeout=60,extra_headers=None,chunk=1<<16,on_redirect=None):
    """Stream a response into part_path, resuming an earlier partial file only if it is provably the same file.

    A small companion file (PART.json) remembers the URL and the server's validator (strong ETag
    or Last-Modified). A resume is sent with If-Range, so a server whose file has changed sends the
    whole new file instead of the rest of the old one. Without a validator the download restarts.
    A 206 answer must start where the partial file ends and declare the total size, and the
    finished file must reach that total. The SHA-256 covers the whole file. The caller moves the
    finished file into place; an interrupted download leaves PART and PART.json for the next try.
    """
    part=pathlib.Path(part_path); meta=part.with_name(part.name+'.json')
    def forget():
        part.unlink(missing_ok=True); meta.unlink(missing_ok=True)
    saved=load_json(meta,{},expect=dict)
    start=part.stat().st_size if part.is_file() and not part.is_symlink() else 0
    validator=saved.get('validator') if saved.get('url')==url else None
    if start and not validator: start=0  # cannot prove the partial file matches the server's current file
    connection,response,final=_request(url,hosts,timeout,extra_headers,range_from=start or None,if_range=validator,on_redirect=on_redirect)
    try:
        headers={k.lower():v for k,v in response.getheaders()}
        total=None
        resumed=bool(start and response.status==206)
        if resumed:
            span=CONTENT_RANGE.match(headers.get('content-range','').strip())
            if not span or int(span[1])!=start or span[3]=='*':
                forget()
                raise TransportError('Server resumed from an unexpected position; the partial file was discarded. Run the command again.',kind='drift',retry_later=True)
            total=int(span[3])
            current=_validator(headers)
            if current and validator and current!=validator:
                forget()
                raise TransportError('The file changed on the server; the partial file was discarded. Run the command again.',kind='drift',retry_later=True)
        else:
            start=0
            length=headers.get('content-length')
            total=int(length) if length and length.isdigit() else None
            new=_validator(headers)
            if new: atomic_write(meta,json.dumps({'url':url,'validator':new}).encode('utf-8'))
            else: meta.unlink(missing_ok=True)
        if total is not None and total>max_bytes:
            raise TransportError('File is larger than the allowed download size',kind='limit')
        digest=hashlib.sha256()
        if resumed:
            with open(part,'rb') as existing:
                for block in iter(lambda: existing.read(chunk),b''): digest.update(block)
            fd=os.open(part,os.O_WRONLY|os.O_APPEND|PRIVATE_OPEN)
        else:
            fd=os.open(part,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|PRIVATE_OPEN,0o600)
        written=start
        with os.fdopen(fd,'wb') as out:  # O_APPEND (resume) or O_TRUNC (fresh) on the descriptor decides placement
            while True:
                block=response.read(chunk)
                if not block: break
                written+=len(block)
                if written>max_bytes:
                    out.close(); forget()
                    raise TransportError('File is larger than the allowed download size',kind='limit')
                out.write(block); digest.update(block)
        if total is not None and written!=total:
            raise TransportError('Download incomplete (%d of %d bytes); run the same command again to resume'%(written,total),kind='network')
        meta.unlink(missing_ok=True)
        return {'final_url':final,'bytes':written,'sha256':digest.hexdigest(),'resumed':resumed,
                'content_type':(headers.get('content-type') or '').split(';')[0].strip() or None,
                'last_modified':headers.get('last-modified')}
    except TransportError: raise
    except (OSError,ValueError,http.client.HTTPException):
        raise TransportError('Download interrupted; run the same command again to resume') from None
    finally: connection.close()
