"""Shared result envelope, error type and text decoding for Alien Search modules."""
import codecs
import datetime as dt
import re

VERSION = '3.3.0'
UNOFFICIAL = 'Unofficial research tool; not affiliated with or endorsed by any government agency.'
# Labels encodings the way browsers do: pages declared as Latin-1 or ASCII are really Windows-1252.
BROWSER_ALIASES = {'iso-8859-1':'cp1252','iso8859-1':'cp1252','latin-1':'cp1252','latin1':'cp1252','us-ascii':'cp1252','ascii':'cp1252'}
TEXT_TYPES = ('text/plain','text/csv')


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Problem(Exception):
    def __init__(self, kind, message, retry='after_fix', retry_after=None, next_steps=None):
        super().__init__(message); self.kind=kind; self.retry=retry; self.retry_after=retry_after
        self.next_steps=list(next_steps or [])


def problem_envelope(e):
    """Error result with the hints an assistant or agent needs to continue."""
    extra={'next_steps':e.next_steps} if getattr(e,'next_steps',None) else {}
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

    none: the format is not read as text, the PDF is locked, or PyMuPDF is not installed.
    partial: extraction stopped at its limit, so text beyond it was not read.
    """
    if fmt=='pdf':
        if not pdf_info or not str(pdf_info.get('text_extraction','')).startswith('pymupdf'): return 'none'
        return 'complete' if pdf_info.get('text_complete') else 'partial'
    if fmt in ('html','text'): return 'partial' if partial else 'complete'
    return 'none'
