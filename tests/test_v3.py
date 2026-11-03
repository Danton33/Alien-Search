"""Tests for Alien Search 3.0 behaviour. Network calls are faked at the secure_http boundary."""
import hashlib
import io
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import acquire
import api
import official_apis
import robots
import secure_http
import storage
from api import Records, make_server
from common import Problem
from secure_http import TransportError

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import audit_release  # noqa: E402


def fake_conn(status=200, headers=None, chunks=(), location=None):
    conn = MagicMock(); resp = conn.getresponse.return_value; h = dict(headers or {})
    if location: h['Location'] = location
    resp.status = status
    resp.getheader.side_effect = lambda k, d=None: h.get(k, d)
    resp.getheaders.return_value = list(h.items())
    data = list(chunks)
    resp.read.side_effect = lambda n=-1: data.pop(0) if data else b''
    return conn


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / 'state'
        self.r = Records(data_dir=self.state)

    def tearDown(self):
        self.tmp.cleanup()

    def quiet(self, *names):
        for name in names:
            p = patch.object(self.r, name); p.start(); self.addCleanup(p.stop)


class HostsAndLinks(Base):
    def test_www_and_bare_forms_are_both_allowed(self):
        for host in ('war.gov', 'www.war.gov', 'defense.gov', 'www.defense.gov', 'archives.gov', 'www.archives.gov', 'media.defense.gov'):
            self.assertIn(host, self.r.hosts)
        self.assertNotIn('evil.example', self.r.hosts)

    def test_pursue_file_links_on_bare_domain_are_kept_and_classified(self):
        page = (b'<html><title>PURSUE</title>'
                b'<a href="https://war.gov/medialink/ufo/release_05/x.zip">Release 05</a>'
                b'<a href="https://war.gov/medialink/ufo/clip.mp4">Video</a>'
                b'<a href="/UFO/release/03/">Release 03</a></html>')
        with patch.object(self.r, 'get_bytes', return_value=(page, 'https://www.war.gov/UFO/', {'Content-Type': 'text/html'})):
            out = self.r.read('A019')
        kinds = {l['url']: l['kind'] for l in out['data']}
        self.assertEqual(kinds['https://war.gov/medialink/ufo/release_05/x.zip'], 'archive')
        self.assertEqual(kinds['https://war.gov/medialink/ufo/clip.mp4'], 'media')
        self.assertEqual(kinds['https://www.war.gov/UFO/release/03/'], 'page')

    def test_nara_bulk_links_classified_by_type(self):
        page = (b'<html><title>Bulk</title>'
                b'<a href="https://catalog.archives.gov/medialz/bulk-downloads/uaps/JSON/catalog-export-1.json">JSON</a>'
                b'<a href="https://catalog.archives.gov/medialz/bulk-downloads/uaps/zips/still-pictures/2.zip">ZIP</a>'
                b'<a href="https://catalog.archives.gov/id/733667">Record</a></html>')
        with patch.object(self.r, 'get_bytes', return_value=(page, self.r.source('A035')['url'], {'Content-Type': 'text/html'})):
            out = self.r.read('A035', kind='archive')
        self.assertEqual([l['kind'] for l in out['data']], ['archive'])
        with patch.object(self.r, 'get_bytes', return_value=(page, self.r.source('A035')['url'], {'Content-Type': 'text/html'})):
            kinds = sorted(l['kind'] for l in self.r.read('A035', refresh=True)['data'])
        self.assertEqual(kinds, ['archive', 'catalog', 'metadata'])

    def test_register_search_covers_alternates(self):
        ids = [s['id'] for s in self.r.list(query='medialink')['data']]
        self.assertEqual(ids, ['A019'])
        self.assertEqual(len(self.r.sources), 35)


class Robots(Base):
    def test_cautious_reading_never_allows_what_a_disallow_matches(self):
        rules = ['User-agent: *', 'Disallow: /UFO/', 'Allow: /UFO/release/', 'Disallow: /*.zip$']
        self.assertFalse(robots.cautious_allowed(rules, 'https://h/UFO/release/x'))  # Allow ignored on purpose
        self.assertFalse(robots.cautious_allowed(rules, 'https://h/a/b.zip'))
        self.assertTrue(robots.cautious_allowed(rules, 'https://h/a/b.zip?x=1'))
        self.assertTrue(robots.cautious_allowed(rules, 'https://h/news/'))
        self.assertTrue(robots.cautious_allowed([], 'https://h/anything'))

    def test_specific_group_beats_star_group(self):
        rules = ['User-agent: *', 'Disallow: /', '', 'User-agent: AlienSearch', 'Disallow: /private/']
        self.assertTrue(robots.cautious_allowed(rules, 'https://h/public'))
        self.assertFalse(robots.cautious_allowed(rules, 'https://h/private/x'))

    @unittest.skipUnless(robots.STDLIB_OK, 'Standard-library parser lacks RFC 9309 matching on this Python')
    def test_rfc9309_longest_match_on_capable_python(self):
        rules = 'User-agent: *\nDisallow: /UFO/\nAllow: /UFO/release/\n'
        self.assertTrue(robots.allowed(rules, 'https://h/UFO/release/x'))
        self.assertFalse(robots.allowed(rules, 'https://h/UFO/other'))

    def test_check_robots_states(self):
        url = 'https://www.archives.gov/research/topics/uaps'
        with patch('api.fetch', return_value=(b'User-agent: *\nDisallow: /research/', url, {})) as fetch, patch.object(self.r, 'pace'):
            with self.assertRaises(Problem) as err:
                self.r.check_robots(url)
            self.assertEqual(err.exception.kind, 'robots')
            with self.assertRaises(Problem):
                self.r.check_robots(url)
            self.assertEqual(fetch.call_count, 1)  # cached per host
        for status, allowed in ((404, True), (503, False)):
            for f in self.r.cache.glob('robots-*'): f.unlink()
            with patch('api.fetch', side_effect=TransportError('x', kind='error', status=status)), patch.object(self.r, 'pace'):
                if allowed:
                    self.r.check_robots(url)
                else:
                    with self.assertRaises(Problem): self.r.check_robots(url)

    def test_archive_hosts_skip_robots(self):
        with patch('api.fetch') as fetch:
            self.r.check_robots('https://web.archive.org/web/2020/x')
        fetch.assert_not_called()

    def test_disallowed_source_is_reported_not_fetched(self):
        url = self.r.source('A001')['url']
        calls = []
        def fake(u, hosts, max_bytes, timeout=30, extra_headers=None):
            calls.append(u)
            return (b'User-agent: *\nDisallow: /\n', u, {})
        with patch('api.fetch', side_effect=fake), patch.object(self.r, 'pace'), patch.object(self.r, 'validate_url'):
            out = self.r.read('A001')
        self.assertEqual(out['class'], 'robots')
        self.assertEqual(calls, ['https://www.archives.gov/robots.txt'])


class Pacing(Base):
    def test_government_and_archive_intervals(self):
        with patch('time.sleep') as sleep:
            self.r.pace('www.archives.gov'); self.r.pace('www.archives.gov')
            self.r.pace('web.archive.org'); self.r.pace('web.archive.org')
        waits = [round(c.args[0]) for c in sleep.call_args_list]
        self.assertEqual(waits, [2, 5])


class Transport(unittest.TestCase):
    def test_user_agent_is_honest_and_link_is_opt_in(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('ALIEN_SEARCH_INFO_URL', None)
            self.assertEqual(secure_http.user_agent(), secure_http.PRODUCT + ' (unofficial public-records research)')
        with patch.dict(os.environ, {'ALIEN_SEARCH_INFO_URL': 'https://example.org/about'}):
            self.assertIn('(+https://example.org/about;', secure_http.user_agent())
        for bad in ('http://example.org', 'https://u:p@example.org', 'https://example.org/a b'):
            with patch.dict(os.environ, {'ALIEN_SEARCH_INFO_URL': bad}):
                with self.assertRaises(TransportError): secure_http.user_agent()

    def test_api_key_header_is_dropped_on_redirect_to_another_host(self):
        first = fake_conn(302, location='https://www.govinfo.gov/content/x.pdf')
        second = fake_conn(200, {'Content-Length': '4'}, [b'%PDF'])
        with patch('secure_http.PinnedHTTPS', side_effect=[first, second]):
            secure_http.fetch('https://api.govinfo.gov/packages/X/pdf', {'api.govinfo.gov', 'www.govinfo.gov'}, 100, extra_headers={'X-Api-Key': 'k' * 40})
        sent = [c.request.call_args.kwargs['headers'] for c in (first, second)]
        self.assertEqual(sent[0]['X-Api-Key'], 'k' * 40)
        self.assertNotIn('X-Api-Key', sent[1])

    def test_probe_reads_headers_only(self):
        conn = fake_conn(200, {'Content-Length': '123456789', 'Content-Type': 'application/zip'})
        with patch('secure_http.PinnedHTTPS', return_value=conn):
            info = secure_http.probe('https://war.gov/a.zip', {'war.gov'})
        self.assertEqual(info['content_length'], 123456789)
        conn.getresponse.return_value.read.assert_not_called()

    def test_streaming_download_resume_restart_and_limit(self):
        with tempfile.TemporaryDirectory() as d:
            part = pathlib.Path(d) / 'f.part'
            part.write_bytes(b'abc')
            (pathlib.Path(d) / 'f.part.json').write_text(json.dumps({'url': 'https://war.gov/f', 'validator': '"v1"'}))
            conn = fake_conn(206, {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3', 'ETag': '"v1"'}, [b'def'])
            with patch('secure_http.PinnedHTTPS', return_value=conn):
                got = secure_http.download('https://war.gov/f', {'war.gov'}, part, 100)
            self.assertEqual(conn.request.call_args.kwargs['headers']['Range'], 'bytes=3-')
            self.assertEqual(conn.request.call_args.kwargs['headers']['If-Range'], '"v1"')
            self.assertTrue(got['resumed']); self.assertEqual(got['sha256'], hashlib.sha256(b'abcdef').hexdigest())
            self.assertEqual(part.read_bytes(), b'abcdef')
            part.write_bytes(b'abc')
            with patch('secure_http.PinnedHTTPS', return_value=fake_conn(200, {'Content-Length': '6'}, [b'abcdef'])):
                got = secure_http.download('https://war.gov/f', {'war.gov'}, part, 100)
            self.assertFalse(got['resumed']); self.assertEqual(part.read_bytes(), b'abcdef')
            part.unlink()
            with patch('secure_http.PinnedHTTPS', return_value=fake_conn(200, {}, [b'abc', b'def'])):
                with self.assertRaises(TransportError) as err:
                    secure_http.download('https://war.gov/f', {'war.gov'}, part, 4)
            self.assertEqual(err.exception.kind, 'limit'); self.assertFalse(part.exists())


class Downloads(Base):
    def setUp(self):
        super().setUp(); self.quiet('validate_url', 'check_robots', 'pace')

    def test_size_first_then_confirm(self):
        info = {'final_url': self.r.source('A035')['url'], 'status': 200, 'content_type': 'text/html', 'content_length': 2048, 'accept_ranges': True, 'last_modified': None}
        with patch('acquire.probe', return_value=info), patch('acquire.stream_download') as stream:
            out = acquire.download(self.r, 'A035')
        self.assertTrue(out['confirm_required']); stream.assert_not_called()

        def fake_stream(url, hosts, part, limit, extra_headers=None, on_redirect=None):
            pathlib.Path(part).write_bytes(b'PK\x05\x06' + b'\0' * 18)
            return {'final_url': url, 'bytes': 22, 'sha256': hashlib.sha256(b'PK\x05\x06' + b'\0' * 18).hexdigest(), 'resumed': False, 'content_type': 'application/zip', 'last_modified': None}
        with patch('acquire.probe', return_value=info), patch('acquire.stream_download', side_effect=fake_stream):
            out = acquire.download(self.r, 'A035', confirm=True)
        self.assertTrue(out['ok']); self.assertTrue(out['is_zip'])
        self.assertEqual(out['stored_as'], 'objects/' + out['sha256'])
        self.assertNotIn(str(self.state), json.dumps(out))
        self.assertEqual(len(json.loads((self.state / 'downloads' / 'index.json').read_text(encoding='utf-8'))), 1)

    def test_over_limit_and_low_disk(self):
        big = {'final_url': 'https://war.gov/x.zip', 'status': 200, 'content_type': 'application/zip', 'content_length': 900 * acquire.MB, 'accept_ranges': True, 'last_modified': None}
        with patch('acquire.probe', return_value=big):
            out = acquire.download(self.r, 'A035', confirm=True)
        self.assertEqual(out['class'], 'limit'); self.assertIn('--max-mb 900', out['next_steps'][0])
        with patch('acquire.probe', return_value=big), patch('acquire.shutil.disk_usage', return_value=types.SimpleNamespace(free=10)):
            with self.assertRaises(Problem):
                acquire.download(self.r, 'A035', confirm=True, max_mb=1000)

    def test_govinfo_route_needs_own_key(self):
        os.environ.pop('ALIEN_SEARCH_GOVINFO_KEY', None)
        with self.assertRaises(Problem) as err:
            acquire.download(self.r, 'A011', via='govinfo')
        self.assertEqual(err.exception.kind, 'auth')


def mark_encrypted(data, name):
    """Set the 'encrypted' flag on one entry (zipfile cannot write encrypted entries itself)."""
    data = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as z:
        local = z.getinfo(name.decode()).header_offset
    data[local + 6] |= 0x1
    pos = data.find(b'PK\x01\x02')
    while pos != -1:
        length = int.from_bytes(data[pos + 28:pos + 30], 'little')
        if bytes(data[pos + 46:pos + 46 + length]) == name: data[pos + 8] |= 0x1
        pos = data.find(b'PK\x01\x02', pos + 4)
    return bytes(data)


class Zips(Base):
    def store_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('docs/report.pdf', b'%PDF-1.4 report')
            z.writestr('notes.txt', b'hello')
            z.writestr('../escape.txt', b'x')
            z.writestr('/abs.txt', b'x')
            link = zipfile.ZipInfo('link'); link.external_attr = 0o120777 << 16; z.writestr(link, b'/etc/passwd')
            z.writestr('secret.txt', b'x')
            z.writestr('zeros.bin', b'\0' * (30 * acquire.MB))
        data = mark_encrypted(buf.getvalue(), b'secret.txt'); sha = hashlib.sha256(data).hexdigest()
        (self.r.objects / sha).write_bytes(data)
        return sha

    def test_list_flags_unsafe_members(self):
        sha = self.store_zip()
        out = acquire.zip_list(self.r, sha)
        problems = {m['name']: m['problem'] for m in out['members']}
        self.assertIsNone(problems['docs/report.pdf'])
        self.assertEqual(problems['../escape.txt'], 'unsafe path')
        self.assertEqual(problems['/abs.txt'], 'unsafe path')
        self.assertEqual(problems['link'], 'symbolic link')
        self.assertEqual(problems['secret.txt'], 'encrypted')
        self.assertEqual(problems['zeros.bin'], 'suspicious compression ratio')
        self.assertFalse((self.state / 'extracted').exists())

    def test_extract_only_safe_requested_files(self):
        sha = self.store_zip()
        out = acquire.zip_extract(self.r, sha, ['../escape.txt', 'link'], pdfs=True)
        done = {f['name']: f for f in out['files']}
        self.assertTrue(done['docs/report.pdf']['extracted'])
        self.assertEqual(done['docs/report.pdf']['stored_as'], 'extracted/%s/docs/report.pdf' % sha)
        self.assertFalse(done['../escape.txt']['extracted']); self.assertFalse(done['link']['extracted'])
        self.assertFalse((self.state / 'escape.txt').exists())
        self.assertFalse((self.state / 'extracted' / 'escape.txt').exists())
        self.assertTrue((self.state / 'extracted' / (sha + '.json')).exists())

    def test_extract_size_limit(self):
        sha = self.store_zip()
        self.assertEqual(acquire.zip_extract(self.r, sha, ['notes.txt', 'docs/report.pdf'], max_mb=1)['class'], 'ok')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_STORED) as z: z.writestr('big.bin', b'x' * (2 * acquire.MB))
        data = buf.getvalue(); sha2 = hashlib.sha256(data).hexdigest(); (self.r.objects / sha2).write_bytes(data)
        self.assertEqual(acquire.zip_extract(self.r, sha2, ['big.bin'], max_mb=1)['class'], 'limit')

    def test_bad_sha_is_rejected(self):
        for value in ('../../etc/passwd', 'A' * 64, ''):
            with self.assertRaises(Problem): acquire.zip_list(self.r, value)


class FakeDoc:
    def __init__(self, needs_pass=False, auth_ok=False, texts=('one', 'two'), fail=False, encrypted=False):
        self.needs_pass, self.auth_ok, self.texts, self.fail, self.is_encrypted = needs_pass, auth_ok, texts, fail, encrypted
    def __len__(self): return len(self.texts)
    def authenticate(self, password):
        self.tried = password
        if self.auth_ok: self.needs_pass = False; return 4
        return 0
    def __iter__(self):
        for t in self.texts:
            page = MagicMock()
            if self.fail: page.get_text.side_effect = RuntimeError('cannot read')
            else: page.get_text.return_value = t
            yield page
    def close(self): pass


class LockedPdfs(unittest.TestCase):
    def run_with(self, doc):
        fake = types.SimpleNamespace(open=lambda stream, filetype: doc)
        with patch.dict(sys.modules, {'fitz': fake}):
            return acquire.pdf_text(b'%PDF-1.7')

    def test_locked_pdf_is_labelled_not_cracked(self):
        doc = FakeDoc(needs_pass=True)
        out = self.run_with(doc)
        self.assertTrue(out['locked']); self.assertIn('password-protected', out['text_extraction'])
        self.assertEqual(doc.tried, '')

    def test_empty_password_pdf_is_read(self):
        out = self.run_with(FakeDoc(needs_pass=True, auth_ok=True))
        self.assertFalse(out['locked']); self.assertIn('[PDF page 2] two', out['text'])

    def test_encrypted_read_failure_is_locked_but_other_failures_raise(self):
        self.assertTrue(self.run_with(FakeDoc(fail=True, encrypted=True))['locked'])
        with self.assertRaises(ValueError): self.run_with(FakeDoc(fail=True))


class Imports(Base):
    def write(self, data):
        path = pathlib.Path(self.tmp.name) / 'mine.pdf'; path.write_bytes(data); return path

    def test_unmatched_file_stays_user_supplied(self):
        out = acquire.import_file(self.r, 'A014', self.write(b'%PDF-1.4 mine'), from_url='https://www.aaro.mil/x.pdf')
        self.assertEqual(out['evidence_status'], 'user-supplied'); self.assertEqual(out['matches'], [])
        self.assertTrue(out['stored_as'].startswith('objects/'))
        self.assertNotIn(self.tmp.name, json.dumps(out))

    def test_match_against_official_fetch_and_archived_capture(self):
        data = b'%PDF-1.4 official'; sha = hashlib.sha256(data).hexdigest()
        (self.r.cache / 'A014.json').write_text(json.dumps({'ok': True, 'source_id': 'A014', 'sha256': sha, 'source_url': 'https://www.aaro.mil/a.pdf', 'fetched_at': 'then', 'links': []}), encoding='utf-8')
        out = acquire.import_file(self.r, 'A014', self.write(data))
        self.assertEqual(out['evidence_status'], 'user-supplied; bytes match an official download (A014)')
        (self.r.cache / 'A014.json').unlink()
        (self.r.cache / 'snapshot-abc.json').write_text(json.dumps({'source_id': 'A014', 'sha256': sha, 'capture_timestamp': '20250101000000'}), encoding='utf-8')
        out = acquire.import_file(self.r, 'A014', self.write(data))
        self.assertEqual(out['evidence_status'], 'user-supplied; bytes match an archived capture (A014)')

    def test_verify_archive_uses_closest_capture(self):
        data = b'%PDF-1.4 archived'; sha = hashlib.sha256(data).hexdigest()
        closest = {'ok': True, 'archive_result': {'data': {'timestamp': '20250101000000'}}}
        snap = {'ok': True, 'sha256': sha, 'replay_url': 'https://web.archive.org/web/20250101000000id_/x'}
        with patch.object(self.r, 'closest', return_value=closest), patch.object(self.r, 'snapshot', return_value=snap):
            out = acquire.import_file(self.r, 'A014', self.write(data), verify_archive=True)
        self.assertEqual(out['archive_check']['sha256'], sha)

    def test_from_url_with_credentials_is_rejected(self):
        with self.assertRaises(Problem):
            acquire.import_file(self.r, 'A014', self.write(b'x'), from_url='https://user:pw@www.aaro.mil/x')


class ArchiveFallback(Base):
    def test_blocked_source_gets_labelled_archived_copy(self):
        closest = {'ok': True, 'archive_result': {'data': {'timestamp': '20250101000000'}}}
        snap = {'ok': True, 'capture_utc': '2025-01-01T00:00:00+00:00', 'replay_url': 'https://web.archive.org/web/20250101000000id_/x', 'format': 'html', 'title': 'Old', 'text_excerpt': 'old text', 'links': [], 'sha256': 'a' * 64}
        with patch.object(self.r, 'get_bytes', side_effect=Problem('blocked', 'HTTP 403', 'no')), patch.object(self.r, 'closest', return_value=closest) as c, patch.object(self.r, 'snapshot', return_value=snap):
            out = self.r.read('A014', archive_fallback=True)
            again = self.r.read('A014', archive_fallback=True)
        self.assertFalse(out['ok']); self.assertEqual(out['class'], 'blocked')
        copy = out['archived_copy']
        self.assertTrue(copy['available']); self.assertEqual(copy['evidence_status'], 'archived-copy')
        self.assertIn('not the live official page', copy['label']); self.assertEqual(copy['capture_timestamp'], '20250101000000')
        self.assertEqual(c.call_count, 1); self.assertTrue(again['archived_copy']['available'])

    def test_no_fallback_for_transient_errors_or_by_default(self):
        with patch.object(self.r, 'get_bytes', side_effect=Problem('network', 'down', 'no')), patch.object(self.r, 'closest') as c:
            self.assertNotIn('archived_copy', self.r.read('A014', archive_fallback=True))
            with patch.object(self.r, 'get_bytes', side_effect=Problem('blocked', 'HTTP 403', 'no')):
                self.assertNotIn('archived_copy', self.r.read('A001'))
        c.assert_not_called()

    def test_no_capture_is_said_plainly(self):
        with patch.object(self.r, 'get_bytes', side_effect=Problem('missing', 'HTTP 404', 'no')), patch.object(self.r, 'closest', return_value={'ok': True, 'archive_result': {'data': None}}):
            out = self.r.read('A014', archive_fallback=True)
        self.assertFalse(out['archived_copy']['available'])

    def test_archive_answers_are_reused(self):
        with patch('wayback_api.WaybackClient.closest', return_value={'data': {}, 'source_as_of': None}) as call, patch.object(self.r, 'pace'):
            self.r.closest('A001', '20200101'); second = self.r.closest('A001', '20200101')
        self.assertEqual(call.call_count, 1); self.assertTrue(second['cached'])
        with patch('wayback_api.WaybackClient.captures', return_value={'data': [], 'complete': True}) as call, patch.object(self.r, 'pace'):
            self.r.captures('A001', '2020', '2020'); self.r.captures('A001', '2020', '2020')
        self.assertEqual(call.call_count, 1)


class VerifySources(Base):
    def test_report_classes_and_private_report(self):
        self.quiet('validate_url', 'pace')
        def robots_check(url):
            if 'aaro.mil' in url: raise Problem('robots', 'disallowed', 'no')
        info = lambda ctype: {'final_url': 'x', 'status': 200, 'content_type': ctype, 'content_length': 10, 'accept_ranges': False, 'last_modified': None}
        with patch.object(self.r, 'check_robots', side_effect=robots_check), patch('acquire.probe', side_effect=lambda url, hosts, **kw: info('text/html')):
            out = acquire.verify_sources(self.r, ['A001', 'A014', 'A004'])
        classes = {row['id']: row['class'] for row in out['results']}
        self.assertEqual(classes, {'A001': 'ok', 'A014': 'robots', 'A004': 'drift'})
        self.assertTrue(out['report'].startswith('reports/verify-'))
        self.assertTrue((self.state / out['report']).exists())


class OfficialApis(Base):
    def test_set_key_validates_and_never_echoes(self):
        key = 'A' * 40
        out = official_apis.set_key(self.r, 'govinfo', key)
        self.assertNotIn(key, json.dumps(out))
        path = self.r.runtime / 'govinfo-api-key.txt'
        if os.name == 'posix': self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(official_apis.load_key(self.r, 'govinfo'), key)
        with self.assertRaises(Problem): official_apis.set_key(self.r, 'govinfo', 'short')
        with patch.dict(os.environ, {'ALIEN_SEARCH_GOVINFO_KEY': 'B' * 40}):
            self.assertEqual(official_apis.load_key(self.r, 'govinfo'), 'B' * 40)

    def test_govinfo_summary_strips_keys_and_maps_auth(self):
        official_apis.set_key(self.r, 'govinfo', 'C' * 40)
        body = json.dumps({'title': 'Report', 'dateIssued': '2024-03-01', 'download': {'pdfLink': 'https://api.govinfo.gov/packages/X/pdf?api_' + 'key=SECRETKEY123'}}).encode()
        with patch('official_apis.fetch', return_value=(body, 'u', {})) as fetch, patch.object(self.r, 'pace'):
            out = official_apis.govinfo_summary(self.r, 'A011')
        self.assertEqual(fetch.call_args.kwargs['extra_headers'], {'X-Api-Key': 'C' * 40})
        self.assertNotIn('SECRETKEY123', json.dumps(out)); self.assertNotIn('C' * 40, json.dumps(out))
        with patch('official_apis.fetch', side_effect=TransportError('x', kind='error', status=403)), patch.object(self.r, 'pace'):
            with self.assertRaises(Problem) as err: official_apis.govinfo_summary(self.r, 'A011')
        self.assertEqual(err.exception.kind, 'auth')
        with self.assertRaises(Problem): official_apis.govinfo_summary(self.r, 'A001')

    def test_nara_search_budget_notice_and_parsing(self):
        official_apis.set_key(self.r, 'nara', 'nara-key-1234')
        body = json.dumps({'body': {'hits': {'hits': [{'_source': {'record': {'naId': 733667, 'title': 'JAL 1628', 'levelOfDescription': 'series'}}}]}}}).encode()
        with patch.dict(os.environ, {'ALIEN_SEARCH_NARA_MONTHLY_LIMIT': '1'}), patch('official_apis.fetch', return_value=(body, 'u', {})), patch.object(self.r, 'pace'):
            out = official_apis.nara_search(self.r, 'japan airlines')
            self.assertEqual(out['data'][0]['catalog_url'], 'https://catalog.archives.gov/id/733667')
            self.assertIn('not endorsed or certified', out['notice'])
            with self.assertRaises(Problem) as err: official_apis.nara_search(self.r, 'again')
        self.assertEqual(err.exception.kind, 'rate')


class StorageV3(unittest.TestCase):
    def test_new_schema_legacy_import_and_private_export(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            store = storage.JsonStore(d / 'j'); store.put('CASE_1', {'a': 1})
            self.assertEqual(store.export()['schema'], 'alien-search-storage/1')
            legacy = d / 'legacy.json'; legacy.write_text(json.dumps({'schema': 'earlier-edition-storage/1', 'records': {'CASE_2': {'b': 2}}}), encoding='utf-8')
            run = lambda *a: subprocess.run([sys.executable, str(ROOT / 'storage.py'), '--data-dir', str(d / 's'), *a], capture_output=True, text=True)
            self.assertEqual(run('import', '--input', str(legacy)).returncode, 0)
            self.assertEqual(run('export', '--output', str(d / 'out.json')).returncode, 0)
            self.assertEqual(json.loads((d / 'out.json').read_text(encoding='utf-8'))['records'], {'CASE_2': {'b': 2}})
            if os.name == 'posix': self.assertEqual((d / 'out.json').stat().st_mode & 0o777, 0o600)
            bad = d / 'bad.json'; bad.write_text(json.dumps({'schema': 'something-else', 'records': {}}), encoding='utf-8')
            self.assertEqual(run('import', '--input', str(bad)).returncode, 1)


class StartersAndSkill(unittest.TestCase):
    def test_generated_files_are_current(self):
        done = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'build_starters.py'), '--check'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout)

    def test_skill_metadata_fits_claude_limits(self):
        text = (ROOT / '.claude' / 'skills' / 'alien-search' / 'SKILL.md').read_text(encoding='utf-8')
        front = re.match(r'^---\n(.*?)\n---\n', text, re.S)[1]
        fields = dict(line.split(': ', 1) for line in front.splitlines())
        self.assertEqual(fields['name'], 'alien-search')
        self.assertLessEqual(len(fields['name']), 64); self.assertLessEqual(len(fields['description']), 200)
        for ref in ('framework.md', 'sources.md'):
            self.assertIn(ref, text); self.assertTrue((ROOT / '.claude' / 'skills' / 'alien-search' / ref).exists())

    def test_starter_lists_every_source_and_unofficial_notice(self):
        starter = (ROOT / 'ASSISTANT_STARTER.md').read_text(encoding='utf-8')
        for s in json.loads((ROOT / 'sources.json').read_text(encoding='utf-8'))['sources']:
            self.assertIn('**%s** ' % s['id'], starter)
        self.assertIn('not affiliated with or endorsed by any government agency', starter.lower())


class PrivacyAudit(unittest.TestCase):
    def test_release_is_clean(self):
        count, problems, _ = audit_release.audit()
        self.assertGreater(count, 30); self.assertEqual(problems, [])

    def test_private_identity_terms_are_caught_without_printing(self):
        with tempfile.TemporaryDirectory() as d:
            terms = pathlib.Path(d) / 'identity-terms.txt'
            terms.write_text('# my identifiers\nUnidentified Anomalous\n', encoding='utf-8')
            with patch.object(audit_release, 'PRIVATE_TERMS', terms):
                _, problems, checked = audit_release.audit()
        self.assertEqual(checked, 1)
        self.assertIn('private identity term', {rule for _, rule in problems})

    def test_package_has_fixed_metadata_and_skill_layout(self):
        import package_release
        with tempfile.TemporaryDirectory() as d:
            out = package_release.build(d)
            # Both archives are closed before the folder is removed; Windows cannot delete open files.
            with zipfile.ZipFile(pathlib.Path(d) / out['repository_zip']) as repo, zipfile.ZipFile(pathlib.Path(d) / out['skill_zip']) as skill:
                self.check_archives(repo, skill, d, out)

    def check_archives(self, repo, skill, d, out):
        self.assertEqual({(i.date_time, i.external_attr >> 16) for i in repo.infolist()}, {((2026, 1, 1, 0, 0, 0), 0o100644)})
        self.assertTrue(all(n.startswith('alien-search/') for n in repo.namelist()))
        self.assertEqual(sorted(skill.namelist()), ['alien-search/LICENSE', 'alien-search/SKILL.md', 'alien-search/framework.md', 'alien-search/guide.md', 'alien-search/sources.md'])
        self.assertEqual(skill.read('alien-search/LICENSE'), (ROOT / 'LICENSE').read_bytes())
        self.assertEqual(repo.comment, b''); self.assertNotIn(d, json.dumps(out))


class LocalServerV3(Base):
    def test_health_notice_and_fallback_flag(self):
        server = make_server(self.r, 0, 'a' * 64)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            import urllib.request
            base = 'http://127.0.0.1:%d' % server.server_port
            health = json.load(urllib.request.urlopen(base + '/health'))
            self.assertIn('not affiliated', health['notice'])
            with patch.object(self.r, 'read', return_value={'ok': True}) as read:
                req = urllib.request.Request(base + '/v1/sources/A001/read?archive_fallback=0', headers={'Authorization': 'Bearer ' + 'a' * 64})
                json.load(urllib.request.urlopen(req))
            self.assertFalse(read.call_args.kwargs['archive_fallback'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
