import json
import os
import pathlib
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch,MagicMock
from api import Records,make_server
from storage import JsonStore,SQLiteStore,load_or_create_token
from secure_http import target,public_addresses,fetch,PinnedHTTPS,TransportError

class Portability(unittest.TestCase):
    def test_each_install_has_distinct_owner_only_credentials(self):
        with tempfile.TemporaryDirectory() as d:
            a=pathlib.Path(d)/'a';b=pathlib.Path(d)/'b'
            first=load_or_create_token(a);second=load_or_create_token(b)
            self.assertNotEqual(first,second);self.assertEqual(first,load_or_create_token(a))
            self.assertEqual(len(first),64)
            if os.name=='posix': self.assertEqual(a.stat().st_mode&0o777,0o600)
    @unittest.skipUnless(os.name=='posix','POSIX symlink permissions')
    def test_token_symlinks_and_insecure_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            a=pathlib.Path(d)/'a';b=pathlib.Path(d)/'b'
            load_or_create_token(a);b.symlink_to(a)
            with self.assertRaises(OSError):load_or_create_token(b)
            a.chmod(0o644)
            with self.assertRaises(ValueError):load_or_create_token(a)
    def test_json_and_sqlite_roundtrip_and_migration(self):
        with tempfile.TemporaryDirectory() as d:
            a=JsonStore(pathlib.Path(d)/'json');b=SQLiteStore(pathlib.Path(d)/'db/evidence.sqlite3')
            record={'source_id':'A001','finding':'Évidence','unknown':None,'extra':{'x':[1,2]}}
            a.put('CASE_1',record)
            for k,v in a.export()['records'].items(): b.put(k,v)
            self.assertEqual(a.export(),b.export());self.assertEqual(b.get('CASE_1'),record)
            for store in (a,b):
                with self.assertRaises(ValueError):store.put('../escape',record)
    def test_private_state_can_live_outside_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            r=Records(data_dir=pathlib.Path(d)/'state')
            self.assertEqual(r.state,pathlib.Path(d)/'state')
            self.assertEqual(len(r.sources),35)
    def test_https_redirect_guard_rejects_credentials_ports_and_private_hosts(self):
        hosts={'www.archives.gov','web.archive.org'}
        for url in ('http://www.archives.gov/a','https://user:pass@www.archives.gov/a','https://www.archives.gov:444/a','https://localhost/a','https://www.archives.gov.evil.example/a'):
            with self.assertRaises(TransportError):target(url,hosts)
    def test_mixed_private_dns_is_rejected(self):
        with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]):
            with self.assertRaises(TransportError):public_addresses('www.archives.gov')
    def test_connection_uses_validated_address_without_second_dns_lookup(self):
        address=('127.0.0.1',443) # Fake pinned socket; no real outbound connection.
        row=(socket.AF_INET,socket.SOCK_STREAM,6,'',address)
        sock=MagicMock();context=MagicMock()
        with patch('secure_http.public_addresses',return_value=[row]),patch('secure_http.socket.socket',return_value=sock):
            c=PinnedHTTPS('www.archives.gov',context=context);c.connect()
            sock.connect.assert_called_once_with(address)
            context.wrap_socket.assert_called_once_with(sock,server_hostname='www.archives.gov')
    def test_disallowed_redirect_is_not_followed(self):
        connection=MagicMock();response=connection.getresponse.return_value
        response.status=302;response.getheader.return_value='https://localhost/private'
        with patch('secure_http.PinnedHTTPS',return_value=connection) as ctor:
            with self.assertRaises(TransportError):fetch('https://www.archives.gov/test',{'www.archives.gov'},100)
            self.assertEqual(ctor.call_count,1)
    def test_size_and_encoding_bounds(self):
        for headers,body in [({'Content-Length':'101'},b''),({'Content-Encoding':'gzip'},b''),({},b'x'*101)]:
            connection=MagicMock();response=connection.getresponse.return_value
            response.status=200;response.getheader.side_effect=lambda k,default=None:headers.get(k,default)
            response.getheaders.return_value=list(headers.items());response.read.return_value=body
            with patch('secure_http.PinnedHTTPS',return_value=connection):
                with self.assertRaises(TransportError):fetch('https://www.archives.gov/test',{'www.archives.gov'},100)
    def test_mislabeled_pdf_payload_is_quarantined(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            r=Records(data_dir=pathlib.Path(d))
            with patch.object(r,'get_bytes',return_value=(b'not a pdf',r.source('A004')['url'],{'Content-Type':'application/pdf'})):
                self.assertEqual(r.read('A004')['class'],'drift')
    def test_archived_pdf_viewer_is_quarantined(self):
        from unittest.mock import patch
        from wayback_api import replay_url
        with tempfile.TemporaryDirectory() as d:
            r=Records(data_dir=pathlib.Path(d));stamp='20260101000000'
            final=replay_url(r.source('A004')['url'],stamp,True)
            with patch('wayback_api.WaybackClient._get',return_value=(b'<html>viewer</html>',{'Content-Type':'text/html','Memento-Datetime':'Thu, 01 Jan 2026 00:00:00 GMT'},final)):
                self.assertEqual(r.snapshot('A004',stamp)['class'],'drift')
    def test_browser_origin_rejected_even_with_token(self):
        with tempfile.TemporaryDirectory() as d:
            r=Records(data_dir=pathlib.Path(d));token=load_or_create_token(r.runtime/'api-token.txt')
            server=make_server(r,0,token);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                request=urllib.request.Request('http://127.0.0.1:'+str(server.server_port)+'/v1/sources',headers={'Authorization':'Bearer '+token,'Origin':'https://untrusted.example'})
                with self.assertRaises(urllib.error.HTTPError) as err:urllib.request.urlopen(request)
                self.assertEqual(err.exception.code,403)
            finally:server.shutdown();server.server_close();thread.join()

if __name__=='__main__':unittest.main()
