const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_PATH || 'playwright-core');
const root=path.resolve(__dirname,'..'), reports=[];
fs.mkdirSync(path.join(root,'evidence'),{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});
 try {
  for (const viewport of [{width:1440,height:900},{width:1000,height:650},{width:620,height:420}]) {
   const context=await browser.newContext({viewport});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
   await context.addInitScript(()=>{
    const now=Date.now()/1000;
    window.clock=now;
    window.tasks=Array.from({length:120},(_,i)=>({id:'work-'+i,title:['正在整理课程','等待检查的成果','最近确认完成'][Math.floor(i/40)]+' '+i,project:'课程制作',cwd:'/fixture',updatedAt:now-i,lastConversationAt:now-i,archived:false,execution:{state:i<40?'running':'idle'},review:{status:i<80?'pending':'done',revision:0,registeredAt:now,confirmedAt:i>=80?now-100:null,note:'',starred:false}}));
    window.tasks.push({...window.tasks[80],id:'old',title:'较早完成的任务',review:{...window.tasks[80].review,confirmedAt:now-8*86400,completionReason:'aged_30_days'}});
    window.chats=Array.from({length:40},(_,i)=>({...window.tasks[40],id:'chatgpt:a:'+i,title:'聊天任务 '+i,source:'chatgpt',conversationId:String(i)}));
    window.snapshot=()=>({tasks:structuredClone(window.tasks),chats:structuredClone(window.chats),syncedAt:++window.clock,chatSyncedAt:window.clock,chatAccountCheckedAt:window.clock,chatSource:{accountId:'a',complete:true}});
    window.openai={theme:'light',callTool:async name=>{if(name!=='list_tasks') throw Error('Unexpected mutation');return {structuredContent:window.snapshot()};}};
   });
   await page.goto('file://'+root+'/ui/board.html');await page.waitForFunction(()=>document.querySelector('#running-count').textContent==='40');
   const counts=async()=>Promise.all(['running','pending','done','chat'].map(id=>page.locator('#'+id+' .card').count()));
   assert.deepEqual(await counts(),[40,40,40,40]);
   assert.equal(await page.locator('.column:visible').count(),3);
   assert.equal(await page.locator('#chat').isVisible(),false);
   assert.equal(await page.locator('#running .archive-task').count(),0);
   assert.equal(await page.locator('#running [data-key$=":action"]:disabled').count(),40);
   assert.equal(await page.locator('#all-completed').isChecked(),false);
   await page.locator('#all-completed').check();assert.equal(await page.locator('#done .card').count(),41);
   assert.doesNotMatch(await page.locator('#done').textContent(),/30 天自动归类/);
   await page.locator('#all-completed').uncheck();assert.equal(await page.locator('#done .card').count(),40);
   const positions=()=>page.evaluate(()=>Object.fromEntries(['running','pending','done','chat'].map(id=>[id,document.getElementById(id).scrollTop])));
   let workPositions;
   for(const id of ['running','pending','done','chat']){
    if(id==='chat'){workPositions=await positions();await page.locator('#chat-view').click();assert.equal(await page.locator('.column:visible').count(),1);}
    await page.locator('#'+id).evaluate(el=>{const board=el.closest('.board');board.scrollLeft=el.closest('.column').offsetLeft-board.offsetLeft;});const before=await positions();
    await page.mouse.move(5,5);await page.waitForTimeout(300);const box=await page.locator('#'+id).boundingBox();await page.mouse.move(box.x+box.width/2,box.y+Math.min(35,box.height/2));await page.mouse.wheel(0,300);await page.waitForTimeout(500);
    const after=await positions();assert.ok(after[id]>before[id],id+' independently scrolls '+JSON.stringify({viewport,box,before,after}));for(const other of Object.keys(before)) if(other!==id)assert.equal(after[other],before[other]);
   }
   await page.locator('#work-view').click();
   const restored=await positions();for(const id of ['running','pending','done'])assert.equal(restored[id],workPositions[id]);
   assert.equal(await page.evaluate(()=>document.scrollingElement.scrollTop),0);
   assert.ok(await page.evaluate(()=>document.scrollingElement.scrollHeight<=innerHeight+1));
   await page.evaluate(()=>{window.tasks[0].execution.state='idle';window.tasks[0].review.revision++;window.tasks[0].updatedAt=Date.now()/1000;});
   await page.locator('#refresh').click();await page.waitForFunction(()=>document.querySelector('#running-count').textContent==='39');assert.deepEqual(await counts(),[39,41,40,40]);
   // Unknown status is visible, and a delayed snapshot cannot resurrect running state.
   await page.evaluate(()=>{window.tasks[0].execution.state='unknown';});await page.locator('#refresh').click();await page.waitForFunction(()=>document.querySelector('#pending').textContent.includes('运行状态暂不可确认'));
   await page.evaluate(()=>window.dispatchEvent(new CustomEvent('openai:set_globals',{detail:{globals:{toolOutput:{...window.snapshot(),syncedAt:1,tasks:[{...window.tasks[0],execution:{state:'running'}}]}}}})));
   assert.equal(await page.locator('#running-count').textContent(),'39');
   await page.locator('#all-completed').check();await page.reload();await page.waitForFunction(()=>document.querySelector('#running-count').textContent==='40');assert.equal(await page.locator('#all-completed').isChecked(),false);
   // Searches and unsaved note/caret survive view changes and background refresh.
   await page.locator('#search').fill('等待检查的成果 40');
   await page.locator('[data-key="work-40:edit-note"]').click();
   const note=page.locator('[data-key="work-40:note"]');await note.fill('尚未保存的工作备注');await note.evaluate(el=>el.setSelectionRange(2,5));
   await page.locator('#chat-view').click();assert.equal(await page.locator('#search').inputValue(),'');
   assert.equal(await page.locator('#project').isVisible(),false);
   assert.equal(await page.locator('#chat .card').count(),40);
   if(viewport.width===1440)await page.screenshot({path:root+'/evidence/chat-view-dedicated.png'});
   await page.locator('#search').fill('聊天任务 12');await page.locator('#refresh').click();
   await page.waitForFunction(()=>!document.getElementById('refresh').disabled);
   assert.equal(await page.locator('#chat .card').count(),1);
   await page.locator('#work-view').click();assert.equal(await page.locator('#search').inputValue(),'等待检查的成果 40');
   assert.equal(await note.inputValue(),'尚未保存的工作备注');assert.deepEqual(await note.evaluate(el=>[el.selectionStart,el.selectionEnd]),[2,5]);
   await page.locator('#clear-filters').click();
   await page.locator('#chat-view').click();assert.equal(await page.locator('#search').inputValue(),'聊天任务 12');
   await page.reload();await page.waitForFunction(()=>document.querySelector('#running-count').textContent==='40');
   assert.equal(await page.locator('#app').getAttribute('data-view'),'work');assert.equal(await page.locator('.column:visible').count(),3);
   if(viewport.width===1440)await page.screenshot({path:root+'/evidence/chat-view-default-three-columns.png'});
   assert.deepEqual(errors,[]);reports.push({viewport,independentScroll:true,runningToPending:true,sevenDayToggle:true,reopenDefault:true,viewSwitchAndDrafts:true,errors});await context.close();
  }
  fs.writeFileSync(root+'/evidence/chat-view-workflow-browser.json',JSON.stringify({passed:true,scope:'Isolated Chromium with lifecycle fixtures, not official Codex window',reports},null,2));console.log('PASS default three columns and separate Chat, transitions, date filter, scrolling and refresh');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
