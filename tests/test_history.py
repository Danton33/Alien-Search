import datetime as dt,json,pathlib,threading,unittest,urllib.request,urllib.parse
from unittest.mock import patch
import test_api
from api import Records,Problem,make_server
from wayback_api import APIError,replay_url

class History(unittest.TestCase):
    setUp=test_api.Checks.setUp
    tearDown=test_api.Checks.tearDown
    # Reuse fixture setup only; existing behavior tests run separately in test_api.py.
    def test_all_mime_omits_filter(self):
        with patch('wayback_api.WaybackClient.captures',return_value={'data':[],'complete':True}) as call:
            out=self.r.captures('A001',mimetype='all')
            self.assertTrue(out['ok']);self.assertIsNone(call.call_args.kwargs['mimetype'])
    def test_closest_flags_later_snapshot(self):
        with patch('wayback_api.WaybackClient.closest',return_value={'data':{},'source_as_of':'20260102000000'}):
            self.assertTrue(self.r.closest('A001','20260101000000')['capture_after_requested_time'])
    def test_capture_errors_stay_explicit(self):
        with patch('wayback_api.WaybackClient.closest',side_effect=APIError('blocked','blocked','no')) as call:
            out=self.r.closest('A001','20260101000000')
            self.assertFalse(out['ok']); self.assertEqual(out['class'],'blocked'); self.assertEqual(call.call_count,1)
    def test_archive_cooldown_blocks_followup_operations(self):
        with patch('wayback_api.WaybackClient.closest',side_effect=APIError('rate','rate','after_wait','120')) as call:
            self.r.closest('A001','20260101000000')
            self.assertEqual(self.r.closest('A001','20260102000000')['class'],'rate')
            self.assertEqual(call.call_count,1)
    def test_exact_identity_checks(self):
        url='https://www.archives.gov/test';ts='20260101000000';h={'Memento-Datetime':'Thu, 01 Jan 2026 00:00:00 GMT'}
        self.assertTrue(self.r._capture_identity(url,ts,h,replay_url(url,ts,True)))
        for badh,badurl in [({},replay_url(url,ts,True)),({'Memento-Datetime':'Fri, 02 Jan 2026 00:00:00 GMT'},replay_url(url,ts,True)),(h,replay_url(url,'20260102000000',True)),(h,replay_url('https://www.archives.gov/other',ts,True))]:
            with self.assertRaises(APIError):self.r._capture_identity(url,ts,badh,badurl)
    def test_exact_snapshot_cache_and_limits(self):
        ts='20260101000000';url='https://www.archives.gov/test'
        body=b'<html><head><title>Archive</title></head><body>Historical text<a href="/old.pdf">Old PDF</a></body></html>'
        with patch('wayback_api.WaybackClient._get',return_value=(body,{'Content-Type':'text/html','Memento-Datetime':'Thu, 01 Jan 2026 00:00:00 GMT'},replay_url(url,ts,True))) as fetch:
            out=self.r.snapshot('A001',ts,max_chars=10)
            self.assertTrue(out['ok']);self.assertEqual(out['format'],'html');self.assertTrue(out['text_truncated'])
            self.assertEqual(out['links'],['https://www.archives.gov/old.pdf']);self.assertIsNone(out['publication_date'])
            self.assertTrue(self.r.snapshot('A001',ts)['cached']);self.assertEqual(fetch.call_count,1)
    def test_timestamp_and_order_validation(self):
        for ts in ('2026','../','20260230000000'):
            with self.assertRaises(Problem):self.r.snapshot('A001',ts)
        with self.assertRaises(Problem):self.r.compare_snapshots('A001','20260102000000','20260101000000')
    def test_compare_without_original_byte_hash_does_not_claim_equality(self):
        data={'ok':True,'source_url':'https://www.archives.gov/test','agency':'NARA','format':'html','sha256':None,'replay_url':'https://web.archive.org/x','text_excerpt':'text','text_truncated':False,'text_status':'complete'}
        with patch.object(self.r,'snapshot',side_effect=[data,data]):
            self.assertIsNone(self.r.compare_snapshots('A001','20260101000000','20260102000000')['bytes_identical'])
    def test_compare_retains_capture_provenance_and_caps(self):
        older={'ok':True,'source_url':'https://www.archives.gov/test','agency':'NARA','format':'html','sha256':'a'*64,'replay_url':'https://web.archive.org/old','text_excerpt':'old text\nline','text_truncated':False,'text_status':'complete'}
        newer={**older,'sha256':'b'*64,'replay_url':'https://web.archive.org/new','text_excerpt':'new text\nline'}
        with patch.object(self.r,'snapshot',side_effect=[older,newer]):
            out=self.r.compare_snapshots('A001','20260101000000','20260102000000',20)
            self.assertFalse(out['bytes_identical']);self.assertFalse(out['text_identical']);self.assertTrue(out['diff_truncated']);self.assertEqual(len(out['diff_text']),20)
    def test_compare_without_pdf_extractor_is_not_equality(self):
        data={'ok':True,'source_url':'https://www.geipan.fr/file.pdf','agency':'GEIPAN','format':'pdf','sha256':'a'*64,'replay_url':'https://web.archive.org/x','text_excerpt':'','text_truncated':False,'text_status':'none','text_extraction':'unavailable'}
        with patch.object(self.r,'snapshot',side_effect=[data,data]):
            out=self.r.compare_snapshots('A002','20260101000000','20260102000000')
            self.assertFalse(out['text_comparison_available']);self.assertIsNone(out['text_identical'])
    def test_http_snapshot_and_comparison_routes(self):
        token='history-test-token-longer-than-32-characters';server=make_server(self.r,0,token)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            with patch.object(self.r,'closest',return_value={'ok':True}) as closest,patch.object(self.r,'snapshot',return_value={'ok':True}) as snapshot,patch.object(self.r,'compare_snapshots',return_value={'ok':True}) as compare:
                for path in ['/closest?timestamp=20260101','/snapshots/20260101000000?max_chars=50','/compare?before=20260101000000&after=20260102000000&max_chars=100']:
                    req=urllib.request.Request(base+'/v1/sources/A001'+path,headers={'Authorization':'Bearer '+token})
                    self.assertTrue(json.load(urllib.request.urlopen(req))['ok'])
                closest.assert_called_once_with('A001','20260101');snapshot.assert_called_once_with('A001','20260101000000',50);compare.assert_called_once_with('A001','20260101000000','20260102000000',100)
        finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
