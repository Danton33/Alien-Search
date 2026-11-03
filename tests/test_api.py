import json,pathlib,socket,tempfile,threading,time,unittest,urllib.request,urllib.error
from unittest.mock import patch
from api import Records,Problem,make_server

class Checks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); p=pathlib.Path(self.tmp.name); (p/'specs').mkdir()
        (p/'sources.json').write_text(json.dumps({'sources':[{'id':'A001','url':'https://www.archives.gov/test','agency':'NARA','title':'UAP','country':'United States'},{'id':'A002','url':'https://www.geipan.fr/file.pdf','agency':'GEIPAN','title':'Case','country':'France'}]})); self.r=Records(p)
    def tearDown(self): self.tmp.cleanup()
    def test_registered_search_and_empty(self):
        self.assertEqual(self.r.list(country='France')['data'][0]['id'],'A002')
        self.assertEqual(self.r.list(query='NO_SUCH_REGISTER_ENTRY')['total'],0)
        with self.assertRaises(Problem): self.r.source('https://evil.example')
    def test_url_and_private_address_guards(self):
        for u in ['http://www.archives.gov/test','https://127.0.0.1/x','https://www.archives.gov.evil.example/x','https://user:pw@www.archives.gov/x','https://www.archives.gov:444/x']:
            with self.assertRaises(Problem): self.r.validate_url(u)
        with patch('urllib.request.getproxies',return_value={}), patch('socket.getaddrinfo',return_value=[(2,1,6,'',('127.0.0.1',443))]):
            with self.assertRaises(Problem): self.r.validate_url('https://www.archives.gov/test')
    def test_extract_filter_cache_and_deduplicate(self):
        body=b'<html><title>Records</title><main><a href="/a.pdf">UAP report</a><a href="/a.pdf#page=2">same</a><a href="https://evil.example/a.pdf">foreign</a><script>bad</script></main></html>'
        with patch.object(self.r,'get_bytes',return_value=(body,'https://www.archives.gov/test',{'Content-Type':'text/html'})) as fetch:
            r=self.r.read('A001',kind='pdf',query='uap')
            self.assertEqual(len(r['data']),1); self.assertEqual(r['data'][0]['url'],'https://www.archives.gov/a.pdf')
            self.assertNotIn('bad',r['text_excerpt']); self.assertEqual(len(r['sha256']),64)
            self.assertEqual(self.r.read('A001',query='impossible')['data'],[])
            self.assertEqual(fetch.call_count,1)
            self.assertTrue(self.r.read('A001',max_chars=0)['cached'])
    def test_pdf_redirect_is_not_accepted(self):
        with patch.object(self.r,'get_bytes',return_value=(b'<html><title>Home</title></html>','https://www.geipan.fr/',{'Content-Type':'text/html'})):
            self.assertEqual(self.r.read('A002')['class'],'drift')
    def test_challenge_and_rate_not_retried(self):
        with patch.object(self.r,'get_bytes',return_value=(b'<html><title>Access Denied</title></html>','https://www.archives.gov/test',{'Content-Type':'text/html'})):
            self.assertEqual(self.r.read('A001')['class'],'blocked')
        with patch.object(self.r,'get_bytes',side_effect=Problem('rate','rate','after_wait','120')) as fetch:
            self.r.read('A001',refresh=True)
            self.assertEqual(self.r.read('A001',refresh=True)['class'],'rate')
            self.assertEqual(fetch.call_count,1)
    def test_limits_and_unknown_link(self):
        with self.assertRaises(Problem): self.r.read('A001',limit=251)
        with self.assertRaises(Problem): self.r.read('A001',max_chars=20001)
        with self.assertRaises(Problem): self.r.captures('A001',limit=21)
    def test_local_http_auth_host_and_readonly(self):
        server=make_server(self.r,0,'test-token-longer-than-32-characters'); thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        try:
            self.assertTrue(json.load(urllib.request.urlopen(base+'/health'))['local_only'])
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(base+'/v1/sources')
            self.assertEqual(e.exception.code,401)
            req=urllib.request.Request(base+'/v1/sources?country=France',headers={'Authorization':'Bearer test-token-longer-than-32-characters'})
            self.assertEqual(json.load(urllib.request.urlopen(req))['total'],1)
            req=urllib.request.Request(base+'/v1/sources',headers={'Host':'evil.example','Authorization':'Bearer test-token-longer-than-32-characters'})
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
            self.assertEqual(e.exception.code,400)
            req=urllib.request.Request(base+'/v1/sources',method='POST')
            with self.assertRaises(urllib.error.HTTPError) as e: urllib.request.urlopen(req)
            self.assertEqual(e.exception.code,405)
        finally: server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main(verbosity=2)
