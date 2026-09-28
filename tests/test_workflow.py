import json, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
from test_server import server
DAY=86400

class Workflow(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.store=server.Store(self.temp.name);self.now=time.time()
 def tearDown(self):self.temp.cleanup()
 def task(self, state='idle', age=0, **execution):
  return dict(id='a',title='A',updatedAt=self.now-age*DAY,archived=False, execution=dict(state=state,**execution))
 def sync(self,t,now=None):
  with patch('store.time.time',return_value=self.now if now is None else now): self.store.sync([t],workflow=True)
 def review(self):return self.store.snapshot()['tasks'][0]['review']
 def test_pending_never_completes_from_age(self):
  self.sync(self.task(age=100));self.assertEqual(self.review()['status'],'pending')
  self.store.update('a',self.review()['revision'],note='保留',starred=True)
  self.sync(self.task(age=100),self.now+365*DAY);r=self.review()
  self.assertEqual(r['status'],'pending');self.assertIsNone(r['confirmedAt'])
  self.assertEqual(r['note'],'保留');self.assertTrue(r['starred'])
  self.sync(self.task(age=100),self.now+366*DAY);self.assertEqual(self.review(),r)
 def test_old_auto_completion_restored_once_manual_done_preserved(self):
  self.sync(self.task(age=100));data=self.store.read();r=data['reviews']['a']
  r.update(status='done',confirmedAt=self.now-70*DAY,completionReason='aged_30_days',note='保留',starred=True)
  self.store.write(data);old_revision=r['revision'];self.sync(self.task(age=100))
  restored=self.review();self.assertEqual(restored['status'],'pending');self.assertEqual(restored['revision'],old_revision+1)
  self.assertNotIn('completionReason',restored);self.assertEqual(restored['note'],'保留');self.assertTrue(restored['starred'])
  self.sync(self.task(age=100));self.assertEqual(self.review(),restored)
  with patch('store.time.time',return_value=self.now):self.store.mark('a','done',restored['revision'])
  manual=self.review();self.sync(self.task(age=100),self.now+365*DAY);self.assertEqual(self.review(),manual)
 def test_running_and_unknown_never_age(self):
  for state in ['running','unknown']:
   self.sync(self.task(state,age=100));self.assertEqual(self.review()['status'],'pending')
 def test_done_new_run_then_completed_returns_pending(self):
  self.sync(self.task());self.store.mark('a','done',self.review()['revision'])
  start=self.now+10
  self.sync(self.task('running',startedAt=start,turnId='t'),start)
  r=self.review();self.assertEqual(r['status'],'pending')
  with self.assertRaises(ValueError):self.store.mark('a','done',r['revision'])
  with self.assertRaises(ValueError):self.store.archive('a',r['revision'],self.now,True,lambda t:None)
  self.sync(self.task('idle',startedAt=start,endedAt=start+30),start+30)
  self.assertEqual(self.review()['status'],'pending');self.assertEqual(self.review()['pendingSince'],start+30)
  with patch('store.time.time',return_value=start+35):self.store.mark('a','done',self.review()['revision'])
  self.sync(self.task('idle',startedAt=start,endedAt=start+30),start+40)
  self.assertEqual(self.review()['status'],'done')
 def test_completed_run_between_polls_reopens(self):
  self.sync(self.task());self.store.mark('a','done',self.review()['revision'])
  self.sync(self.task('idle',startedAt=self.now+20,endedAt=self.now+30),self.now+40)
  self.assertEqual(self.review()['status'],'pending')
 def test_crashed_run_gets_fresh_review_window(self):
  self.sync(self.task('running',age=100,startedAt=self.now-100*DAY))
  self.sync(self.task('idle',age=100,interrupted=True,startedAt=self.now-100*DAY))
  self.assertEqual(self.review()['status'],'pending');self.assertEqual(self.review()['pendingSince'],self.now)
 def test_unknown_gap_does_not_lose_running_to_pending_transition(self):
  self.sync(self.task('running',age=100))
  self.sync(self.task('unknown',age=100))
  self.sync(self.task('idle',age=100,interrupted=True))
  self.assertEqual(self.review()['status'],'pending');self.assertEqual(self.review()['pendingSince'],self.now)
 def test_chat_default_store_does_not_age(self):
  self.store.sync([self.task(age=100)]);self.assertEqual(self.review()['status'],'pending')
 def test_manual_review_undo_keeps_pending_after_old_age(self):
  self.sync(self.task(age=100));r=self.review();result=self.store.mark('a','done',r['revision'])
  self.store.undo('a',result['undo']['token'],result['record']['revision'])
  self.sync(self.task(age=100));self.assertEqual(self.review()['status'],'pending')

class Lifecycle(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'rollout-test.jsonl';self.reader=server.ExecutionReader([self.temp.name]);self.task={'rolloutPath':str(self.path)}
 def tearDown(self):self.temp.cleanup()
 def event(self,kind,turn='t'):
  return json.dumps({'type':'event_msg','timestamp':'2026-09-27T04:00:00Z','payload':{'type':kind,'turn_id':turn}}).encode()+b'\n'
 def read(self,owners=True):return self.reader.read(self.task,{str(self.path.resolve())} if owners is True else set() if owners is False else None)
 def test_start_finish_restart_abort(self):
  self.path.write_bytes(self.event('task_started'));self.assertEqual(self.read()['state'],'running')
  with self.path.open('ab') as f:f.write(self.event('task_complete'))
  self.assertEqual(self.read()['state'],'idle')
  with self.path.open('ab') as f:f.write(self.event('task_started','new'))
  self.assertEqual(self.read()['turnId'],'new');self.assertEqual(self.read()['state'],'running')
  with self.path.open('ab') as f:f.write(self.event('turn_aborted','new'))
  self.assertTrue(self.read()['interrupted']);self.assertEqual(self.read()['state'],'idle')
 def test_crash_and_owner_failure(self):
  self.path.write_bytes(self.event('task_started'))
  self.assertTrue(self.read(False)['interrupted']);self.assertEqual(self.read(False)['state'],'idle');self.assertEqual(self.read(None)['state'],'unknown')
 def test_messages_cannot_spoof_lifecycle_and_truncated_line(self):
  self.path.write_bytes(self.event('task_started')+json.dumps({'type':'response_item','payload':{'type':'message','text':self.event('task_complete').decode()}}).encode()+b'\n'+self.event('task_complete')[:-2])
  self.assertEqual(self.read()['state'],'running')
 def test_large_tail_limit_and_cache_invalidation(self):
  self.path.write_bytes(self.event('task_started')+(b'{"type":"other"}\n'*15000))
  self.assertEqual(self.read()['state'],'running')
  self.path.write_bytes(self.event('task_complete'));self.assertEqual(self.read()['state'],'idle')
  self.path.write_bytes(b'x'*(self.reader.LIMIT+1));self.assertEqual(self.read(False)['state'],'unknown')
 def test_live_long_turn_beyond_normal_tail_limit(self):
  self.path.write_bytes(self.event('task_started')+(b'{"type":"other"}\n'*600000))
  self.assertEqual(self.read()['state'],'running')
  self.assertEqual(self.read(False)['state'],'unknown')
 def test_missing_and_disallowed_paths(self):
  self.assertEqual(self.read()['state'],'unknown')
  self.assertEqual(self.reader.read({'rolloutPath':'/etc/passwd'},set())['state'],'unknown')
 def test_no_events_without_owner_is_idle(self):
  self.path.write_bytes(b'{"type":"session_meta"}\n')
  self.assertEqual(self.read(False)['state'],'idle');self.assertEqual(self.read()['state'],'unknown')
if __name__=='__main__':unittest.main()

class Conversation(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'rollout-test.jsonl'
  self.reader=server.ConversationReader([self.temp.name]);self.task={'rolloutPath':str(self.path)}
 def tearDown(self):self.temp.cleanup()
 def event(self,kind,at='2026-09-27T04:00:00Z',**fields):
  return json.dumps({'type':'event_msg','timestamp':at,'payload':{'type':kind,'message':'visible',**fields}}).encode()+b'\n'
 def test_only_real_dialogue_not_background_or_injection(self):
  self.path.write_bytes(self.event('user_message')+self.event('agent_message','2026-09-27T05:00:00Z')+self.event('task_complete','2026-09-27T06:00:00Z')+json.dumps({'type':'response_item','timestamp':'2026-09-27T07:00:00Z','payload':{'role':'user','content':'<environment_context>background</environment_context>'}}).encode()+b'\n'+self.event('agent_reasoning','2026-09-27T08:00:00Z'))
  self.assertEqual(self.reader.read(self.task),server.datetime.datetime.fromisoformat('2026-09-27T05:00:00+00:00').timestamp())
 def test_cache_new_message_truncation_and_invalid_time(self):
  self.path.write_bytes(self.event('user_message'));first=self.reader.read(self.task)
  with self.path.open('ab') as f:f.write(self.event('agent_message','2026-09-27T05:00:00Z')[:-2])
  self.assertEqual(self.reader.read(self.task),first)
  self.path.write_bytes(self.event('user_message','2026-09-27T06:00:00Z')+self.event('agent_message','invalid'))
  self.assertEqual(self.reader.read(self.task),first+7200)
 def test_old_dialogue_beyond_execution_tail_is_still_found(self):
  self.path.write_bytes(self.event('user_message')+b'{"type":"other"}\n'*600000)
  self.assertIsNotNone(self.reader.read(self.task))
 def test_unreadable_missing_and_escaped_spoof_are_unknown(self):
  self.assertIsNone(self.reader.read(self.task));self.assertIsNone(self.reader.read({'rolloutPath':'/etc/passwd'}))
  self.path.write_bytes(json.dumps({'type':'response_item','payload':{'text':self.event('user_message').decode()}}).encode()+b'\n')
  self.assertIsNone(self.reader.read(self.task))

 def test_modern_message_envelopes_and_metadata_injections(self):
  base={'type':'response_item','timestamp':'2026-09-27T05:00:00Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'hello'}],'internal_chat_message_metadata_passthrough':{'create_time':100,'content_item_kinds':['user.text']}}}
  inject={'type':'response_item','timestamp':'2026-09-27T06:00:00Z','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'configuration'}],'internal_chat_message_metadata_passthrough':{'create_time':200,'content_item_kinds':['agents_md.instructions']}}}
  self.path.write_bytes((json.dumps(base)+'\n'+json.dumps(inject)+'\n').encode());self.assertEqual(self.reader.read(self.task),100)
  reply={'type':'event_msg','timestamp':'2026-09-27T07:00:00Z','payload':{'type':'item_completed','completed_at_ms':300000,'item':{'type':'AgentMessage','phase':'final','content':[{'text':'reply'}]}}}
  with self.path.open('ab') as f:f.write((json.dumps(reply)+'\n').encode())
  self.assertEqual(self.reader.read(self.task),300)
