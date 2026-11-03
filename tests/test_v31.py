"""Tests for 3.1: guidance for assistants and agent-friendly tool output."""
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import acquire
from api import Records
from common import Problem

ROOT = pathlib.Path(__file__).resolve().parents[1]
STARTER = (ROOT / 'ASSISTANT_STARTER.md').read_text(encoding='utf-8')
GUIDE = (ROOT / 'docs' / 'ASSISTANT_GUIDE.md').read_text(encoding='utf-8')


class GuideContent(unittest.TestCase):
    def test_starter_opens_with_guide_after_person_section(self):
        order = [STARTER.index(h) for h in ('## For the person using it', '## Assistant guide', '### Always', '### Your first reply',
                                             '### What you can do in this chat', '### Newer than your training', '### Status words',
                                             '### Answer template', '## Alien Search research framework', '## Public source register')]
        self.assertEqual(order, sorted(order))
        self.assertIn('assistants: skip', STARTER)

    def test_guide_covers_first_move_scope_and_dates(self):
        self.assertIn('Do not summarize this file', GUIDE)
        self.assertIn('not your training cutoff', GUIDE)
        for fact in ('Executive Order 14347', 'May 8, 2026', 'information cutoff of May 30, 2025', 'September 10, 2025'):
            self.assertIn(fact, GUIDE)
        self.assertIn('NEW-1', GUIDE)

    def test_every_tool_class_is_explained(self):
        for kind in ('ok', 'input', 'auth', 'blocked', 'missing', 'drift', 'robots', 'rate', 'limit', 'network, error'):
            self.assertRegex(GUIDE, r'\| %s \|' % re.escape(kind))
        for word in ('`candidate`', '`quarantined`', '`after_fix`', '`after_wait`'):
            self.assertIn(word, GUIDE)

    def test_register_has_review_notes_aliases_and_gaps(self):
        self.assertRegex(STARTER, r'\*\*A021\*\* [^\n]*Also: JAL 1628')
        self.assertIn('Review: Blocked: HTTP 403', STARTER)
        self.assertIn('Known gaps (not in the register yet)', STARTER)
        self.assertIn('USS Nimitz 2004', STARTER)

    def test_skill_reads_guide_first_and_triggers_on_key_terms(self):
        skill = (ROOT / '.claude' / 'skills' / 'alien-search' / 'SKILL.md').read_text(encoding='utf-8')
        description = re.search(r'^description: (.*)$', skill, re.M)[1]
        for term in ('PURSUE', 'AARO', 'Blue Book', 'Roswell'):
            self.assertIn(term, description)
        self.assertLessEqual(len(description), 200)
        self.assertLess(skill.index('guide.md'), skill.index('framework.md'))
        self.assertTrue((ROOT / '.claude' / 'skills' / 'alien-search' / 'guide.md').exists())


class ToolOutput(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Records(data_dir=pathlib.Path(self.tmp.name) / 's')

    def tearDown(self):
        self.tmp.cleanup()

    def test_register_search_finds_aliases(self):
        self.assertEqual([s['id'] for s in self.r.list(query='JAL')['data']], ['A021'])
        self.assertIn('A014', [s['id'] for s in self.r.list(query='kona blue')['data']])

    def test_empty_search_points_to_known_gaps(self):
        out = self.r.list(query='nimitz tic tac')
        self.assertEqual(out['total'], 0)
        self.assertIn('Nimitz', out['known_gaps'][0]['topic'])
        self.assertIn('NEW-1', out['next_steps'][0])

    def test_blocked_read_gives_next_steps_and_alternates(self):
        with patch.object(self.r, 'get_bytes', side_effect=Problem('blocked', 'HTTP 403', 'no')):
            out = self.r.read('A011')
        self.assertEqual(out['alternates'][0]['kind'], 'official-api')
        steps = ' '.join(out['next_steps'])
        self.assertIn('official alternates', steps); self.assertIn('import-file A011', steps)

    def test_successful_read_points_to_download_and_review(self):
        page = b'<html><title>T</title><a href="/x.pdf">Report</a></html>'
        with patch.object(self.r, 'get_bytes', return_value=(page, 'https://www.archives.gov/research/topics/uaps', {'Content-Type': 'text/html'})):
            out = self.r.read('A001')
        self.assertIn('download A001 LINK_ID', out['next_steps'][0])
        self.assertIn('register_date_note', out); self.assertNotIn('source_as_of_hint', out)

    def test_rate_and_network_steps(self):
        with patch.object(self.r, 'get_bytes', side_effect=Problem('rate', '429', 'after_wait', '60')):
            self.assertIn('Wait until retry_after', self.r.read('A001')['next_steps'][0])
        with patch.object(self.r, 'get_bytes', side_effect=Problem('network', 'down', 'no')):
            self.assertIn('own web tools', self.r.read('A002')['next_steps'][0])

    def test_unknown_id_and_allowlist_errors_carry_hints(self):
        with self.assertRaises(Problem) as err: self.r.source('A099')
        self.assertIn('python api.py sources', err.exception.next_steps[0])
        with self.assertRaises(Problem) as err: self.r.validate_url('https://example.com/x')
        self.assertIn('Do not edit the allowlist', err.exception.next_steps[0])

    def test_cli_prints_next_steps_for_errors(self):
        done = subprocess.run([sys.executable, str(ROOT / 'api.py'), '--data-dir', str(pathlib.Path(self.tmp.name) / 'cli'), 'read', 'A099'], capture_output=True, text=True)
        out = json.loads(done.stdout)
        self.assertEqual(done.returncode, 1); self.assertEqual(out['class'], 'input'); self.assertTrue(out['next_steps'])

    def test_download_asks_user_first_in_decimal_megabytes(self):
        info = {'final_url': 'https://war.gov/x.zip', 'status': 200, 'content_type': 'application/zip', 'content_length': 123_700_000, 'accept_ranges': True, 'last_modified': None}
        for name in ('validate_url', 'check_robots', 'pace'):
            p = patch.object(self.r, name); p.start(); self.addCleanup(p.stop)
        with patch('acquire.probe', return_value=info):
            out = acquire.download(self.r, 'A019')
            limited = acquire.download(self.r, 'A019', max_mb=100, confirm=True)
        self.assertEqual(out['size_mb'], 123.7)
        self.assertTrue(out['next_steps'][0].startswith('Ask the user'))
        self.assertIn('--confirm', out['next_steps'][1])
        self.assertEqual(limited['class'], 'limit'); self.assertIn('Only if they want it', limited['next_steps'][0])


class NoLeftovers(unittest.TestCase):
    CODE = ['api.py', 'acquire.py', 'common.py', 'official_apis.py', 'robots.py', 'secure_http.py', 'storage.py', 'wayback_api.py']

    def test_removed_features_stay_removed(self):
        gone = ['preserved' + '_snapshots', 'use' + '_engine', 'shared' + '_connector', 'current' + '_time', 'def ' + 'main()']
        for name in self.CODE:
            text = (ROOT / name).read_text(encoding='utf-8')
            for token in gone:
                if token == 'def main()' and name == 'api.py': continue
                if token == 'def main()' and name == 'storage.py': continue
                self.assertNotIn(token, text, name)

    def test_docs_match_the_tool(self):
        import common, secure_http
        self.assertIn(secure_http.PRODUCT + ' (unofficial public-records research)', (ROOT / 'docs' / 'API.md').read_text(encoding='utf-8'))
        spec = json.loads((ROOT / 'openapi.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['info']['version'], common.VERSION)
        self.assertNotRegex(json.dumps(spec), r'\b\d+-source\b')
        self.assertIn('belonged to the original reviewer', STARTER)

    def test_package_names(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import package_release
        with tempfile.TemporaryDirectory() as d:
            out = package_release.build(d)
        self.assertEqual((out['repository_zip'], out['skill_zip']), ('Alien-Search.zip', 'Alien-Search-Skill.zip'))


class TokenEfficiency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.r = Records(data_dir=pathlib.Path(self.tmp.name) / 's')

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_listings_are_summaries_small_ones_full(self):
        many = self.r.list()
        self.assertEqual(many['detail'], 'summary')
        self.assertEqual(set(many['data'][0]), {'id', 'title', 'agency', 'url'})
        self.assertIn('--full', many['next_steps'][0])
        full = self.r.list(full=True)
        self.assertNotIn('detail', full)
        self.assertTrue({'aliases', 'note', 'scope'} <= set(full['data'][0]))
        self.assertIn('alternates', full['data'][0])
        few = self.r.list(query='JAL')
        self.assertNotIn('detail', few); self.assertIn('aliases', few['data'][0])

    def test_json_is_compact_for_programs_and_indented_in_terminals(self):
        import io, os, storage
        class Terminal(io.StringIO):
            def isatty(self): return True
        piped, term = io.StringIO(), Terminal()
        storage.print_json({'a': [1, 2]}, piped); storage.print_json({'a': [1, 2]}, term)
        self.assertEqual(piped.getvalue().strip(), '{"a":[1,2]}')
        self.assertIn('\n  "a"', term.getvalue())
        with patch.dict(os.environ, {'ALIEN_SEARCH_PRETTY': '1'}):
            forced = io.StringIO(); storage.print_json({'a': 1}, forced)
        self.assertIn('\n  "a"', forced.getvalue())

    def test_reads_carry_no_constant_prose_and_short_ids(self):
        page = b'<html><title>T</title><a href="/x.pdf">Report</a></html>'
        with patch.object(self.r, 'get_bytes', return_value=(page, 'https://www.archives.gov/research/topics/uaps', {'Content-Type': 'text/html'})):
            out = self.r.read('A001')
        for gone in ('scope', 'filter_scope', 'coverage', 'note'):
            self.assertNotIn(gone, out)
        self.assertEqual(len(out['data'][0]['id']), 12)
        self.assertEqual(len(out['next_steps']), 1)
        self.assertIn('Limits every result shares', GUIDE)

    def test_starter_and_skill_trimmed_without_losing_content(self):
        framework = (ROOT / 'docs' / 'FRAMEWORK.md').read_text(encoding='utf-8')
        skill = (ROOT / '.claude' / 'skills' / 'alien-search' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertNotIn('BEGIN GENERATED', STARTER)
        self.assertNotIn('| ID | Source |', STARTER)
        self.assertIn('- A001, A002 (official-bulk)', STARTER)
        self.assertNotIn('Alien Search is an unofficial research method', STARTER)
        self.assertIn('Alien Search is an unofficial research method', (ROOT / '.claude' / 'skills' / 'alien-search' / 'framework.md').read_text(encoding='utf-8'))
        self.assertIn('Default research timezone: UTC', STARTER)
        self.assertNotIn('## Procedure', skill)
        self.assertNotIn('| Documented administrative fact |', GUIDE)
        self.assertIn('Documented administrative fact', framework)
        self.assertIn('spending', framework)


if __name__ == '__main__':
    unittest.main(verbosity=2)
