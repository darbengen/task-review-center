"""Exercise connection recovery with real child processes and isolated sockets."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import server

spec = importlib.util.spec_from_file_location('lifecycle_fixture', Path(__file__).with_name('lifecycle-checks.py'))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

FAKE_CORE = '''#!PYTHON
import json, os, sys
from pathlib import Path
root = Path(os.environ['RECOVERY_FIXTURE'])
with (root/'calls.jsonl').open('a') as f: f.write(json.dumps({'start':os.getpid(),'args':sys.argv[1:]})+'\\n')
for line in sys.stdin:
 req=json.loads(line);method=req['method']
 if 'id' not in req: continue
 with (root/'calls.jsonl').open('a') as f: f.write(json.dumps({'method':method})+'\\n')
 mode=(root/'mode').read_text()
 if mode=='die_always' and method=='thread/list': sys.exit(0)
 if mode=='die_once' and method=='thread/list' and not (root/'died').exists():
  (root/'died').touch();sys.exit(0)
 if mode=='archive_eof' and method=='thread/archive': sys.exit(0)
 if mode=='deny': out={'error':{'code':-32000,'message':'permission denied'}}
 elif method=='initialize': out={'result':{'userAgent':'isolated'}}
 elif method=='thread/read': out={'result':{'thread':{'id':'fixture','name':'sample','updatedAt':1,'status':{'type':'idle'}}}}
 elif method=='thread/list':
  out={'result': {'data': [] if req['params']['archived'] else [{'id':'fixture','name':'sample','updatedAt':1}], 'nextCursor':None}}
  if mode=='invalid': out={'result':{'data':None}}
 else: raise AssertionError(method)
 print(json.dumps(dict(out,id=req['id'])),flush=True)
'''


class CoreRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.temp = tempfile.TemporaryDirectory(prefix='tr-recovery-', dir='/tmp')
  self.addCleanup(self.temp.cleanup)
  self.home = Path(self.temp.name)
  self.sock = self.home/'.codex/app-server-control/app-server-control.sock'
  self.sock.parent.mkdir(parents=True)
  self.bin = self.home/'codex'
  self.bin.write_text(FAKE_CORE.replace('PYTHON', sys.executable, 1));self.bin.chmod(0o700)
  (self.home/'mode').write_text('normal')
  self.addCleanup(patch.stopall)
  patch.dict(os.environ, TASK_REVIEW_CODEX_BIN=str(self.bin), RECOVERY_FIXTURE=str(self.home)).start()
  os.environ.pop('TASK_REVIEW_CORE_SOCKET', None)
  patch.object(server.Path, 'home', return_value=self.home).start()
  self.core = server.Core();self.addCleanup(self.core.close)

 def calls(self):
  path=self.home/'calls.jsonl'
  return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

 def test_missing_default_socket_starts_official_stdio_and_reuses_it(self):
  self.assertEqual(len(self.core.catalog()),1)
  self.assertEqual(len(self.core.catalog()),1)
  starts=[r for r in self.calls() if 'start' in r]
  self.assertEqual(len(starts),1)
  self.assertEqual(starts[0]['args'],['app-server','--listen','stdio://'])
  child=self.core.process;self.core.close()
  self.assertIsNotNone(child.poll());self.assertTrue(child.stdin.closed);self.assertTrue(child.stdout.closed)

 def test_refused_stale_socket_falls_back_without_deleting_socket(self):
  stale=socket.socket(socket.AF_UNIX);stale.bind(str(self.sock));stale.close()
  self.assertEqual(len(self.core.catalog()),1)
  self.assertTrue(self.sock.exists())

 def test_live_socket_keeps_existing_transport_and_recovers_after_host_stops(self):
  host=fixture.FixtureHost(self.sock);host.start()
  try:
   self.assertEqual(self.core.catalog()[0]['id'],'fixture-a')
   self.assertFalse(self.calls())
  finally:host.stop()
  self.assertEqual(self.core.catalog()[0]['id'],'fixture')

 def test_explicit_socket_never_falls_back_into_another_environment(self):
  os.environ['TASK_REVIEW_CORE_SOCKET']=str(self.sock)
  with self.assertRaises(ConnectionError):self.core.catalog()
  self.assertFalse(self.calls())

 def test_child_restart_and_mid_read_eof_recover(self):
  (self.home/'mode').write_text('die_once')
  self.assertEqual(len(self.core.catalog()),1)
  self.assertEqual(len([r for r in self.calls() if 'start' in r]),2)
  child=self.core.process;child.terminate();child.wait(3)
  self.assertEqual(len(self.core.catalog()),1)
  self.assertTrue(child.stdout.closed)

 def test_permission_rejection_is_not_retried(self):
  (self.home/'mode').write_text('deny')
  with self.assertRaisesRegex(RuntimeError,'permission denied'):self.core.catalog()
  self.assertEqual(len([r for r in self.calls() if 'start' in r]),1)

 def test_repeated_transport_failure_is_bounded_and_keeps_reviews(self):
  store=server.Store(self.home/'state');store.sync([server.normalize({'id':'fixture','updatedAt':1})])
  store.mark('fixture','done',0);before=store.read()['reviews']
  (self.home/'mode').write_text('die_always')
  result=server.Service(store,self.core).listing(True)
  self.assertTrue(result['syncError']);self.assertEqual(store.read()['reviews'],before)
  self.assertEqual(len([r for r in self.calls() if 'start' in r]),2)

 def test_rejected_socket_handshake_does_not_switch_transport(self):
  listener=socket.socket(socket.AF_UNIX);listener.bind(str(self.sock));listener.listen()
  def reject():
   conn,_=listener.accept()
   with conn:
    conn.recv(8192);conn.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
  worker=threading.Thread(target=reject,daemon=True);worker.start()
  try:
   with self.assertRaises(RuntimeError):self.core.catalog()
   self.assertFalse(self.calls())
  finally:listener.close();worker.join(2)

 def test_invalid_catalog_is_not_retried_and_cached_review_survives(self):
  store=server.Store(self.home/'state');store.sync([server.normalize({'id':'fixture','name':'sample','updatedAt':1})])
  store.update('fixture',0,note='keep',starred=True);store.mark('fixture','done',1)
  before=store.read()['reviews']
  (self.home/'mode').write_text('invalid')
  result=server.Service(store,self.core).listing(True)
  self.assertTrue(result['syncError']);self.assertEqual(len(result['tasks']),1)
  self.assertEqual(store.read()['reviews'],before)
  self.assertEqual(len([r for r in self.calls() if 'start' in r]),1)

 def test_uncertain_archive_is_never_replayed(self):
  (self.home/'mode').write_text('archive_eof')
  with self.assertRaises(ConnectionError):
   self.core.archive(server.normalize({'id':'fixture','name':'sample','updatedAt':1}))
  self.assertEqual([r['method'] for r in self.calls() if 'method' in r],['initialize','thread/read','thread/archive'])

 def test_unavailable_binary_preserves_cached_list(self):
  os.environ['TASK_REVIEW_CODEX_BIN']=str(self.home/'absent')
  store=server.Store(self.home/'state');store.sync([server.normalize({'id':'fixture','updatedAt':1})])
  result=server.Service(store,self.core).listing(True)
  self.assertTrue(result['syncError']);self.assertEqual(len(result['tasks']),1)


if __name__=='__main__':unittest.main()
