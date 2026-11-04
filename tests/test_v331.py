"""Regression tests for the 3.3.1 fixes from the 2026-11-04 coding audit.

F1  PDF pages without a text layer (scans) are no longer reported as completely read.
F3  An HTTP 429 pauses the whole site, across source IDs, refresh and separate processes.
F4  Concurrent index appends (threads and separate processes) keep every entry.
F5  Error results keep the retry delay, in one normalized shape.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import MagicMock, patch

import acquire
import api
import common
import storage
from api import Records
from common import Problem, problem_envelope, text_status
from secure_http import TransportError

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = (b'<html><head><title>ok</title></head><body>hello</body></html>', 'https://www.aaro.mil/UAP-Records/-1/',
        {'Content-Type': 'text/html'})


class FakeDoc:
    """Minimal stand-in for a PyMuPDF document with the given text per page."""
    needs_pass = False
    is_encrypted = False

    def __init__(self, texts):
        self.texts = texts

    def __len__(self):
        return len(self.texts)

    def __iter__(self):
        for t in self.texts:
            page = MagicMock()
            page.get_text.return_value = t
            yield page

    def authenticate(self, password):
        return 0

    def close(self):
        pass


def pdf_with_pages(texts, limit=20000):
    fake = types.SimpleNamespace(open=lambda stream, filetype: FakeDoc(texts))
    with patch.dict(sys.modules, {'fitz': fake}):
        return acquire.pdf_text(b'%PDF-1.7', limit=limit)


class PdfCoverage(unittest.TestCase):  # F1
    def test_image_only_pdf_has_no_text(self):
        info = pdf_with_pages(['', '  \n'])
        self.assertEqual(text_status('pdf', info), 'none')
        self.assertEqual(info['text'], '')
        self.assertFalse(info['has_text'])
        self.assertEqual(info['pages_without_text'], '1-2')
        self.assertIn('no text layer', info['text_extraction'])

    def test_mixed_pdf_names_unread_pages(self):
        info = pdf_with_pages(['real text', '', ' ', 'more text', ''])
        self.assertEqual(text_status('pdf', info), 'partial')
        self.assertEqual(info['pages_without_text'], '2-3, 5')
        self.assertEqual(info['pages_with_text'], 2)
        self.assertIn('real text', info['text'])

    def test_text_pdf_is_complete_with_page_locators(self):
        info = pdf_with_pages(['first page', 'second page'])
        self.assertEqual(text_status('pdf', info), 'complete')
        self.assertIn('[PDF page 2] second page', info['text'])
        self.assertEqual(info['pages_without_text'], '')

    def test_limit_still_reports_partial(self):
        info = pdf_with_pages(['x' * 300] * 5, limit=500)
        self.assertEqual(text_status('pdf', info), 'partial')
        self.assertTrue(info['text_cut'])
        self.assertLess(info['pages_examined'], 5)

    def test_page_ranges(self):
        self.assertEqual(acquire.page_ranges([]), '')
        self.assertEqual(acquire.page_ranges([4]), '4')
        self.assertEqual(acquire.page_ranges([1, 2, 3, 7, 9, 10]), '1-3, 7, 9-10')

    @unittest.skipUnless(__import__('importlib').util.find_spec('fitz'), 'optional PyMuPDF not installed')
    def test_real_image_only_pdf(self):
        import fitz
        doc = fitz.open()
        page = doc.new_page()
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 40), False)
        pix.clear_with(180)
        page.insert_image(fitz.Rect(20, 20, 220, 220), pixmap=pix)
        info = acquire.pdf_text(doc.tobytes())
        self.assertEqual(text_status('pdf', info), 'none')
        self.assertEqual(info['text'], '')

    def test_read_reports_scan_and_guides_assistant(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        r = Records(data_dir=pathlib.Path(tmp.name) / 'state')
        info = pdf_with_pages(['', 'page two text', ''])
        with patch.object(r, 'validate_url'), patch.object(r, 'check_robots'), patch.object(r, 'pace'), \
                patch('api.fetch', return_value=(b'%PDF-1.7 fake', 'https://www.aaro.mil/x.pdf', {'Content-Type': 'application/pdf'})), \
                patch('acquire.pdf_text', return_value=info):
            result = r.read('A014')
        self.assertEqual(result['text_status'], 'partial')
        self.assertEqual(result['pages_without_text'], '1, 3')
        self.assertTrue(any('no text layer' in step and '1, 3' in step for step in result['next_steps']))


class SitePause(unittest.TestCase):  # F3
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = pathlib.Path(self.tmp.name) / 'state'
        self.r = Records(data_dir=self.state)
        for p in (patch.object(Records, 'validate_url'), patch.object(Records, 'check_robots'), patch('time.sleep')):
            p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def limited(self):
        return TransportError('Public source returned HTTP 429', kind='error', status=429, retry_after='3600', host='www.aaro.mil')

    def test_pause_covers_other_sources_on_same_site(self):
        with patch('api.fetch', side_effect=[self.limited(), HTML, HTML]) as fetch:
            self.assertEqual(self.r.read('A014')['class'], 'rate')
            other = self.r.read('A015')
            self.assertEqual(other['class'], 'rate')
            self.assertEqual(self.r.read('A015', refresh=True)['class'], 'rate')
            self.assertEqual(fetch.call_count, 1)  # no new request reached the paused site
            self.assertGreater(other['retry_after_seconds'], 3500)
            # Another site is unaffected.
            self.assertTrue(self.r.read('A001')['ok'])
        self.assertEqual(fetch.call_count, 2)

    def test_pause_survives_a_new_process_and_bare_host(self):
        with patch('api.fetch', side_effect=[self.limited()]):
            self.r.read('A014')
        fresh = Records(data_dir=self.state)  # what a second CLI run would build
        self.assertIsNotNone(fresh.paused_until('aaro.mil'))
        with self.assertRaises(Problem) as caught:
            fresh.pace('www.aaro.mil')
        self.assertEqual(caught.exception.kind, 'rate')

    def test_pause_blocks_downloads_before_any_request(self):
        self.r.note_rate('www.aaro.mil', '600')
        with patch('acquire.probe') as probe:
            with self.assertRaises(Problem) as caught:
                acquire.download(self.r, 'A014')
        self.assertEqual(caught.exception.kind, 'rate')
        probe.assert_not_called()

    def test_pause_is_never_shortened_and_expires(self):
        first = self.r.note_rate('www.aaro.mil', '600')
        self.assertEqual(self.r.note_rate('aaro.mil', '5'), first)
        with patch('time.time', return_value=first + 1):
            self.assertIsNone(self.r.paused_until('www.aaro.mil'))

    def test_rate_limit_without_host_uses_request_url(self):
        bare = TransportError('Public source returned HTTP 429', kind='error', status=429)
        problem = self.r.transport_problem(bare, 'https://www.cia.gov/x')
        self.assertIsNotNone(self.r.paused_until('www.cia.gov'))
        self.assertEqual(problem_envelope(problem)['retry_after_seconds'], common.DEFAULT_RETRY_AFTER)


class ConcurrentIndex(unittest.TestCase):  # F4
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.index = pathlib.Path(self.tmp.name) / 'index.json'

    def tearDown(self):
        self.tmp.cleanup()

    def test_threads_keep_every_entry(self):
        real = storage.load_json

        def slow(*args, **kwargs):  # widen the read-to-write window that lost entries before
            value = real(*args, **kwargs); time.sleep(0.002); return value

        def worker(n):
            for i in range(15): acquire._append_index(self.index, {'w': n, 'i': i})
        with patch('storage.load_json', side_effect=slow):
            threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
            for t in threads: t.start()
            for t in threads: t.join()
        items = json.loads(self.index.read_text(encoding='utf-8'))
        self.assertEqual(len(items), 90)
        self.assertEqual(len({(x['w'], x['i']) for x in items}), 90)

    def test_separate_processes_keep_every_entry(self):
        code = ('import sys,pathlib; sys.path.insert(0,sys.argv[1]); import acquire\n'
                'for i in range(25): acquire._append_index(pathlib.Path(sys.argv[2]),{"p":int(sys.argv[3]),"i":i})')
        procs = [subprocess.Popen([sys.executable, '-c', code, str(ROOT), str(self.index), str(n)]) for n in range(4)]
        for p in procs: self.assertEqual(p.wait(timeout=120), 0)
        items = json.loads(self.index.read_text(encoding='utf-8'))
        self.assertEqual(len({(x['p'], x['i']) for x in items}), 100)
        self.assertEqual(len(items), 100)

    def test_failed_change_keeps_previous_records(self):
        acquire._append_index(self.index, {'n': 1})

        def boom(items): raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):
            storage.update_json(self.index, boom, default=[], expect=list)
        self.assertEqual(json.loads(self.index.read_text(encoding='utf-8')), [{'n': 1}])
        acquire._append_index(self.index, {'n': 2})  # the lock was released
        self.assertEqual(len(json.loads(self.index.read_text(encoding='utf-8'))), 2)


class RetryMetadata(unittest.TestCase):  # F5
    def test_envelope_keeps_delay(self):
        out = problem_envelope(Problem('rate', 'slow down', 'after_wait', 3600))
        self.assertEqual(out['retry_after_seconds'], 3600)
        self.assertEqual(out['retry_after'], 3600)
        self.assertIn('T', out['retry_at'])

    def test_missing_or_invalid_header_falls_back_to_one_hour(self):
        for value in (None, 'not a date'):
            out = problem_envelope(Problem('rate', 'slow down', 'after_wait', value))
            self.assertEqual(out['retry_after_seconds'], common.DEFAULT_RETRY_AFTER)

    def test_other_errors_have_no_retry_fields(self):
        out = problem_envelope(Problem('input', 'bad id'))
        self.assertNotIn('retry_after_seconds', out)

    def test_cli_transport_error_keeps_delay(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        limited = TransportError('Public source returned HTTP 429', kind='error', status=429, retry_after='120', host='www.aaro.mil')
        printed = []
        argv = ['api.py', '--data-dir', str(pathlib.Path(tmp.name) / 'state'), 'download', 'A014']
        with patch.object(sys, 'argv', argv), patch('acquire.download', side_effect=limited), \
                patch('api.print_json', side_effect=printed.append):
            with self.assertRaises(SystemExit):
                api.main()
        self.assertEqual(printed[0]['class'], 'rate')
        self.assertAlmostEqual(printed[0]['retry_after_seconds'], 120, delta=2)
        self.assertEqual(printed[0]['retry_after'], '120')

    def test_archive_rate_result_has_same_fields(self):
        from wayback_api import APIError
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        r = Records(data_dir=pathlib.Path(tmp.name) / 'state')
        with patch('time.sleep'), patch('wayback_api.WaybackClient.closest', side_effect=APIError('rate', 'rate', 'after_wait', '90')):
            first = r.closest('A001', '20260101000000')
        second = r.closest('A001', '20250101000000')
        for out in (first, second):
            self.assertEqual(out['class'], 'rate')
            self.assertIn('retry_at', out)
            self.assertGreater(out['retry_after_seconds'], 80)
        self.assertIsNotNone(r.paused_until('web.archive.org'))


if __name__ == '__main__':
    unittest.main()
