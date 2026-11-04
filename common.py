"""Shared result envelope, error type and text decoding for Alien Search modules."""
import codecs
import datetime as dt
import math
import re
import time
from email.utils import parsedate_to_datetime

VERSION = '3.3.1'
UNOFFICIAL = 'Unofficial research tool; not affiliated with or endorsed by any government agency.'
# Labels encodings the way browsers do: pages declared as Latin-1 or ASCII are really Windows-1252.
BROWSER_ALIASES = {'iso-8859-1':'cp1252','iso8859-1':'cp1252','latin-1':'cp1252','latin1':'cp1252','us-ascii':'cp1252','ascii':'cp1252'}
TEXT_TYPES = ('text/plain','text/csv')
MAX_RETRY_AFTER = 86400  # a server's Retry-After is honoured for at most 24 hours
DEFAULT_RETRY_AFTER = 3600  # used when a rate limit has no Retry-After, or one that cannot be parsed


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Problem(Exception):
    def __init__(self, kind, message, retry='after_fix', retry_after=None, next_steps=None, not_before=None):
        super().__init__(message); self.kind=kind; self.retry=retry; self.retry_after=retry_after
        self.next_steps=list(next_steps or [])
        self.not_before=not_before  # end of a known pause (epoch seconds), when the tool already worked it out


def retry_delay(value, now):
    """Seconds to wait for a Retry-After header (delta-seconds or HTTP-date), clamped to 1..MAX_RETRY_AFTER.

    A missing or unparseable value means DEFAULT_RETRY_AFTER (one hour)."""
    try: delay=int(value)
    except (ValueError,TypeError):
        try: delay=parsedate_to_datetime(value).timestamp()-now
        except (ValueError,TypeError,OverflowError): delay=DEFAULT_RETRY_AFTER
    return min(MAX_RETRY_AFTER,max(1,delay))


def iso_utc(seconds):
    return dt.datetime.fromtimestamp(seconds,dt.timezone.utc).replace(microsecond=0).isoformat()


def retry_fields(retry_after=None, not_before=None, now=None):
    """The same three retry fields on every result that asks the caller to wait.

    retry_after_seconds: whole seconds still to wait; retry_at: the UTC time it ends;
    retry_after: the server's own header value, kept as sent when there was one.
    Give either the raw retry_after value or an absolute not_before (seconds since the epoch).
    """
    now=time.time() if now is None else now
    if not_before is None: not_before=now+retry_delay(retry_after,now)
    out={'retry_after_seconds':max(1,math.ceil(not_before-now)),'retry_at':iso_utc(not_before)}
    if retry_after is not None: out['retry_after']=retry_after
    return out


def problem_envelope(e):
    """Error result with the hints an assistant or agent needs to continue, including how long to wait."""
    extra={'next_steps':e.next_steps} if getattr(e,'next_steps',None) else {}
    retry_after=getattr(e,'retry_after',None); not_before=getattr(e,'not_before',None)
    if retry_after is not None or not_before is not None or e.kind=='rate':
        extra.update(retry_fields(retry_after,not_before=not_before))
    return envelope(e.kind, reason=str(e), retry=e.retry, **extra)


def envelope(kind='ok', **values):
    return {'schema':'alien-search-api/1','version':VERSION,'ok':kind=='ok','class':kind,**values}


def _codec(name):
    if not name: return None
    name=BROWSER_ALIASES.get(name.strip().strip('"\'').lower(),name.strip().strip('"\''))
    try: return codecs.lookup(name).name
    except LookupError: return None


def decode_text(body, content_type='', html=False):
    """Decode a page or text file using its byte-order mark, declared charset or (for HTML) meta tag.

    Without a usable declaration, UTF-8 is tried strictly and Windows-1252 is the fallback, so
    accented text on older government pages is not turned into replacement characters.
    """
    for bom,name in ((b'\xef\xbb\xbf','utf-8-sig'),(b'\xff\xfe','utf-16'),(b'\xfe\xff','utf-16')):
        if body.startswith(bom): return body.decode(name,errors='replace')
    match=re.search(r'charset\s*=\s*["\']?([A-Za-z0-9._:-]+)',content_type or '',re.I)
    declared=_codec(match[1]) if match else None
    if not declared and html:
        head=body[:4096].decode('ascii',errors='ignore')
        meta=re.search(r'<meta[^>]+charset\s*=\s*["\']?([A-Za-z0-9._:-]+)',head,re.I)
        declared=_codec(meta[1]) if meta else None
    if declared:
        return body.decode(declared,errors='replace')
    try: return body.decode('utf-8')
    except UnicodeDecodeError: return body.decode('cp1252',errors='replace')


def text_status(fmt, pdf_info=None, partial=False):
    """How much of a document's text the tool extracted: complete, partial or none.

    none: the format is not read as text, the PDF is locked, PyMuPDF is not installed, or no page
    has a text layer (a scan). partial: extraction stopped at its limit, or some pages have no text
    layer, so part of the document was not read. Reaching the last page is not the same as reading it.
    """
    if fmt=='pdf':
        if not pdf_info or not str(pdf_info.get('text_extraction','')).startswith('pymupdf'): return 'none'
        if pdf_info.get('has_text') is False or not str(pdf_info.get('text','')).strip(): return 'none'
        return 'complete' if pdf_info.get('text_complete') else 'partial'
    if fmt in ('html','text'): return 'partial' if partial else 'complete'
    return 'none'
