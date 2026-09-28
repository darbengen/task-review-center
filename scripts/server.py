#!/usr/bin/env python3
"""Task center MCP extension. Official archive API; no direct Codex database writes."""
import base64, contextlib, re, datetime, fcntl, json, os, selectors, shutil, subprocess, sys, threading, time, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import Store, timestamp
from chat_catalog import ChatService

ROOT=Path(__file__).resolve().parents[1]
# Older desktop sessions may retain the original server.py launch command.
# Route new invocations through the published runtime, even with cached arguments.
# Release copies have no runtime/current.json here, so the child cannot recurse.
if __name__=='__main__' and (ROOT/'runtime/current.json').is_file():
 os.execv(sys.executable,[sys.executable,str(ROOT/'scripts/launcher.py'),*sys.argv[1:]])
VERSION=json.loads((ROOT/'.codex-plugin/plugin.json').read_text())['version']
UI_HTML=(ROOT/'ui/board.html').read_text()
STATE_DIR=Path(os.environ.get('TASK_REVIEW_STATE_DIR',str(Path.home()/'Library/Application Support/CodexTaskReview')))
UI_URI='ui://task-review-center/board.html'
MIME='text/html;profile=mcp-app'
SVG='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><rect x="3" y="4" width="7" height="16" rx="2" fill="#d58b42"/><rect x="14" y="4" width="7" height="16" rx="2" fill="#51a779"/></svg>'
ICON={'src':'data:image/svg+xml;base64,'+base64.b64encode(SVG.encode()).decode(),'mimeType':'image/svg+xml','sizes':['any']}

class ExecutionReader:
 """Read only lifecycle envelopes. Auxiliary server status is not desktop status."""
 LIMIT = 8 * 1024 * 1024
 CHUNK = 128 * 1024
 EVENT = re.compile(rb'(?<!\\)"type"\s*:\s*"(?:task_started|task_complete|turn_aborted)"')
 def __init__(self, roots=None):
  self.roots = tuple(Path(p).resolve() for p in (roots or [Path.home()/'.codex/sessions', Path.home()/'.codex/archived_sessions']))
  self.cache = {}; self.lock = threading.Lock()
 def owners(self):
  try:
   result = subprocess.run(['/usr/sbin/lsof','-n','-P','-c','codex','-Fn'], capture_output=True, timeout=5)
   if result.returncode not in (0,1) or (result.returncode == 1 and result.stderr): return None
   return {str(Path(line[1:].decode()).resolve()) for line in result.stdout.splitlines() if line.startswith(b'n/') and b'rollout-' in line and line.endswith(b'.jsonl')}
  except (OSError, subprocess.TimeoutExpired, UnicodeError): return None
 def latest(self, path, live=False):
  limit = max(self.LIMIT, 128 * 1024 * 1024) if live else self.LIMIT
  st = path.stat(); key = (st.st_ino, st.st_size, st.st_mtime_ns, limit)
  cached = self.cache.get(str(path))
  if cached and cached[0] == key: return cached[1]
  event = None
  with path.open('rb') as stream:
   start = st.st_size; scanned = 0; carry = b''; skip_partial_end = True
   while start > 0 and scanned < limit:
    size = min(self.CHUNK, start, limit-scanned); start -= size; scanned += size
    stream.seek(start); data = stream.read(size) + carry
    if skip_partial_end:
     boundary = data.rfind(b'\n')
     if boundary < 0: continue
     data = data[:boundary]; skip_partial_end = False
    lines = data.split(b'\n')
    carry = lines.pop(0) if start else b''
    for line in reversed(lines):
     if not self.EVENT.search(line): continue
     try: obj = json.loads(line)
     except (ValueError, UnicodeError): continue
     if not isinstance(obj,dict): continue
     payload = obj.get('payload')
     if obj.get('type') != 'event_msg' or not isinstance(payload,dict): continue
     kind = payload.get('type')
     if kind not in ('task_started','task_complete','turn_aborted'): continue
     try: at = datetime.datetime.fromisoformat(obj['timestamp'].replace('Z','+00:00')).timestamp()
     except (ValueError,KeyError,TypeError,AttributeError): continue
     if not timestamp(at): continue
     event = {'event':kind,'turnId':payload.get('turn_id') if isinstance(payload.get('turn_id'),str) else '', 'eventAt':at}
     for source,target in [('started_at','startedAt'),('completed_at','endedAt')]:
      if timestamp(payload.get(source)): event[target] = payload[source]
     if kind == 'task_started': event.setdefault('startedAt',at)
     else: event.setdefault('endedAt',at)
     break
    if event: break
  if event is None: event = {'event':'none' if start == 0 else 'unknown'}
  self.cache[str(path)] = (key,event)
  return event
 def read(self, task, owners):
  try:
   if not task.get('rolloutPath'): return {'state':'unknown'}
   path = Path(task['rolloutPath']).resolve()
   if not any(root == path.parent or root in path.parents for root in self.roots): return {'state':'unknown'}
   with self.lock: event = dict(self.latest(path, owners is not None and str(path) in owners))
   kind = event.get('event')
   if kind in ('task_complete','turn_aborted'): state = 'idle'
   elif owners is None or kind == 'unknown': state = 'unknown'
   elif str(path) in owners: state = 'running' if kind == 'task_started' else 'unknown'
   else: state = 'idle'
   return dict(event,state=state,interrupted=(kind == 'turn_aborted' or kind == 'task_started' and state == 'idle'))
  except (OSError, ValueError): return {'state':'unknown'}
 def decorate(self, tasks):
  owners = self.owners()
  return [dict(task,execution=self.read(task,owners)) for task in tasks]


class ConversationReader:
 """Extract only timestamps of actual user/visible assistant message events.

 Metadata edits, tool traffic, run-end events, injected response_item instructions,
 reasoning and token accounting do not count as a conversation. Scan backwards
 in bounded chunks (without an arbitrary history cutoff) and cache unchanged logs.
 No message text is retained in the catalog.
 """
 EVENT = re.compile(rb'(?<!\\)"type"\s*:\s*"(?:user_message|agent_message|item_completed|message)"')
 CHUNK = 128 * 1024
 def __init__(self, roots=None):
  self.roots = tuple(Path(p).resolve() for p in (roots or [Path.home()/'.codex/sessions', Path.home()/'.codex/archived_sessions']))
  self.cache = {}; self.lock = threading.Lock()
 def latest(self, path):
  st = path.stat(); key = (st.st_ino, st.st_size, st.st_mtime_ns)
  cached = self.cache.get(str(path))
  if cached and cached[0] == key: return cached[1]
  at = None
  with path.open('rb') as stream:
   start = st.st_size; carry = b''; skip_partial_end = True
   while start > 0:
    size = min(self.CHUNK, start); start -= size
    stream.seek(start); data = stream.read(size) + carry
    if skip_partial_end:
     boundary = data.rfind(b'\n')
     if boundary < 0: continue
     data = data[:boundary]; skip_partial_end = False
    lines = data.split(b'\n'); carry = lines.pop(0) if start else b''
    for line in reversed(lines):
     if not self.EVENT.search(line): continue
     try: obj = json.loads(line)
     except (ValueError, UnicodeError): continue
     if not isinstance(obj,dict): continue
     payload = obj.get('payload')
     if not isinstance(payload,dict): continue
     kind = payload.get('type'); meta = payload.get('internal_chat_message_metadata_passthrough')
     created = meta.get('create_time') if isinstance(meta,dict) else None
     if obj.get('type') == 'event_msg' and kind in ('user_message','agent_message'):
      if not isinstance(payload.get('message'),str) or not payload['message'].strip(): continue
     elif obj.get('type') == 'event_msg' and kind == 'item_completed':
      item = payload.get('item',{})
      if not isinstance(item,dict) or item.get('type') not in ('UserMessage','AgentMessage') or not item.get('content'): continue
      if item.get('phase') == 'analysis': continue
      completed = payload.get('completed_at_ms')
      if timestamp(completed): created = completed / 1000
     elif obj.get('type') == 'response_item' and kind == 'message':
      role = payload.get('role')
      if role not in ('user','assistant') or payload.get('phase') == 'analysis' or not payload.get('content'): continue
      if role == 'user':
       kinds = meta.get('content_item_kinds') if isinstance(meta,dict) else None
       if isinstance(kinds,list):
        if not any(isinstance(k,str) and k.startswith('user.') for k in kinds): continue
       else:
        texts = [c.get('text','') for c in payload['content'] if isinstance(c,dict)]
        text = '\n'.join(texts).lstrip()
        if text.startswith(('<environment_context>','<recommended_plugins>','# AGENTS.md instructions','<permissions instructions>','<turn_aborted>','<system_reminder>')): continue
     else: continue
     try: candidate = created if timestamp(created) else datetime.datetime.fromisoformat(obj['timestamp'].replace('Z','+00:00')).timestamp()
     except (ValueError,KeyError,TypeError,AttributeError): continue
     if timestamp(candidate): at = candidate; break
    if at is not None: break
  self.cache[str(path)] = (key, at)
  return at
 def read(self, task):
  try:
   if not task.get('rolloutPath'): return None
   path = Path(task['rolloutPath']).resolve()
   if not any(root == path.parent or root in path.parents for root in self.roots): return None
   with self.lock: return self.latest(path)
  except (OSError,ValueError): return None
 def decorate(self, tasks):
  return [dict(task,lastConversationAt=self.read(task)) for task in tasks]

class Core:
 def __init__(self):self.process=None;self.serial=0;self.buffer=b'';self.selector=None;self.mutex=threading.Lock();self.transport=None
 def close(self):
  if self.selector:self.selector.close();self.selector=None
  process=self.process;self.process=None;self.buffer=b'';self.transport=None
  if process:
   # Only the bridge or stdio child we started belongs to this plugin.
   try:
    if process.poll() is None:
     process.terminate()
     try:process.wait(timeout=3)
     except subprocess.TimeoutExpired:process.kill();process.wait()
   finally:
    for stream in (process.stdin,process.stdout):
     if stream:
      with contextlib.suppress(OSError):stream.close()
 def disconnected(self):
  # Bridge exit 78 means a protocol/permission rejection, never an invitation
  # to bypass that rejection using a different transport.
  if self.transport=='socket':
   try:code=self.process.wait(timeout=.5)
   except subprocess.TimeoutExpired:code=None
   if code==78:raise RuntimeError('Codex 连接被拒绝或协议不兼容，请检查连接权限和客户端版本。')
  raise ConnectionError('Codex 核心连接已断开。')
 def call(self,method,params):
  self.serial+=1;rid=self.serial
  try:
   self.process.stdin.write(json.dumps({'id':rid,'method':method,'params':params}).encode()+b'\n');self.process.stdin.flush()
  except BrokenPipeError:self.disconnected()
  deadline=time.monotonic()+25
  while time.monotonic()<deadline:
   while b'\n' in self.buffer:
    line,self.buffer=self.buffer.split(b'\n',1);obj=json.loads(line)
    if obj.get('id')==rid:
     if 'error' in obj:raise RuntimeError(obj['error'].get('message','Codex 接口错误'))
     return obj.get('result')
   if not self.selector.select(max(.01,deadline-time.monotonic())):break
   chunk=os.read(self.process.stdout.fileno(),65536)
   if not chunk:self.disconnected()
   self.buffer+=chunk
  raise TimeoutError('Codex 任务同步超时。')
 def connect(self):
  if self.process and self.process.poll() is None:return
  self.close()
  socket=Path(os.environ.get('TASK_REVIEW_CORE_SOCKET',str(Path.home()/'.codex/app-server-control/app-server-control.sock')))
  try:
   if not socket.exists():raise ConnectionError('指定的 Codex 核心连接暂不可用。')
   self.start([sys.executable,str(ROOT/'scripts/core_bridge.py'),'--socket',str(socket)],'socket')
   return
  except (ConnectionError,TimeoutError):
   self.close()
   # An explicit socket is a configuration boundary (including offline fixtures).
   if 'TASK_REVIEW_CORE_SOCKET' in os.environ:raise
  except Exception:self.close();raise
  configured=os.environ.get('TASK_REVIEW_CODEX_BIN')
  candidates=[configured] if configured is not None else [
   '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',
   '/Applications/Codex.app/Contents/Resources/codex',shutil.which('codex')]
  binary=next((p for p in candidates if p and Path(p).is_file() and os.access(p,os.X_OK)),None)
  if not binary:raise RuntimeError('未找到可用的 Codex 核心程序，已保留原任务列表。')
  try:self.start([binary,'app-server','--listen','stdio://'],'stdio')
  except Exception:self.close();raise
 def start(self,command,transport):
  self.process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
  self.transport=transport
  self.selector=selectors.DefaultSelector();self.selector.register(self.process.stdout,selectors.EVENT_READ)
  self.call('initialize',{'clientInfo':{'name':'task_review_center','title':'任务中心','version':VERSION}})
  self.process.stdin.write(b'{"method":"initialized","params":{}}\n');self.process.stdin.flush()
 def archive(self,task):
  with self.mutex:
   try:
    self.connect()
    result=self.call('thread/read',{'threadId':task['id'],'includeTurns':False})
    raw=result.get('thread') if isinstance(result,dict) else None
    fresh=normalize(raw) if isinstance(raw,dict) else None
    if not fresh or fresh['id']!=task['id']:
     raise ValueError('无法核对该聊天，请刷新后重试。')
    status=raw['status'].get('type') if isinstance(raw.get('status'),dict) else None
    execution=ExecutionReader(); observed=execution.read(fresh,execution.owners())
    if fresh.get('rolloutPath') and observed['state']=='unknown':raise ValueError('无法核对任务运行状态，请稍后再归档。')
    if status=='active' or observed['state']=='running':raise ValueError('该任务仍在运行，请等它结束后再归档。')
    if status not in ('notLoaded','idle','systemError'):
     raise ValueError('无法确认聊天运行状态，请刷新后重试。')
    if fresh['updatedAt']!=task['updatedAt'] or fresh['title']!=task['title']:
     raise ValueError('任务有新进展或名称已变化，请刷新并重新确认归档。')
    result=self.call('thread/archive',{'threadId':task['id']})
    if not isinstance(result,dict):raise ValueError('归档结果尚未确认，请刷新核对后再试。')
   except Exception as e:
    self.close()
    if 'no rollout found' in str(e).lower():
     raise ValueError('该聊天尚未保存或来源已不可用，请刷新列表后再试。') from e
    raise
 def catalog(self):
  with self.mutex:
   # Only a read-only catalog can safely restart from page one after EOF.
   for attempt in range(2):
    try:return self.read_catalog()
    except (ConnectionError,TimeoutError):
     self.close()
     if attempt:raise
    except Exception:self.close();raise
 def read_catalog(self):
  self.connect();tasks={}
  for archived in (False,True):
   cursor=None;visited=set()
   while True:
    p={'limit':100,'sourceKinds':['cli','vscode','appServer'],'modelProviders':[],'sortKey':'updated_at','useStateDbOnly':True,'archived':archived}
    if cursor:p['cursor']=cursor
    result=self.call('thread/list',p)
    if not isinstance(result,dict) or not isinstance(result.get('data'),list):
     raise ValueError('客户端返回的任务列表格式已变化，已保留原列表，请检查版本兼容。')
    cursor=result.get('nextCursor')
    if cursor is not None and (not isinstance(cursor,str) or not cursor):
     raise ValueError('客户端返回的任务分页格式异常，已保留原列表。')
    for raw in result['data']:
     if not isinstance(raw,dict) or not isinstance(raw.get('id'),str) or not raw['id']:
      raise ValueError('客户端返回了无法识别的任务，已保留原列表。')
     t=normalize(raw,archived)
     if t:tasks[t['id']]=t
    if not cursor:break
    if cursor in visited:raise RuntimeError('任务列表分页重复。')
    visited.add(cursor)
  return sorted(tasks.values(),key=lambda t:t['updatedAt'],reverse=True)

def normalize(raw,archived=False):
 id=raw.get('id')
 if not isinstance(id,str) or not id or raw.get('parentThreadId'):return None
 source=str(raw.get('threadSource','')).lower();origin=str(raw.get('source','')).lower()
 if any(x in source or x in origin for x in ['subagent','automation','mcp_extension_host']):return None
 title=raw.get('name') or raw.get('preview') or '新任务'
 if not isinstance(title,str):title='新任务'
 title=title.split('## My request:')[-1].strip().splitlines()
 return {'id':id,'title':(title[0][:160] if title else '新任务'),'cwd':raw.get('cwd') if isinstance(raw.get('cwd'),str) else '','project':Path(raw.get('cwd') if isinstance(raw.get('cwd'),str) and raw['cwd'] else '/').name or '未指定项目','updatedAt':raw.get('updatedAt') if timestamp(raw.get('updatedAt')) else 0,'archived':archived,'rolloutPath':raw.get('path') if isinstance(raw.get('path'),str) else None}

class Service:
 def __init__(self,store=None,core=None,chat=None):self.execution=ExecutionReader();self.conversation=ConversationReader();self.store=store or Store(STATE_DIR);self.core=core or Core();self.chat=chat;self.sync_lock=threading.Lock();self.last_attempt=0;self.sync_error=None;self.stopped=threading.Event()
 def refresh(self,force=False):
  with self.sync_lock:
   if not force and time.monotonic()-self.last_attempt<5:return
   self.last_attempt=time.monotonic();started_at=time.time()
   try:self.store.sync(self.conversation.decorate(self.execution.decorate(self.core.catalog())),started_at,workflow=True);self.sync_error=None
   except Exception as e:
    self.sync_error=str(e)
    try:self.store.sync_failed(self.sync_error,started_at)
    except (ValueError,OSError):pass
   if self.chat:
    try:self.chat.refresh()
    except (ValueError,OSError):pass  # Snapshot still reports its own store/availability error.
 def listing(self,force=False):
  snap=self.store.snapshot()
  if force or any('execution' not in t or 'lastConversationAt' not in t for t in snap['tasks']) or not snap['syncedAt'] or time.time()-snap['syncedAt']>15:self.refresh(force)
  snap=self.store.snapshot()
  if self.chat:
   try:
    chat=self.chat.snapshot()
    # Another (possibly older) Work watcher must not mask a stale/empty Chat cache.
    if not chat['syncedAt'] or time.time()-chat['syncedAt']>15:
     self.chat.refresh();chat=self.chat.snapshot()
    snap.update(chats=chat['tasks'],chatSyncedAt=chat['syncedAt'],chatSyncError=chat['syncError'],chatSource=chat.get('source'),chatAccountCheckedAt=chat['accountCheckedAt'])
   except (ValueError,OSError):snap.update(chats=[],chatSyncedAt=None,chatSyncError='聊天审查记录暂时无法读取，原记录已保留。',chatSource=None,chatAccountCheckedAt=time.time())
  return snap
 def review_store(self,thread_id):
  if isinstance(thread_id,str) and thread_id.startswith('chatgpt:'):
   if not self.chat:raise ValueError('当前聊天栏尚未连接。')
   return self.chat
  return self.store
 def mutate(self,method,args):
  target=self.review_store(args['threadId']);id=args['threadId'];revision=args['expectedRevision']
  if target is self.store and method in ('mark','undo') and isinstance(self.core,Core):
   self.refresh(True)
   if self.sync_error: raise ValueError('无法核对任务运行状态，请同步成功后重试。')
  if method=='mark':values=(args['status'],revision)
  elif method=='undo':values=(args['token'],revision)
  else:values=(revision,args.get('note'),args.get('starred'))
  return target.mutate(method,id,*values) if isinstance(target,ChatService) else getattr(target,method)(id,*values)
 def archive(self,args):
  if isinstance(args.get('threadId'),str) and args['threadId'].startswith('chatgpt:'):
   raise ValueError('Chat 聊天请在原对话中归档；任务中心不会使用 Work 的归档接口。')
  with self.sync_lock:
   return self.store.archive(args['threadId'],args['expectedRevision'],args['expectedUpdatedAt'],args.get('confirmed'),self.core.archive)
 def watch(self):
  # Many chats may load the same plugin; only one elected watcher does periodic reads.
  while not self.stopped.is_set():
   fd=os.open(self.store.path/'watcher.lock',os.O_CREAT|os.O_RDWR,0o600)
   try:
    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:self.stopped.wait(10);continue
    while not self.stopped.is_set():
     self.refresh();self.stopped.wait(30 if self.sync_error else 8)
   finally:os.close(fd)
 def tools(self):
  return [
   {'name':'task_center','title':'任务中心','description':'默认显示运行中、待审查、已完成三栏，可通过聊天任务按钮切换界面；待审查与已完成各自默认最近7天，可显示全部，并按最近对话倒序。','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'icons':[ICON],'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False},'_meta':{'ui':{'resourceUri':UI_URI},'openai/ui':{'entrypoints':[{'type':'global'}],'preferredModelDisplayMode':'fullscreen'},'openai/widgetAccessible':True}},
   {'name':'list_tasks','title':'刷新任务','description':'同步运行状态、登记新任务和最近对话时间；待审查不会因超时自动完成。','inputSchema':{'type':'object','properties':{'refresh':{'type':'boolean'}},'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}},
   {'name':'set_review','title':'确认任务状态','description':'仅按用户点击将任务确认完成或移回待审查，不更改或归档聊天。','inputSchema':{'type':'object','properties':{'threadId':{'type':'string'},'status':{'type':'string','enum':['pending','done']},'expectedRevision':{'type':'integer'}},'required':['threadId','status','expectedRevision'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}},
   {'name':'update_task','title':'保存审查备注与重点','description':'保存用户填写的审查备注或重点标记。','inputSchema':{'type':'object','properties':{'threadId':{'type':'string'},'expectedRevision':{'type':'integer'},'note':{'type':'string','maxLength':2000},'starred':{'type':'boolean'}},'required':['threadId','expectedRevision'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}},
   {'name':'undo_review','title':'撤销本次审查','description':'用服务器签发的撤销编号恢复上一次审查状态。','inputSchema':{'type':'object','properties':{'threadId':{'type':'string'},'expectedRevision':{'type':'integer'},'token':{'type':'string'}},'required':['threadId','expectedRevision','token'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':False,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}},
   {'name':'archive_task','title':'归档待审查任务','description':'仅在用户二次确认后，通过官方接口归档聊天；保留聊天和审查记录，不标记完成。','inputSchema':{'type':'object','properties':{'threadId':{'type':'string'},'expectedRevision':{'type':'integer'},'expectedUpdatedAt':{'type':'number'},'confirmed':{'type':'boolean','const':True}},'required':['threadId','expectedRevision','expectedUpdatedAt','confirmed'],'additionalProperties':False},'annotations':{'readOnlyHint':False,'destructiveHint':True,'idempotentHint':True,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}}
  ]
 def handle(self,method,params):
  if method=='initialize':return {'protocolVersion':params.get('protocolVersion','2025-06-18'),'capabilities':{'tools':{},'resources':{}},'serverInfo':{'name':'task-review-center','title':'任务中心','version':VERSION,'icons':[ICON]}}
  if method=='ping':return {}
  if method=='tools/list':return {'tools':self.tools()}
  if method=='resources/list':return {'resources':[{'uri':UI_URI,'name':'任务中心','mimeType':MIME}]}
  if method=='resources/templates/list':return {'resourceTemplates':[]}
  if method=='resources/read':
   if params.get('uri')!=UI_URI:raise ValueError('Unknown UI resource')
   return {'contents':[{'uri':UI_URI,'mimeType':MIME,'text':UI_HTML,'_meta':{'ui':{'prefersBorder':False,'csp':{'connectDomains':[],'resourceDomains':[]}},'openai/widgetPrefersBorder':False}}]}
  if method=='tools/call':
   name=params.get('name');args=params.get('arguments') or {}
   try:
    if name in ('task_center','list_tasks'):data=self.listing(args.get('refresh',False))
    elif name=='set_review':data=self.mutate('mark',args)
    elif name=='undo_review':data=self.mutate('undo',args)
    elif name=='update_task':data=self.mutate('update',args)
    elif name=='archive_task':data=self.archive(args)
    else:raise ValueError('Unknown tool')
    return {'content':[{'type':'text','text':'任务中心已更新。'}],'structuredContent':data,'_meta':{'taskReviewVersion':VERSION}}
   except Exception as e:return {'isError':True,'content':[{'type':'text','text':str(e)}]}
  raise ValueError('Unsupported request: '+method)

def main():
 service=Service(chat=ChatService(STATE_DIR/'chat'));worker=threading.Thread(target=service.watch,daemon=True);worker.start()
 try:
  for line in sys.stdin:
   try:
    request=json.loads(line)
    if 'id' not in request:continue
    try:response={'jsonrpc':'2.0','id':request['id'],'result':service.handle(request['method'],request.get('params',{}))}
    except Exception as e:response={'jsonrpc':'2.0','id':request['id'],'error':{'code':-32603,'message':str(e)}}
    print(json.dumps(response,ensure_ascii=False),flush=True)
   except json.JSONDecodeError:continue
 finally:
  service.stopped.set();worker.join(timeout=1);service.core.close()
if __name__=='__main__':main()
