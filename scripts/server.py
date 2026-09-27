#!/usr/bin/env python3
"""Task center MCP extension. Official archive API; no direct Codex database writes."""
import base64, contextlib, fcntl, json, os, selectors, subprocess, sys, threading, time, uuid
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

class Core:
 def __init__(self):self.process=None;self.serial=0;self.buffer=b'';self.selector=None;self.mutex=threading.Lock()
 def close(self):
  if self.selector:self.selector.close();self.selector=None
  if self.process:
   self.process.terminate()
   try:self.process.wait(timeout=3)
   except subprocess.TimeoutExpired:self.process.kill();self.process.wait()
  self.process=None
 def call(self,method,params):
  self.serial+=1;rid=self.serial
  self.process.stdin.write(json.dumps({'id':rid,'method':method,'params':params}).encode()+b'\n');self.process.stdin.flush()
  deadline=time.monotonic()+25
  while time.monotonic()<deadline:
   while b'\n' in self.buffer:
    line,self.buffer=self.buffer.split(b'\n',1);obj=json.loads(line)
    if obj.get('id')==rid:
     if 'error' in obj:raise RuntimeError(obj['error'].get('message','Codex 接口错误'))
     return obj.get('result')
   if not self.selector.select(max(.01,deadline-time.monotonic())):break
   chunk=os.read(self.process.stdout.fileno(),65536)
   if not chunk:raise ConnectionError('Codex 核心连接已断开。')
   self.buffer+=chunk
  raise TimeoutError('Codex 任务同步超时。')
 def connect(self):
  if self.process and self.process.poll() is None:return
  self.close();self.buffer=b''
  socket=Path(os.environ.get('TASK_REVIEW_CORE_SOCKET',str(Path.home()/'.codex/app-server-control/app-server-control.sock')))
  if not socket.exists():raise ConnectionError('Codex 核心尚未启动，打开聊天后重试。')
  self.process=subprocess.Popen([sys.executable,str(ROOT/'scripts/core_bridge.py'),'--socket',str(socket)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
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
    if status=='active':raise ValueError('该任务仍在运行，请等它结束后再归档。')
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
   try:
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
   except Exception:self.close();raise

def normalize(raw,archived=False):
 id=raw.get('id')
 if not isinstance(id,str) or not id or raw.get('parentThreadId'):return None
 source=str(raw.get('threadSource','')).lower();origin=str(raw.get('source','')).lower()
 if any(x in source or x in origin for x in ['subagent','automation','mcp_extension_host']):return None
 title=raw.get('name') or raw.get('preview') or '新任务'
 if not isinstance(title,str):title='新任务'
 title=title.split('## My request:')[-1].strip().splitlines()
 return {'id':id,'title':(title[0][:160] if title else '新任务'),'cwd':raw.get('cwd') if isinstance(raw.get('cwd'),str) else '','project':Path(raw.get('cwd') if isinstance(raw.get('cwd'),str) and raw['cwd'] else '/').name or '未指定项目','updatedAt':raw.get('updatedAt') if timestamp(raw.get('updatedAt')) else 0,'archived':archived}

class Service:
 def __init__(self,store=None,core=None,chat=None):self.store=store or Store(STATE_DIR);self.core=core or Core();self.chat=chat;self.sync_lock=threading.Lock();self.last_attempt=0;self.sync_error=None;self.stopped=threading.Event()
 def refresh(self,force=False):
  with self.sync_lock:
   if not force and time.monotonic()-self.last_attempt<5:return
   self.last_attempt=time.monotonic();started_at=time.time()
   try:self.store.sync(self.core.catalog(),started_at);self.sync_error=None
   except Exception as e:
    self.sync_error=str(e)
    try:self.store.sync_failed(self.sync_error,started_at)
    except (ValueError,OSError):pass
   if self.chat:
    try:self.chat.refresh()
    except (ValueError,OSError):pass  # Snapshot still reports its own store/availability error.
 def listing(self,force=False):
  snap=self.store.snapshot()
  if force or not snap['syncedAt'] or time.time()-snap['syncedAt']>15:self.refresh(force)
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
   {'name':'task_center','title':'任务中心','description':'显示应用内的待审查、已完成和 Chat 聊天栏。','inputSchema':{'type':'object','properties':{},'additionalProperties':False},'icons':[ICON],'annotations':{'readOnlyHint':True,'openWorldHint':False},'_meta':{'ui':{'resourceUri':UI_URI},'openai/ui':{'entrypoints':[{'type':'global'}],'preferredModelDisplayMode':'fullscreen'},'openai/widgetAccessible':True}},
   {'name':'list_tasks','title':'刷新任务','description':'同步当前 Codex 用户任务，自动登记新任务。','inputSchema':{'type':'object','properties':{'refresh':{'type':'boolean'}},'additionalProperties':False},'annotations':{'readOnlyHint':True,'openWorldHint':False},'_meta':{'ui':{'visibility':['app']},'openai/widgetAccessible':True}},
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
