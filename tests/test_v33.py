"""Tests for Alien Search 3.3: evidence integrity, site rules, safe state and fallback order.

Network access is faked at the HTTPS connection; the real http.client parser is used where it matters.
"""
import hashlib
import io
import http.client
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from unittest.mock import MagicMock, patch

import acquire
import robots
import secure_http
import storage
import wayback_api
from api import Records
from common import Problem, decode_text, text_status
from secure_http import TransportError

ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeSock:
    def __init__(self, raw): self.f = io.BytesIO(raw)
    def makefile(self, *a, **k): return self.f


def real_conn(raw):
    """A connection whose response is parsed by the real http.client from raw bytes."""
    conn = MagicMock()
    def respond():
        r = http.client.HTTPResponse(FakeSock(raw)); r.begin(); return r
    conn.getresponse.side_effect = respond
    return conn


def response(status, headers, body=b''):
    reason = {200: 'OK', 206: 'Partial Content', 302: 'Found'}[status]
    head = ''.join('%s: %s\r\n' % kv for kv in headers.items())
    return ('HTTP/1.1 %d %s\r\n%s\r\n' % (status, reason, head)).encode('latin-1') + body


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


class CompleteResponses(unittest.TestCase):
    def test_short_body_is_an_error_not_a_smaller_document(self):
        raw = response(200, {'Content-Length': '100'}, b'0123456789')
        with patch('secure_http.PinnedHTTPS', return_value=real_conn(raw)):
            with self.assertRaises(TransportError) as err:
                secure_http.fetch('https://www.archives.gov/x', {'www.archives.gov'}, 1000)
        self.assertEqual(err.exception.kind, 'network')

    def test_complete_body_is_accepted(self):
        raw = response(200, {'Content-Length': '4'}, b'%PDF')
        with patch('secure_http.PinnedHTTPS', return_value=real_conn(raw)):
            body, final, _ = secure_http.fetch('https://www.archives.gov/x', {'www.archives.gov'}, 1000)
        self.assertEqual(body, b'%PDF')


class Resume(unittest.TestCase):
    URL = 'https://war.gov/f.zip'

    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.part = pathlib.Path(self.d.name) / 'f.part'; self.meta = pathlib.Path(self.d.name) / 'f.part.json'

    def run_with(self, *raws):
        conns = [real_conn(r) for r in raws]
        with patch('secure_http.PinnedHTTPS', side_effect=conns):
            out = secure_http.download(self.URL, {'war.gov'}, self.part, 10**6)
        return out, conns

    def test_partial_file_without_validator_restarts(self):
        self.part.write_bytes(b'OLD')
        out, conns = self.run_with(response(200, {'Content-Length': '6'}, b'newnew'))
        self.assertNotIn('Range', conns[0].request.call_args.kwargs['headers'])
        self.assertFalse(out['resumed']); self.assertEqual(self.part.read_bytes(), b'newnew')

    def test_changed_file_is_not_spliced(self):
        self.part.write_bytes(b'OLD')
        self.meta.write_text(json.dumps({'url': self.URL, 'validator': '"v1"'}))
        # If-Range does not match: the server sends the whole new file with status 200.
        out, conns = self.run_with(response(200, {'Content-Length': '6', 'ETag': '"v2"'}, b'newnew'))
        self.assertEqual(conns[0].request.call_args.kwargs['headers']['If-Range'], '"v1"')
        self.assertFalse(out['resumed']); self.assertEqual(self.part.read_bytes(), b'newnew')
        self.assertEqual(out['sha256'], hashlib.sha256(b'newnew').hexdigest())

    def test_changed_validator_on_206_discards_partial(self):
        self.part.write_bytes(b'OLD')
        self.meta.write_text(json.dumps({'url': self.URL, 'validator': '"v1"'}))
        with self.assertRaises(TransportError) as err:
            self.run_with(response(206, {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3', 'ETag': '"v2"'}, b'new'))
        self.assertEqual(err.exception.kind, 'drift'); self.assertFalse(self.part.exists()); self.assertFalse(self.meta.exists())

    def test_partial_range_is_not_a_finished_file(self):
        self.part.write_bytes(b'OLD')
        self.meta.write_text(json.dumps({'url': self.URL, 'validator': '"v1"'}))
        with self.assertRaises(TransportError) as err:
            self.run_with(response(206, {'Content-Range': 'bytes 3-5/100', 'Content-Length': '3', 'ETag': '"v1"'}, b'new'))
        self.assertIn('incomplete', str(err.exception)); self.assertEqual(err.exception.kind, 'network')
        self.assertTrue(self.meta.exists())  # still resumable next time

    def test_valid_resume_and_cleanup(self):
        self.part.write_bytes(b'abc')
        self.meta.write_text(json.dumps({'url': self.URL, 'validator': 'Wed, 01 Jan 2025 00:00:00 GMT'}))
        out, _ = self.run_with(response(206, {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3', 'Last-Modified': 'Wed, 01 Jan 2025 00:00:00 GMT'}, b'def'))
        self.assertTrue(out['resumed']); self.assertEqual(out['sha256'], hashlib.sha256(b'abcdef').hexdigest())
        self.assertFalse(self.meta.exists())

    def test_fresh_download_saves_validator_while_running(self):
        raw = response(200, {'Content-Length': '6', 'ETag': '"v9"'}, b'abc')
        with patch('secure_http.PinnedHTTPS', return_value=real_conn(raw)):
            with self.assertRaises(TransportError):
                secure_http.download(self.URL, {'war.gov'}, self.part, 10**6)
        self.assertEqual(json.loads(self.meta.read_text())['validator'], '"v9"')

    def test_weak_etag_is_not_used(self):
        self.assertIsNone(secure_http._validator({'etag': 'W/"x"'}))
        self.assertEqual(secure_http._validator({'etag': 'W/"x"', 'last-modified': 'd'}), 'd')


class RedirectRules(Base):
    def test_robots_and_pacing_apply_to_every_redirect(self):
        seen = []
        def robots_check(url):
            seen.append(url)
            if '/private/' in url: raise Problem('robots', 'disallowed', 'no')
        conns = [real_conn(response(302, {'Location': '/private/doc.pdf', 'Content-Length': '0'})),
                 real_conn(response(200, {'Content-Length': '4'}, b'%PDF'))]
        with patch.object(self.r, 'validate_url'), patch.object(self.r, 'pace') as pace, \
                patch.object(self.r, 'check_robots', side_effect=robots_check), patch('secure_http.PinnedHTTPS', side_effect=conns):
            with self.assertRaises(Problem) as err:
                self.r.get_bytes('https://www.archives.gov/public/doc.pdf')
        self.assertEqual(err.exception.kind, 'robots')
        self.assertEqual(seen, ['https://www.archives.gov/public/doc.pdf', 'https://www.archives.gov/private/doc.pdf'])
        self.assertEqual(pace.call_count, 1)  # the disallowed destination is never requested

    def test_percent_encoded_paths_match_robots_rules(self):
        rules = ['User-agent: *', 'Disallow: /private']
        self.assertFalse(robots.cautious_allowed(rules, 'https://h/%70rivate/report'))
        self.assertFalse(robots.cautious_allowed(rules, 'https://h/%70%72ivate'))
        self.assertTrue(robots.cautious_allowed(rules, 'https://h/public'))
        self.assertFalse(robots.cautious_allowed(['User-agent: *', 'Disallow: /caf%C3%A9'], 'https://h/café/x'))


class Reading(Base):
    def read_with(self, body, ctype, sid='A001', **kw):
        with patch.object(self.r, 'get_bytes', return_value=(body, self.r.source(sid)['url'], {'Content-Type': ctype})):
            return self.r.read(sid, **kw)

    def test_declared_and_undeclared_legacy_encodings(self):
        page = '<html><head><title>Synthèse</title></head><body>Rapport</body></html>'.encode('cp1252')
        self.assertEqual(self.read_with(page, 'text/html; charset=windows-1252')['title'], 'Synthèse')
        self.assertEqual(self.read_with(page, 'text/html', refresh=True)['title'], 'Synthèse')
        meta = '<meta charset="iso-8859-1"><title>Synthèse</title>'.encode('latin-1')
        self.assertEqual(self.read_with(meta, 'text/html', refresh=True)['title'], 'Synthèse')
        self.assertEqual(decode_text('Ünïcode'.encode('utf-8'), ''), 'Ünïcode')

    def test_plain_text_is_kept_literally(self):
        out = self.read_with(b'Name: <REDACTED>\nStatus: <unknown>', 'text/plain')
        self.assertEqual(out['format'], 'text'); self.assertEqual(out['text_status'], 'complete')
        self.assertIn('<REDACTED>', out['text_excerpt']); self.assertIn('<unknown>', out['text_excerpt'])

    def test_unread_format_says_so(self):
        out = self.read_with(b'\x00\x01binary', 'application/octet-stream')
        self.assertTrue(out['ok']); self.assertEqual(out['text_status'], 'none')
        self.assertTrue(any('No text was extracted' in s for s in out['next_steps']))

    def test_incomplete_pdf_extraction_is_flagged(self):
        info = {'pages': 300, 'text': 'x' * 20000, 'text_complete': False, 'locked': False, 'text_extraction': 'pymupdf; OCR not performed'}
        with patch('acquire.pdf_text', return_value=info):
            out = self.read_with(b'%PDF-1.7', 'application/pdf', sid='A014', max_chars=20000)
        self.assertEqual(out['text_status'], 'partial'); self.assertFalse(out['text_truncated'])
        self.assertTrue(any('Only part' in s for s in out['next_steps']))

    def test_missing_pdf_parser_means_no_text(self):
        self.assertEqual(text_status('pdf', {'text_extraction': 'unavailable; optional PyMuPDF is not installed'}), 'none')

    def test_cache_follows_register_url_and_parser_format(self):
        first = self.read_with(b'<title>One</title>', 'text/html')
        self.assertFalse(first['cached'])
        self.assertTrue(self.read_with(b'<title>Two</title>', 'text/html')['cached'])
        self.r.byid['A001']['url'] = 'https://www.archives.gov/moved'
        out = self.read_with(b'<title>Two</title>', 'text/html')
        self.assertFalse(out['cached']); self.assertEqual(out['title'], 'Two')
        cache = self.r.cache / 'A001.json'
        data = json.loads(cache.read_text()); data['cache_format'] = 1; cache.write_text(json.dumps(data))
        self.assertFalse(self.read_with(b'<title>Two</title>', 'text/html')['cached'])

    def test_a_sites_request_to_pause_survives_an_upgrade(self):
        (self.r.cache / 'A001.json').write_text(json.dumps({'ok': False, 'class': 'rate', 'retry_not_before': 4102444800, 'source_url': 'old'}))
        with patch.object(self.r, 'get_bytes') as fetch:
            out = self.r.read('A001')
        fetch.assert_not_called(); self.assertEqual(out['class'], 'rate')

    def test_damaged_cache_is_rebuilt(self):
        (self.r.cache / 'A001.json').write_text('{not json')
        out = self.read_with(b'<title>Fine</title>', 'text/html')
        self.assertTrue(out['ok']); self.assertEqual(out['title'], 'Fine')

    def test_link_ids_stay_unique_when_short_ids_collide(self):
        real = hashlib.sha256
        class Same:
            def __init__(self, data=b''): self.h = real(data)
            def update(self, d): self.h.update(d)
            def hexdigest(self): return '0' * 12 + self.h.hexdigest()[12:]
        page = b'<a href="https://www.archives.gov/a.pdf">A</a><a href="https://www.archives.gov/b.pdf">B</a>'
        with patch('api.hashlib.sha256', side_effect=Same):
            out = self.read_with(page, 'text/html')
        ids = [d['id'] for d in out['data']]
        self.assertEqual(len(ids), 2); self.assertEqual(len(set(ids)), 2)


class FallbackOrder(Base):
    def blocked(self, sid, **kw):
        with patch.object(self.r, 'get_bytes', side_effect=Problem('blocked', 'HTTP 403', 'no')):
            return self.r.read(sid, **kw)

    def test_official_govinfo_copy_comes_before_archives(self):
        found = {'title': 'Report', 'date_issued': '2024-03-08', 'download_links': {'pdfLink': 'https://api.govinfo.gov/x/pdf'}}
        with patch('official_apis.load_key', return_value='k' * 40), patch('official_apis.govinfo_summary', return_value=found), \
                patch.object(self.r, '_archived_copy') as archive:
            out = self.blocked('A011', archive_fallback=True)
        archive.assert_not_called()
        self.assertTrue(out['official_alternate']['available']); self.assertEqual(out['official_alternate']['copy_kind'], 'official-alternate')
        self.assertIn('official_alternate', out['next_steps'][0])

    def test_without_key_the_steps_keep_the_framework_order(self):
        copy = {'available': True, 'capture_timestamp': '20250101000000'}
        with patch('official_apis.load_key', return_value=None), patch.object(self.r, '_archived_copy', return_value=copy):
            out = self.blocked('A011', archive_fallback=True)
        self.assertNotIn('official_alternate', out)
        steps = out['next_steps']
        self.assertLess(next(i for i, s in enumerate(steps) if 'official alternates' in s), next(i for i, s in enumerate(steps) if 'archived_copy' in s))
        self.assertIn('import-file', steps[-1])

    def test_government_web_archive_is_preferred_where_listed(self):
        with patch.object(self.r, '_archived_copy', return_value={'available': False}):
            out = self.blocked('A022', archive_fallback=True)
        self.assertTrue(any('government web archive' in s for s in out['next_steps']))

    def test_govinfo_failure_does_not_stop_the_archive(self):
        with patch('official_apis.load_key', return_value='k' * 40), patch('official_apis.govinfo_summary', side_effect=Problem('network', 'down')), \
                patch.object(self.r, '_archived_copy', return_value={'available': True, 'capture_timestamp': '1'}) as archive:
            out = self.blocked('A012', archive_fallback=True)
        archive.assert_called_once(); self.assertFalse(out['official_alternate']['available'])


class ArchiveReplays(unittest.TestCase):
    TS = '20250101000000'
    ORIGINAL = 'https://www.archives.gov/notes.txt'

    def headers(self, ctype):
        return {'Content-Type': ctype, 'Memento-Datetime': 'Wed, 01 Jan 2025 00:00:00 GMT'}

    def test_plain_text_replay_keeps_markers(self):
        final = 'https://web.archive.org/web/%sid_/%s' % (self.TS, self.ORIGINAL)
        with patch.object(wayback_api.WaybackClient, '_get', return_value=(b'Name: <REDACTED>', self.headers('text/plain'), final)):
            out = wayback_api.WaybackClient().page_text(self.ORIGINAL, self.TS)
        self.assertEqual(out['data']['text'], 'Name: <REDACTED>')

    def test_library_rejects_a_different_original(self):
        final = 'https://web.archive.org/web/%sid_/https://www.archives.gov/other.txt' % self.TS
        with patch.object(wayback_api.WaybackClient, '_get', return_value=(b'x', self.headers('text/plain'), final)):
            with self.assertRaises(wayback_api.APIError):
                wayback_api.WaybackClient().page_text(self.ORIGINAL, self.TS)

    def test_running_the_library_directly_explains_itself(self):
        done = subprocess.run([sys.executable, str(ROOT / 'wayback_api.py'), 'closest', 'https://www.archives.gov/'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2); self.assertIn('python api.py', done.stderr)


class SafeFiles(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup); self.p = pathlib.Path(self.d.name)

    def test_failed_write_keeps_the_previous_file(self):
        target = self.p / 'x.json'; storage.write_json(target, {'a': 1})
        with self.assertRaises(ValueError): storage.write_json(target, {'a': float('nan')})
        self.assertEqual(json.loads(target.read_text()), {'a': 1})
        with patch('storage.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): storage.write_json(target, {'a': 2})
        self.assertEqual(json.loads(target.read_text()), {'a': 1})
        self.assertEqual([f.name for f in self.p.iterdir()], ['x.json'])  # no temporary files left behind

    def test_concurrent_writers_do_not_collide(self):
        target = self.p / 'shared.json'; errors = []
        def work(n):
            try:
                for i in range(30): storage.write_json(target, {'writer': n, 'i': i})
            except Exception as e: errors.append(e)
        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(errors, []); self.assertIn('writer', json.loads(target.read_text()))

    def test_damaged_index_is_set_aside_not_lost(self):
        index = self.p / 'index.json'; index.write_text('[{"half":')
        acquire._append_index(index, {'n': 1})
        self.assertEqual(json.loads(index.read_text()), [{'n': 1}])
        self.assertEqual(len(list(self.p.glob('index.json.corrupt-*'))), 1)

    def test_damaged_stored_object_is_replaced(self):
        good = b'%PDF-1.4 good'; digest = hashlib.sha256(good).hexdigest()
        (self.p / digest).write_bytes(b'corrupted')
        storage.store_object(self.p, digest, data=good)
        self.assertEqual((self.p / digest).read_bytes(), good)

    @unittest.skipUnless(os.name == 'posix', 'POSIX permissions')
    def test_export_replaces_loose_permissions(self):
        out = self.p / 'export.json'; out.write_text('old'); out.chmod(0o644)
        storage.write_export(out, {'schema': storage.SCHEMA, 'records': {}})
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)

    def test_failed_export_keeps_previous_export(self):
        out = self.p / 'export.json'; out.write_text('previous')
        with self.assertRaises(ValueError): storage.write_export(out, {'bad': float('inf')})
        self.assertEqual(out.read_text(), 'previous')


class Stores(unittest.TestCase):
    def stores(self, d):
        return [storage.JsonStore(pathlib.Path(d) / 'json'), storage.SQLiteStore(pathlib.Path(d) / 'db.sqlite3')]

    def test_import_is_all_or_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            for store in self.stores(d):
                with self.assertRaises(ValueError) as err:
                    store.put_many({'first': {'ok': 1}, 'second': {'n': float('inf')}})
                self.assertIn('second', str(err.exception))
                with self.assertRaises(KeyError): store.get('first')

    def test_ids_are_portable_across_systems(self):
        with tempfile.TemporaryDirectory() as d:
            for store in self.stores(d):
                for bad in ('CON', 'nul', 'Com1', 'LPT9'):
                    with self.assertRaises(ValueError): store.put(bad, {})
                store.put('Case', {'v': 1})
                with self.assertRaises(ValueError): store.put('case', {'v': 2})
                with self.assertRaises(ValueError): store.put_many({'Pair': {}, 'pair': {}})
                store.put('Case', {'v': 3}); self.assertEqual(store.get('Case'), {'v': 3})

    def test_cli_import_reports_and_refuses_bad_files(self):
        with tempfile.TemporaryDirectory() as d:
            good = pathlib.Path(d) / 'good.json'; good.write_text(json.dumps({'schema': storage.SCHEMA, 'records': {'a': {}, 'b': {}}}))
            bad = pathlib.Path(d) / 'bad.json'; bad.write_text('{"schema":"%s","records":{"c":{},"d":{"x":1e999}}}' % storage.SCHEMA)
            run = lambda f: subprocess.run([sys.executable, str(ROOT / 'storage.py'), '--data-dir', str(pathlib.Path(d) / 's'), 'import', '--input', str(f)], capture_output=True, text=True)
            done = run(good); self.assertEqual(done.returncode, 0); self.assertEqual(json.loads(done.stdout), {'imported': 2})
            done = run(bad); self.assertEqual(done.returncode, 1); self.assertIn('"d"', done.stderr)
            self.assertFalse((pathlib.Path(d) / 's' / 'evidence' / 'c.json').exists())


class Lineage(Base):
    def test_files_extracted_from_an_official_download_keep_its_provenance(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            z.writestr('manifest.json', b'{"agency": "their own file"}')
            z.writestr('records/memo.pdf', b'%PDF-1.4 memo')
        data = buf.getvalue(); sha = hashlib.sha256(data).hexdigest()
        (self.r.objects / sha).write_bytes(data)
        downloads = storage.private_dir(self.state / 'downloads')
        acquire._append_index(downloads / 'index.json', {'source_id': 'A035', 'sha256': sha, 'copy_kind': 'official-download', 'final_url': 'https://www.archives.gov/x.zip'})
        out = acquire.zip_extract(self.r, sha, ['manifest.json', 'records/memo.pdf'])
        self.assertEqual(out['parent']['source_id'], 'A035'); self.assertEqual(out['extracted'], 2)
        self.assertEqual((self.state / 'extracted' / sha / 'manifest.json').read_bytes(), b'{"agency": "their own file"}')
        again = acquire.zip_extract(self.r, sha, ['manifest.json'])  # a second run does not trip over the member
        self.assertTrue(again['ok'])
        memo = pathlib.Path(self.tmp.name) / 'memo.pdf'; memo.write_bytes(b'%PDF-1.4 memo')
        imported = acquire.import_file(self.r, 'A001', memo)
        self.assertEqual(imported['evidence_status'], 'user-supplied; bytes match a file extracted from an official download (A035)')

    def test_credentials_in_from_url_are_never_stored(self):
        f = pathlib.Path(self.tmp.name) / 'f.pdf'; f.write_bytes(b'%PDF-1.4 x')
        param = 'access_' + 'token'  # assembled so the release audit's key-parameter rule does not flag this test
        out = acquire.import_file(self.r, 'A014', f, from_url='https://www.aaro.mil/f.pdf?id=7&%s=abc123' % param)
        self.assertEqual(out['from_url'], 'https://www.aaro.mil/f.pdf?id=7&%s=REDACTED' % param)
        self.assertNotIn('abc123', (self.state / 'imports' / 'index.json').read_text())

    def test_downloads_follow_site_rules_on_redirects(self):
        info = {'final_url': self.r.source('A035')['url'], 'status': 200, 'content_type': 'text/html', 'content_length': 10, 'accept_ranges': True, 'last_modified': None}
        with patch.object(self.r, 'validate_url'), patch.object(self.r, 'check_robots'), patch.object(self.r, 'pace'), \
                patch('acquire.probe', return_value=info) as probe:
            acquire.download(self.r, 'A035')
        self.assertEqual(probe.call_args.kwargs['on_redirect'], self.r.follow)


class WindowsSafety(unittest.TestCase):
    def test_every_low_level_open_uses_binary_mode(self):
        """On Windows, os.open without O_BINARY turns every \\n into \\r\\n, which would change a
        downloaded PDF's bytes and fingerprint. Every os.open call must include PRIVATE_OPEN."""
        import ast
        for name in ('acquire.py', 'api.py', 'common.py', 'official_apis.py', 'robots.py', 'secure_http.py', 'storage.py', 'wayback_api.py'):
            source = (ROOT / name).read_text(encoding='utf-8')
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and ast.unparse(node.func) == 'os.open':
                    self.assertIn('PRIVATE_OPEN', ast.unparse(node.args[1]), '%s line %d' % (name, node.lineno))


class Workflow(unittest.TestCase):
    def test_macos_job_runs_the_same_checks(self):
        """The hand-started macOS job must run exactly the same steps as the Linux and Windows jobs."""
        text = (ROOT / '.github' / 'workflows' / 'tests.yml').read_text(encoding='utf-8')
        main, mac = text.split('\n  macos:\n')
        steps = lambda block: [l.strip() for l in block.split('    steps:\n', 1)[1].splitlines() if l.strip() and 'python-version' not in l]
        self.assertEqual(steps(main), steps(mac))
        self.assertIn("if: github.event_name == 'workflow_dispatch'", mac)


@unittest.skipUnless(shutil.which('git'), 'Git is not installed')
class AuditCoverage(unittest.TestCase):
    """The privacy audit checks tracked files and, with --history, every earlier version and commit."""
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import audit_release
        self.audit = audit_release
        self.d = tempfile.TemporaryDirectory(); self.addCleanup(self.d.cleanup)
        self.root = pathlib.Path(self.d.name)  # left unresolved on purpose: Windows may give a short 8.3 path
        for name, value in (('ROOT', self.root), ('PRIVATE_TERMS', self.root / 'private_input' / 'identity-terms.txt')):
            p = patch.object(audit_release, name, value); p.start(); self.addCleanup(p.stop)
        (self.root / 'release-files.json').write_text('["release-files.json", "a.txt"]')
        (self.root / 'a.txt').write_text('public text')
        self.git('init', '-q')

    def git(self, *args, email='tester' + '@users.noreply.github.com', when='2026-01-01T00:00:00+00:00'):
        env = dict(os.environ, GIT_AUTHOR_DATE=when, GIT_COMMITTER_DATE=when)
        subprocess.run(['git', '-C', str(self.root), '-c', 'user.name=Tester', '-c', 'user.email=' + email, '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True, env=env)

    def test_patterns_for_links_keys_and_ipv6(self):
        find = lambda text: self.audit.scan_text(text, [])
        self.assertIn('private cloud or chat link', find('see https://chatgpt.com/' + 's/t_123'))
        self.assertIn('private cloud or chat link', find('https://claude.ai/' + 'code/session_123'))
        self.assertIn('credential-like token', find('gl' + 'pat-' + 'x' * 20))
        self.assertIn('IPv6 address', find('server 2606:4700:4700' + '::1111'))
        for harmless in ('items[::2]', '10:30:00', '2001:db8::1', '::1', 'aa:bb:cc:dd:ee:ff', 'a' * 64):
            self.assertEqual(find(harmless), [], harmless)

    def test_tracked_file_missing_from_the_release_list_is_scanned_and_reported(self):
        (self.root / 'notes.txt').write_text('mail me: someone' + '@' + 'example.org')
        self.git('add', '-A'); self.git('commit', '-qm', 'files')
        _, problems, _ = self.audit.audit()
        rules = {rule for name, rule in problems if name == 'notes.txt'}
        self.assertEqual(rules, {'tracked by Git but not in release-files.json', 'email address'})

    def test_history_finds_deleted_files_messages_and_personal_commit_emails(self):
        (self.root / 'old.txt').write_text('token ' + 'gh' + 'p_' + 'a' * 30)
        self.git('add', '-A'); self.git('commit', '-qm', 'first', email='someone' + '@' + 'example.org')
        (self.root / 'old.txt').unlink()
        self.git('add', '-A'); self.git('commit', '-qm', 'see https://claude.ai/' + 'chat/abc', when='2026-01-02T03:04:05-08:00')
        report = self.audit.history_audit()
        found = {(f['where'].split(' (')[0].split(' message')[0], f['rule']) for f in report['findings']}
        self.assertEqual(report['commits_scanned'], 2); self.assertTrue(report['history_complete'])
        self.assertIn(('old.txt', 'credential-like token'), found)
        self.assertTrue(any(rule == 'author email is not a no-reply address' for _, rule in found))
        self.assertTrue(any(rule == 'private cloud or chat link' for _, rule in found))
        self.assertEqual(sum(rule == 'author time records a time-zone offset' for _, rule in found), 1)  # only the -08:00 commit
        self.assertFalse(any('example.org' in json.dumps(f) for f in report['findings']))  # values are never printed

    def test_only_real_no_reply_addresses_count(self):
        ok = ['12345+someone' + '@users.noreply.github.com', 'someone' + '@users.noreply.github.com', 'noreply' + '@github.com', 'no-reply' + '@example.org']
        personal = ['name.noreply' + '@example.org', 'noreply.name' + '@example.org', 'someone' + '@example.org']
        self.assertTrue(all(self.audit.no_reply(e) for e in ok))
        self.assertFalse(any(self.audit.no_reply(e) for e in personal))


class StandardSkillFolder(unittest.TestCase):
    """skills/alien-search/ is what Agent Skills installers find; it must match the Claude Code copy."""
    def test_both_skill_folders_hold_the_same_files(self):
        claude, standard = ROOT / '.claude' / 'skills' / 'alien-search', ROOT / 'skills' / 'alien-search'
        names = sorted(p.name for p in claude.iterdir())
        self.assertEqual(names, sorted(p.name for p in standard.iterdir()))
        for name in names:
            self.assertEqual((claude / name).read_bytes(), (standard / name).read_bytes(), name)

    def test_header_meets_the_agent_skills_rules(self):
        text = (ROOT / 'skills' / 'alien-search' / 'SKILL.md').read_text(encoding='utf-8')
        header = dict(line.split(': ', 1) for line in text.split('---')[1].strip().splitlines())
        self.assertEqual(header['name'], 'alien-search')  # must match the folder name
        self.assertRegex(header['name'], r'^[a-z0-9]+(-[a-z0-9]+)*$'); self.assertLessEqual(len(header['name']), 64)
        self.assertLessEqual(len(header['description']), 200)  # Claude's upload limit, stricter than the standard's
        self.assertTrue(header['license'].startswith('MIT'))


if __name__ == '__main__':
    unittest.main()
