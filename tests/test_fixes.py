"""Regression tests for the 2.0.1 fixes.

Archive tests mock secure_http.fetch (the real network boundary) rather than WaybackClient._get,
so a mismatch between what fetch returns and what callers unpack cannot hide behind a fixture.
"""
import ast
import calendar
import email.utils
import errno
import gc
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
import time
import unittest
import warnings
from unittest.mock import patch, MagicMock

import api
import storage
from api import Records, make_server
from secure_http import TransportError
from wayback_api import WaybackClient, replay_url

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_URL = 'https://www.archives.gov/research/topics/uaps'  # A001 in sources.json


def memento_for(url):
    stamp = re.search(r'/web/(\d{14})', url)[1]
    return email.utils.formatdate(calendar.timegm(time.strptime(stamp, '%Y%m%d%H%M%S')), usegmt=True)


def archive_fetch(pages):
    """Fake secure_http.fetch with the real return order: (body, final_url, headers)."""
    calls = []

    def fake(url, hosts, max_bytes, timeout=30):
        calls.append(url)
        body, ctype = pages(url)
        return body, url, {'Content-Type': ctype, 'Memento-Datetime': memento_for(url)}
    fake.calls = calls
    return fake


class ArchiveThroughRealFetchShape(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Records(data_dir=pathlib.Path(self.tmp.name) / 'state')
        self.no_pace = patch.object(self.r, 'pace')
        self.no_pace.start()

    def tearDown(self):
        self.no_pace.stop()
        self.tmp.cleanup()

    def test_wayback_get_returns_body_headers_final_from_real_fetch(self):
        connection = MagicMock()
        response = connection.getresponse.return_value
        headers = {'Content-Type': 'text/html', 'Memento-Datetime': 'Thu, 01 Jan 2026 00:00:00 GMT'}
        response.status = 200
        response.getheader.side_effect = lambda k, default=None: headers.get(k, default)
        response.getheaders.return_value = list(headers.items())
        response.read.return_value = b'<html></html>'
        url = replay_url(SOURCE_URL, '20260101000000', True)
        with patch('secure_http.PinnedHTTPS', return_value=connection):
            body, got_headers, final = WaybackClient()._get(url)
        self.assertEqual(body, b'<html></html>')
        self.assertIsInstance(got_headers, dict)
        self.assertEqual(final, url)

    def test_html_snapshot(self):
        page = b'<html><title>Archive</title><body>Historical text<a href="/old.pdf">Old</a></body></html>'
        fake = archive_fetch(lambda url: (page, 'text/html'))
        with patch('wayback_api.fetch', side_effect=fake):
            out = self.r.snapshot('A001', '20260101000000')
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['format'], 'html')
        self.assertEqual(out['title'], 'Archive')
        self.assertIn('Historical text', out['text_excerpt'])
        self.assertEqual(out['links'], ['https://www.archives.gov/old.pdf'])
        self.assertEqual(len(fake.calls), 1)  # page_text reuses the same replay response

    def test_pdf_snapshot(self):
        fake = archive_fetch(lambda url: (b'%PDF-1.4 test', 'application/pdf'))
        # Simulate the optional PDF parser being absent so the result does not depend on the machine.
        with patch('wayback_api.fetch', side_effect=fake), patch.dict(sys.modules, {'fitz': None}):
            out = self.r.snapshot('A001', '20260101000000')
        self.assertTrue(out['ok'], out)
        self.assertEqual(out['format'], 'pdf')
        self.assertEqual(len(out['sha256']), 64)

    def test_compare_end_to_end(self):
        def pages(url):
            word = b'before' if '/20260101000000' in url else b'after'
            return b'<html><title>T</title><body>' + word + b'</body></html>', 'text/html'
        with patch('wayback_api.fetch', side_effect=archive_fetch(pages)):
            out = self.r.compare_snapshots('A001', '20260101000000', '20260102000000')
        self.assertTrue(out['ok'], out)
        self.assertFalse(out['text_identical'])
        self.assertIn('+after', out['diff_text'])

    def test_wayback_page_text(self):
        page = b'<html><title>X</title><body>Hello</body></html>'
        with patch('wayback_api.fetch', side_effect=archive_fetch(lambda url: (page, 'text/html'))):
            out = WaybackClient().page_text(SOURCE_URL, '20260101000000')
        self.assertTrue(out['ok'])
        self.assertEqual(out['data']['text'], 'X\nHello')


class ReadCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Records(data_dir=pathlib.Path(self.tmp.name) / 'state')
        for target in (patch.object(self.r, 'validate_url'), patch.object(self.r, 'pace'), patch.object(self.r, 'check_robots')):
            target.start(); self.addCleanup(target.stop)
        self.ok = (b'<html><title>ok</title></html>', SOURCE_URL, {'Content-Type': 'text/html'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_transient_network_failure_is_retried_next_call(self):
        with patch('api.fetch', side_effect=[TransportError('Public source transport failed'), self.ok]) as fetch:
            self.assertEqual(self.r.read('A001')['class'], 'network')
            out = self.r.read('A001')
        self.assertTrue(out['ok'])
        self.assertEqual(fetch.call_count, 2)

    def test_blocked_result_reused_briefly_then_refetched(self):
        blocked = TransportError('Public source returned HTTP 403', kind='error', status=403)
        now = time.time()
        with patch('api.fetch', side_effect=[blocked, self.ok]) as fetch:
            self.assertEqual(self.r.read('A001')['class'], 'blocked')
            self.assertTrue(self.r.read('A001')['cached'])
            with patch('time.time', return_value=now + api.FAIL_TTL + 1):
                self.assertTrue(self.r.read('A001')['ok'])
        self.assertEqual(fetch.call_count, 2)

    def test_rate_limit_honoured_then_released(self):
        limited = TransportError('Public source returned HTTP 429', kind='error', status=429, retry_after='60')
        now = time.time()
        with patch('api.fetch', side_effect=[limited, self.ok]) as fetch:
            self.assertEqual(self.r.read('A001')['class'], 'rate')
            self.assertEqual(self.r.read('A001', refresh=True)['class'], 'rate')
            with patch('time.time', return_value=now + 61):
                self.assertTrue(self.r.read('A001')['ok'])
        self.assertEqual(fetch.call_count, 2)

    def test_retry_after_is_capped_and_parses_dates(self):
        now = 1_800_000_000
        self.assertEqual(api.retry_delay('99999999999', now), api.MAX_RETRY_AFTER)
        self.assertEqual(api.retry_delay('-5', now), 1)
        self.assertEqual(api.retry_delay('not a date', now), 3600)
        self.assertAlmostEqual(api.retry_delay(email.utils.formatdate(now + 120, usegmt=True), now), 120, delta=1)

    def test_archive_cooldown_is_capped(self):
        from wayback_api import APIError
        with patch('wayback_api.WaybackClient.closest', side_effect=APIError('rate', 'rate', 'after_wait', '99999999999')):
            self.r.closest('A001', '20260101000000')
        gate = json.loads((self.r.cache / 'archive-retry.json').read_text(encoding='utf-8'))
        self.assertLessEqual(gate['not_before'], time.time() + api.MAX_RETRY_AFTER + 1)


class Encoding(unittest.TestCase):
    CODE = ['api.py', 'common.py', 'acquire.py', 'robots.py', 'official_apis.py', 'storage.py', 'wayback_api.py', 'secure_http.py',
            'scripts/audit_release.py', 'scripts/package_release.py', 'scripts/build_starters.py']

    def test_text_io_declares_encoding(self):
        """Windows defaults to cp1252; every text read/write must state its encoding."""
        problems = []
        for name in self.CODE:
            tree = ast.parse((ROOT / name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                called = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
                keywords = {k.arg for k in node.keywords}
                if called in ('read_text', 'write_text') and 'encoding' not in keywords:
                    problems.append((name, node.lineno))
                if (isinstance(func, ast.Name) and func.id == 'open') or called == 'fdopen':
                    mode = node.args[1] if len(node.args) > 1 else next((k.value for k in node.keywords if k.arg == 'mode'), None)
                    mode = mode.value if isinstance(mode, ast.Constant) else 'r'
                    if 'b' not in mode and 'encoding' not in keywords:
                        problems.append((name, node.lineno))
        self.assertEqual(problems, [])

    def test_json_files_from_windows_redirects_import(self):
        value = {'finding': 'Synthèse — Trans-en-Provence'}
        with tempfile.TemporaryDirectory() as d:
            for i, raw in enumerate([json.dumps(value).encode('utf-16'),
                                     b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False).encode('utf-8'),
                                     json.dumps(value, ensure_ascii=False).encode('utf-8')]):
                path = pathlib.Path(d) / ('f%d.json' % i)
                path.write_bytes(raw)
                self.assertEqual(storage.read_json_file(path), value)
            legacy = pathlib.Path(d) / 'cp1252.json'
            legacy.write_bytes(json.dumps(value, ensure_ascii=False).encode('cp1252'))
            with self.assertRaises(ValueError):
                storage.read_json_file(legacy)

    def test_print_json_is_ascii_on_non_utf8_streams(self):
        value = {'title': 'Synthèse — ≈'}
        legacy = io.TextIOWrapper(io.BytesIO(), encoding='cp1252')
        storage.print_json(value, legacy); legacy.flush()
        self.assertEqual(json.loads(legacy.buffer.getvalue().decode('ascii')), value)
        modern = io.TextIOWrapper(io.BytesIO(), encoding='utf-8')
        storage.print_json(value, modern); modern.flush()
        self.assertIn('Synthèse'.encode('utf-8'), modern.buffer.getvalue())

    def test_cli_export_redirect_roundtrip_with_cp1252_console(self):
        value = {'finding': 'Synthèse — Trans-en-Provence'}
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / 'rec.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
            env = dict(os.environ, PYTHONIOENCODING='cp1252')
            run = lambda *args, **kw: subprocess.run([sys.executable, str(ROOT / 'storage.py'), *args], env=env, capture_output=True, check=True, **kw)
            run('--data-dir', str(d / 'a'), 'put', 'CASE_1', '--input', str(d / 'rec.json'))
            (d / 'export.json').write_bytes(run('--data-dir', str(d / 'a'), 'export').stdout)
            run('--data-dir', str(d / 'b'), 'import', '--input', str(d / 'export.json'))
            run('--data-dir', str(d / 'a'), 'export', '--output', str(d / 'export2.json'))
            run('--data-dir', str(d / 'c'), 'import', '--input', str(d / 'export2.json'))
            for store in ('b', 'c'):
                self.assertEqual(storage.JsonStore(d / store / 'evidence').get('CASE_1'), value)

    def test_windows_default_state_directory(self):
        self.assertEqual(str(storage.default_data_dir('nt', {'LOCALAPPDATA': 'LocalAppData'})), str(pathlib.Path('LocalAppData') / 'alien-search'))
        self.assertTrue(str(storage.default_data_dir('posix', {})).endswith(str(pathlib.Path('.local/share/alien-search'))))


@unittest.skipUnless(os.name == 'posix', 'POSIX permission bits')
class StateDirectoryPermissions(unittest.TestCase):
    def test_chosen_existing_directory_keeps_its_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            docs = pathlib.Path(d) / 'Documents'; docs.mkdir(); docs.chmod(0o755)
            r = Records(data_dir=docs)
            self.assertEqual(docs.stat().st_mode & 0o777, 0o755)
            self.assertEqual(r.runtime.stat().st_mode & 0o777, 0o700)

    def test_new_chosen_directory_is_owner_only(self):
        with tempfile.TemporaryDirectory() as d:
            r = Records(data_dir=pathlib.Path(d) / 'new-state')
            self.assertEqual(r.state.stat().st_mode & 0o777, 0o700)

    def test_world_writable_existing_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            shared = pathlib.Path(d) / 'shared'; shared.mkdir(); shared.chmod(0o777)
            with self.assertRaises(ValueError):
                Records(data_dir=shared)

    def test_default_location_is_tightened(self):
        with tempfile.TemporaryDirectory() as d:
            loose = pathlib.Path(d) / 'loose'; loose.mkdir(); loose.chmod(0o755)
            storage.private_dir(loose)
            self.assertEqual(loose.stat().st_mode & 0o777, 0o700)


class TokenFile(unittest.TestCase):
    def test_first_write_is_complete_and_leaves_no_temp_files(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / 'api-token.txt'
            token = storage.load_or_create_token(p)
            self.assertEqual(p.read_text(encoding='ascii').strip(), token)
            self.assertEqual([x.name for x in pathlib.Path(d).iterdir()], ['api-token.txt'])
            if os.name == 'posix':
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_filesystem_without_hard_links(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / 'api-token.txt'
            with patch('storage.os.link', side_effect=OSError(errno.EPERM, 'no hard links')):
                token = storage.load_or_create_token(p)
            self.assertEqual(storage.load_or_create_token(p), token)
            self.assertEqual(len(list(pathlib.Path(d).iterdir())), 1)

    def test_concurrent_creator_wins_race(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / 'api-token.txt'; other = 'b' * 64
            def lose(src, dst):
                storage._write_new(dst, other)
                raise FileExistsError(dst)
            with patch('storage.os.link', side_effect=lose):
                self.assertEqual(storage.load_or_create_token(p), other)

    def test_damaged_token_names_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / 'api-token.txt'
            storage._write_new(p, 'not-a-token')
            with self.assertRaisesRegex(ValueError, re.escape(str(p))):
                storage.load_or_create_token(p)

    def test_empty_token_from_an_interrupted_start_is_replaced(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / 'api-token.txt'
            os.close(os.open(p, os.O_CREAT | os.O_WRONLY, 0o600))
            token = storage.load_or_create_token(p)
            self.assertRegex(token, '^[a-f0-9]{64}$'); self.assertEqual(storage.load_or_create_token(p), token)


class Stores(unittest.TestCase):
    def test_missing_record_is_keyerror_for_every_backend(self):
        with tempfile.TemporaryDirectory() as d:
            for store in (storage.JsonStore(pathlib.Path(d) / 'j'), storage.SQLiteStore(pathlib.Path(d) / 's' / 'e.sqlite3')):
                with self.assertRaises(KeyError):
                    store.get('NOPE')

    def test_sqlite_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as d, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            store = storage.SQLiteStore(pathlib.Path(d) / 'e.sqlite3')
            store.put('CASE_1', {'a': 1}); store.get('CASE_1'); store.export()
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_cli_missing_record_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as d:
            done = subprocess.run([sys.executable, str(ROOT / 'storage.py'), '--data-dir', d, 'get', 'NOPE'], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertNotIn('Traceback', done.stderr)


class LocalServer(unittest.TestCase):
    def test_non_ascii_authorization_gets_401(self):
        with tempfile.TemporaryDirectory() as d:
            r = Records(data_dir=pathlib.Path(d)); server = make_server(r, 0, 'a' * 64)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as s:
                    s.sendall(b'GET /v1/sources HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer \xe9\r\nConnection: close\r\n\r\n')
                    reply = b''
                    while chunk := s.recv(4096):
                        reply += chunk
                self.assertTrue(reply.startswith(b'HTTP/1.0 401'), reply[:40])
            finally:
                server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main(verbosity=2)
