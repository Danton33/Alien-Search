"""Read-only Wayback Machine client used by api.py. Python 3.10+, standard library only."""
from secure_http import fetch, TransportError
from common import decode_text, TEXT_TYPES
import datetime as dt
import ipaddress
import json
import re
import time
import urllib.parse
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

VERSION = "1.1.0"
FIELDS = ["timestamp", "original", "mimetype", "statuscode", "digest", "length"]


class APIError(Exception):
    def __init__(self, category, message, retry="no", retry_after=None):
        super().__init__(message)
        self.category, self.retry, self.retry_after = category, retry, retry_after


def validate_url(value):
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise APIError("input", "A public HTTP(S) URL is required.")
    value = value if "://" in value else "https://" + value
    p = urllib.parse.urlsplit(value)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password or "*" in value:
        raise APIError("input", "Use a public HTTP(S) URL without credentials or wildcards; use match_type for scope.")
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise APIError("input", "Private/local target URLs are not supported.")
    try:
        if not ipaddress.ip_address(host).is_global:
            raise APIError("input", "Private/local IP targets are not supported.")
    except ValueError:
        pass
    try:
        if p.port not in (None, 80, 443):
            raise APIError("input", "Only default HTTP(S) ports are supported.")
    except ValueError as exc:
        raise APIError("input", "Invalid port.") from exc
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def date_bound(value, upper=False):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) not in (4, 6, 8, 10, 12, 14) or not value.isdigit():
        raise APIError("input", "Use YYYY[MM[DD[hh[mm[ss]]]]] timestamps.")
    parts = [int(value[:4]), 1, 1, 0, 0, 0]
    for i, start in enumerate((4, 6, 8, 10, 12), 1):
        if len(value) > start:
            parts[i] = int(value[start:start + 2])
    try:
        low = dt.datetime(*parts, tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise APIError("input", str(exc)) from exc
    if not upper:
        return low.strftime("%Y%m%d%H%M%S")
    if len(value) == 4:
        high = low.replace(year=low.year + 1)
    elif len(value) == 6:
        high = low.replace(year=low.year + 1, month=1) if low.month == 12 else low.replace(month=low.month + 1)
    else:
        high = low + {8: dt.timedelta(days=1), 10: dt.timedelta(hours=1), 12: dt.timedelta(minutes=1), 14: dt.timedelta(seconds=1)}[len(value)]
    return (high - dt.timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")


def replay_url(original, timestamp, raw=False):
    original = validate_url(original)
    if len(timestamp) != 14:
        raise APIError("input", "Replay requires an exact 14-digit capture timestamp.")
    date_bound(timestamp)
    # A fragment in the original cannot be sent to the replay endpoint.
    return "https://web.archive.org/web/" + timestamp + ("id_" if raw else "") + "/" + original


def capture_identity(original, timestamp, headers, final):
    """Confirm a replay is the requested capture of the requested document; return its Memento-Datetime.

    Checks the exact timestamp in the final replay address, that the archived original is the same
    document (scheme, host, path and query), and that Memento-Datetime names the same second.
    """
    match = re.match(r"^https://web\.archive\.org/web/(\d{14})(?:[a-z_]+)?/(https?://.+)$", final)
    if not match or match[1] != timestamp:
        raise APIError("drift", "Replay does not match the requested exact timestamp")
    def identity(u):
        p = urllib.parse.urlsplit(u)
        return p.scheme.lower(), p.hostname, urllib.parse.unquote(p.path or "/"), p.query
    if identity(match[2]) != identity(original):
        raise APIError("drift", "Replay redirected to a different original document")
    memento = next((v for k, v in headers.items() if k.lower() == "memento-datetime"), None)
    try:
        actual = parsedate_to_datetime(memento).astimezone(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    except (ValueError, TypeError, AttributeError):
        raise APIError("drift", "Missing or invalid Memento-Datetime") from None
    if actual != timestamp:
        raise APIError("drift", "Memento-Datetime differs from the requested capture")
    return memento


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.in_title = False
        self.title, self.text, self.links = [], [], []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag == "title":
            self.in_title = True
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1
        if tag == "title":
            self.in_title = False

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.text.append(data.strip())
            if self.in_title:
                self.title.append(data.strip())


class WaybackClient:
    def __init__(self, timeout=25, min_interval=1.0, max_bytes=2_000_000):
        self.timeout, self.min_interval, self.max_bytes = timeout, min_interval, max_bytes
        self.last_request = 0.0

    def _get(self, url):
        """Return (body, headers, final_url).

        secure_http.fetch returns (body, final_url, headers); callers in this module and in
        api.py unpack (body, headers, final_url), so reorder here in one place.
        """
        time.sleep(max(0, self.min_interval - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        try:
            body, final_url, headers = fetch(url,{'archive.org','web.archive.org'},self.max_bytes,self.timeout)
            return body, headers, final_url
        except TransportError as exc:
            category={429:'rate',401:'auth',403:'blocked',404:'not_found'}.get(exc.status,exc.kind)
            raise APIError(category,str(exc),'after_wait' if exc.status==429 else 'no',exc.retry_after) from None

    def _json(self, url):
        body, _, _ = self._get(url)
        try:
            return json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise APIError("drift", "Expected JSON; server returned another format.") from exc

    @staticmethod
    def _envelope(operation, source, data, **extra):
        return {"ok": True, "class": "ok", "adapter": "wayback." + operation + ".v1", "version": VERSION,
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(), "source_url": source,
                "data": data, **extra}

    def closest(self, url, timestamp=None):
        url = validate_url(url)
        if timestamp:
            date_bound(timestamp)
        params = {"url": url}
        if timestamp:
            params["timestamp"] = timestamp
        source = "https://archive.org/wayback/available?" + urllib.parse.urlencode(params)
        payload = self._json(source)
        if not isinstance(payload, dict) or not isinstance(payload.get("archived_snapshots"), dict):
            raise APIError("drift", "Missing archived_snapshots object.")
        snap = payload["archived_snapshots"].get("closest")
        if snap:
            if not isinstance(snap, dict) or not all(k in snap for k in ("timestamp", "url", "status", "available")):
                raise APIError("drift", "Unexpected closest snapshot schema.")
            replay_url(url, snap["timestamp"])
            p = urllib.parse.urlsplit(snap["url"])
            if p.hostname != "web.archive.org":
                raise APIError("drift", "Unexpected snapshot host.")
            snap = dict(snap, url=urllib.parse.urlunsplit(("https", p.netloc, p.path, p.query, "")))
        return self._envelope("closest", source, snap, requested_url=url, requested_timestamp=timestamp,
                              source_as_of=snap["timestamp"] if snap else None, complete=True)

    def captures(self, url, start=None, end=None, match_type="exact", limit=100, resume_key=None,
                 status="200", mimetype="text/html"):
        url = validate_url(url)
        lo, hi = date_bound(start), date_bound(end, upper=True)
        if lo and hi and lo > hi:
            raise APIError("input", "start must not be after end.")
        if match_type not in ("exact", "prefix", "host", "domain"):
            raise APIError("input", "match_type must be exact, prefix, host or domain.")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 1000:
            raise APIError("input", "limit must be 1..1000.")
        if status is not None and not re.fullmatch(r"[1-5][0-9]{2}", status):
            raise APIError("input", "status must be a three-digit HTTP status or None.")
        args = [("url", url), ("output", "json"), ("fl", ",".join(FIELDS)), ("matchType", match_type),
                ("limit", str(limit)), ("showResumeKey", "true")]
        for k, v in (("from", start), ("to", end), ("resumeKey", resume_key)):
            if v:
                args.append((k, v))
        for k, v in (("statuscode", status), ("mimetype", mimetype)):
            if v is not None:
                args.append(("filter", k + ":" + re.escape(v)))
        source = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(args)
        payload = self._json(source)
        if not isinstance(payload, list):
            raise APIError("drift", "Expected CDX array.")
        records, next_key = [], None
        if payload:
            if payload[0] != FIELDS:
                raise APIError("drift", "Unexpected CDX header.")
            for row in payload[1:]:
                if row == []:
                    continue
                if isinstance(row, list) and len(row) == 1 and isinstance(row[0], str):
                    next_key = row[0]
                    continue
                if not isinstance(row, list) or len(row) != len(FIELDS):
                    raise APIError("drift", "Unexpected CDX row.")
                record = dict(zip(FIELDS, row))
                ts = record["timestamp"]
                replay = replay_url(record["original"], ts)
                if (lo and ts < lo) or (hi and ts > hi):
                    raise APIError("drift", "Capture outside requested date bounds.")
                if status is not None and record["statuscode"] != status:
                    raise APIError("drift", "Status filter was not respected.")
                if mimetype is not None and record["mimetype"] != mimetype:
                    raise APIError("drift", "MIME filter was not respected.")
                record["archive_url"] = replay
                records.append(record)
        if len(records) > limit:
            raise APIError("drift", "Server exceeded requested result limit.")
        # Do not claim exhaustion if a full page arrives without a resumption token.
        complete = next_key is None and len(records) < limit
        return self._envelope("captures", source, records, next_resume_key=next_key,
                              has_more=True if next_key else (False if complete else None), complete=complete,
                              source_as_of=None)

    def page_text(self, original_url, timestamp, max_chars=50000):
        if not isinstance(max_chars, int) or not 1 <= max_chars <= 500000:
            raise APIError("input", "max_chars must be 1..500000.")
        source = replay_url(original_url, timestamp, raw=True)
        body, headers, final_url = self._get(source)
        ctype = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
        plain = any(t in ctype.lower() for t in TEXT_TYPES)
        if not plain and not any(t in ctype.lower() for t in ("text/html", "application/xhtml+xml")):
            raise APIError("input", "Snapshot is not a supported text page.")
        memento = capture_identity(original_url, timestamp, headers, final_url)
        links = []
        if plain:
            # Plain text is kept literally; markers such as <REDACTED> must not vanish as if they were tags.
            title, text = "", decode_text(body, ctype)
        else:
            parser = PageText()
            parser.feed(decode_text(body, ctype, html=True))
            parser.close()
            title, text = " ".join(parser.title), "\n".join(parser.text)
            for href in parser.links:
                absolute = urllib.parse.urljoin(validate_url(original_url), href)
                if urllib.parse.urlsplit(absolute).scheme in ("http", "https") and absolute not in links:
                    links.append(absolute)
        truncated = len(text) > max_chars or len(links) > 500
        return self._envelope("page_text", source, {"title": title, "text": text[:max_chars],
                              "links": links[:500], "memento_datetime": memento, "final_url": final_url},
                              source_as_of=timestamp, complete=not truncated, truncated=truncated,
                              text_cut=len(text) > max_chars)


if __name__ == "__main__":
    import sys
    sys.stderr.write("wayback_api.py is a library used by api.py and has no command line of its own.\n"
                     "Use: python api.py captures ID | closest ID | snapshot ID TIMESTAMP | compare ID BEFORE AFTER\n")
    sys.exit(2)
