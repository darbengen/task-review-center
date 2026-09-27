import concurrent.futures,importlib.util,json,os,tempfile,unittest
from pathlib import Path
spec=importlib.util.spec_from_file_location('task_center',Path(__file__).parents[1]/'scripts/server.py');server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)
class Tests(unittest.TestCase):
 def setUp(self):self.temp=tempfile.TemporaryDirectory();self.store=server.Store(self.temp.name)
 def tearDown(self):self.temp.cleanup()
 def tasks(self):return [server.normalize({'id':'a','name':'任务 A','cwd':'/tmp/demo','updatedAt':1})]
 def test_registration_confirmation_undo_and_restart(self):
  self.store.sync(self.tasks());r=self.store.mark('a','done',0);self.store.sync(self.tasks());self.assertEqual(self.store.snapshot()['tasks'][0]['review']['status'],'done')
  again=server.Store(self.temp.name);self.assertEqual(again.snapshot()['tasks'][0]['review']['status'],'done')
  again.undo('a',r['undo']['token'],r['undo']['expectedRevision']);self.assertEqual(again.snapshot()['tasks'][0]['review']['status'],'pending')
  with self.assertRaises(ValueError):again.mark('a','done',0)
  self.assertEqual(os.stat(again.file).st_mode&0o777,0o600)
  self.assertEqual(json.loads(again.file.with_suffix('.json.bak').read_text())['reviews']['a']['status'],'done')
 def test_two_clients_cannot_overwrite_a_newer_review(self):
  self.store.sync(self.tasks())
  def mark():
   try:server.Store(self.temp.name).mark('a','done',0);return True
   except ValueError:return False
  with concurrent.futures.ThreadPoolExecutor() as pool:results=list(pool.map(lambda _:mark(),range(6)))
  self.assertEqual(sum(results),1)
 def test_corrupt_state_preserved(self):
  self.store.file.write_text('{bad')
  with self.assertRaises(json.JSONDecodeError):self.store.sync(self.tasks())
  self.assertEqual(self.store.file.read_text(),'{bad')
 def test_sources_and_archiving_do_not_remove_review(self):
  self.assertIsNone(server.normalize({'id':'x','threadSource':'mcp_extension_host'}))
  self.assertIsNone(server.normalize({'id':'x','parentThreadId':'y'}))
  self.assertIsNone(server.normalize({'id':'x','source':'subagent'}))
  self.assertIsNone(server.normalize({'id':'x','threadSource':'automation'}))
  self.store.sync(self.tasks());self.store.mark('a','done',0);self.store.sync([])
  self.assertEqual(self.store.read()['reviews']['a']['status'],'done')
  self.store.sync(self.tasks());self.assertEqual(self.store.snapshot()['tasks'][0]['review']['status'],'done')
 def test_host_entrypoint_resource_and_mutation_contract(self):
  service=server.Service(store=self.store,core=object());tools=service.handle('tools/list',{})['tools']
  entry=next(t for t in tools if t['name']=='task_center')
  self.assertEqual(entry['_meta']['openai/ui']['entrypoints'],[{'type':'global'}])
  self.assertEqual(entry['_meta']['ui']['resourceUri'],server.UI_URI)
  ui=service.handle('resources/read',{'uri':server.UI_URI})['contents'][0]
  self.assertEqual(ui['mimeType'],'text/html;profile=mcp-app');self.assertEqual(ui['_meta']['ui']['csp']['connectDomains'],[])
  self.store.sync(self.tasks())
  self.assertEqual(service.handle('initialize',{})['serverInfo']['version'],server.VERSION)
  self.assertEqual(service.handle('tools/call',{'name':'task_center'})['_meta']['taskReviewVersion'],server.VERSION)
  error=service.handle('tools/call',{'name':'set_review','arguments':{'threadId':'a','status':'done','expectedRevision':99}})
  self.assertTrue(error['isError']);self.assertEqual(self.store.snapshot()['tasks'][0]['review']['status'],'pending')
 def test_sync_failure_keeps_previous_snapshot(self):
  class Broken:
   def catalog(self):raise ConnectionError('offline')
  self.store.sync(self.tasks());service=server.Service(self.store,Broken());s=service.listing(True)
  self.assertEqual(len(s['tasks']),1);self.assertEqual(s['syncError'],'offline')
 def test_running_server_keeps_matching_ui_when_source_changes(self):
  from unittest.mock import patch
  service=server.Service(store=self.store,core=object())
  original=service.handle('resources/read',{'uri':server.UI_URI})['contents'][0]['text']
  # A source update must not pair tomorrow's UI with today's in-memory service.
  with patch('pathlib.Path.read_text',return_value='<p>incompatible next UI</p>'):
   actual=service.handle('resources/read',{'uri':server.UI_URI})['contents'][0]['text']
  self.assertEqual(actual,original)
 def test_notes_stars_survive_sync_and_status_changes(self):
  self.store.sync(self.tasks())
  self.store.update('a',0,note='字幕第二处需要修改',starred=True)
  completed=self.store.mark('a','done',1)
  self.store.sync(self.tasks())
  review=self.store.snapshot()['tasks'][0]['review']
  self.assertEqual(review['note'],'字幕第二处需要修改');self.assertTrue(review['starred'])
  self.assertNotIn('undo',review)
  with self.assertRaises(ValueError):self.store.update('a',2,note='x'*2001)
  self.store.update('a',2,note='再次检查')
  with self.assertRaises(ValueError):self.store.undo('a',completed['undo']['token'],2)
 def test_completed_updates_flag_without_automatic_reopening(self):
  self.store.sync(self.tasks());self.store.mark('a','done',0)
  tasks=self.tasks();tasks[0]['updatedAt']=2;self.store.sync(tasks)
  task=self.store.snapshot()['tasks'][0]
  self.assertEqual(task['review']['status'],'done');self.assertTrue(task['changedSinceReview'])
  self.store.mark('a','pending',1);self.store.mark('a','done',2)
  self.assertFalse(self.store.snapshot()['tasks'][0]['changedSinceReview'])
 def test_undo_restores_original_confirmation_time(self):
  self.store.sync(self.tasks());confirmed=self.store.mark('a','done',0)['record']
  action=self.store.mark('a','pending',1)['undo']
  restored=self.store.undo('a',action['token'],2)['record']
  self.assertEqual(restored['confirmedAt'],confirmed['confirmedAt'])
  self.assertEqual(restored['reviewedUpdatedAt'],confirmed['reviewedUpdatedAt'])
  with self.assertRaises(ValueError):self.store.undo('a',action['token'],2)
 def test_slow_catalog_does_not_overwrite_newer_metadata(self):
  a=self.tasks();self.store.sync(a,started_at=20)
  a[0]['title']='new name';self.store.sync(a,started_at=30)
  self.store.sync(self.tasks(),started_at=10)
  self.assertEqual(self.store.snapshot()['tasks'][0]['title'],'new name')
  self.store.sync_failed('old error',20);self.assertIsNone(self.store.snapshot()['syncError'])
 def test_semantically_corrupt_state_is_not_overwritten(self):
  self.store.sync(self.tasks());d=self.store.read();d['reviews']['a']['status']='nonsense'
  self.store.file.write_text(json.dumps(d));before=self.store.file.read_bytes()
  with self.assertRaises(ValueError):self.store.sync(self.tasks())
  self.assertEqual(self.store.file.read_bytes(),before)
 def test_failed_atomic_commit_preserves_original(self):
  from unittest.mock import patch
  self.store.sync(self.tasks());before=self.store.file.read_bytes()
  with patch('store.os.replace',side_effect=OSError('disk failure')):
   with self.assertRaises(OSError):self.store.mark('a','done',0)
  self.assertEqual(self.store.file.read_bytes(),before)
  self.assertFalse(list(self.store.path.glob('*.tmp')))
 def test_cross_instance_sync_error_and_recovery(self):
  self.store.sync(self.tasks());start=__import__('time').time()
  self.store.sync_failed('offline',start)
  self.assertEqual(server.Store(self.temp.name).snapshot()['syncError'],'offline')
  self.store.sync(self.tasks());self.assertIsNone(self.store.snapshot()['syncError'])
 def test_changed_catalog_schema_keeps_cached_tasks_and_reviews(self):
  class FixtureCore(server.Core):
   def connect(self):pass
   def close(self):pass
   def call(self,method,params):return self.response
  self.store.sync(self.tasks());self.store.mark('a','done',0)
  for response in [{},{'data':None},{'data':{}},{'data':[{}]},{'data':[{'id':None}]},{'data':[],'nextCursor':2}]:
   with self.subTest(response=response):
    core=FixtureCore();core.response=response
    snapshot=server.Service(self.store,core).listing(True)
    self.assertEqual(len(snapshot['tasks']),1)
    self.assertEqual(snapshot['tasks'][0]['review']['status'],'done')
    self.assertTrue(snapshot['syncError'])
 def test_valid_empty_catalog_is_distinct_from_invalid_response(self):
  class EmptyCore(server.Core):
   def connect(self):pass
   def close(self):pass
   def call(self,method,params):return {'data':[],'nextCursor':None}
  self.store.sync(self.tasks());self.store.mark('a','done',0)
  result=server.Service(self.store,EmptyCore()).listing(True)
  self.assertEqual(result['tasks'],[]);self.assertIsNone(result['syncError'])
  self.assertEqual(self.store.read()['reviews']['a']['status'],'done')
 def test_review_history_survives_unchanged_background_sync(self):
  self.store.sync(self.tasks());self.store.mark('a','done',0)
  history=self.store.path/'backups/review-history'
  files=list(history.glob('review-*.json'));self.assertEqual(len(files),1)
  before=files[0].read_bytes()
  for _ in range(5):self.store.sync(self.tasks())
  self.assertEqual(files[0].read_bytes(),before)
  self.assertEqual(json.loads(before)['reviews']['a']['status'],'pending')
  self.assertEqual(len(list(history.glob('review-*.json'))),1)
  self.assertEqual(os.stat(files[0]).st_mode&0o777,0o600)
 def test_history_retention_does_not_prune_manual_backups(self):
  self.store.sync(self.tasks());folder=self.store.path/'backups/review-history'
  folder.mkdir(parents=True);manual=folder/'manual.json';manual.write_text('keep')
  for revision in range(35):self.store.update('a',revision,note=str(revision))
  self.assertEqual(len(list(folder.glob('review-*.json'))),30)
  self.assertEqual(manual.read_text(),'keep')
 def test_registration_alone_does_not_fill_review_history(self):
  self.store.sync(self.tasks())
  self.store.sync(self.tasks()+[server.normalize({'id':'b','name':'新任务','updatedAt':2})])
  self.assertEqual(list((self.store.path/'backups/review-history').glob('review-*.json')),[])
  self.assertEqual(self.store.read()['reviews']['b']['status'],'pending')
if __name__=='__main__':unittest.main()
