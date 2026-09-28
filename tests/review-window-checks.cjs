const assert=require('node:assert/strict'), fs=require('node:fs'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_PATH || 'playwright-core');
const root=path.resolve(__dirname,'..');
fs.mkdirSync(path.join(root,'evidence'),{recursive:true});
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.TEST_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});try{
 const page=await browser.newPage({viewport:{width:1440,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{const now=1790500000,day=86400;Date.now=()=>now*1000;window.calls=[];window.clock=now;
 const t=(id,status,age,confirmAge=0,starred=false)=>({id,title:id,cwd:'/fixture',project:'测试项目',updatedAt:now+1000,lastConversationAt:now-age*day,archived:false,execution:{state:'idle'},review:{status,revision:0,registeredAt:now,confirmedAt:status==='done'?now-confirmAge*day:null,note:'',starred}});
 window.tasks=[t('pending-old','pending',8,0,true),t('pending-boundary','pending',7),t('pending-recent','pending',1),t('pending-second','pending',2,0,true),t('done-old-confirm','done',.1,8),t('done-recent-dialogue','done',1,6),t('done-old-dialogue-new-confirm','done',10,0,true),t('pending-unknown','pending',100)];window.tasks[7].lastConversationAt=null;
 window.snapshot=()=>({tasks:structuredClone(window.tasks),chats:[],syncedAt:++window.clock,chatSyncedAt:window.clock,chatAccountCheckedAt:window.clock,chatSource:{accountId:'fixture',complete:true}});
 window.openai={theme:'light',callTool:async(name)=>{window.calls.push(name);if(name!=='list_tasks')throw Error('Display controls must not write review states');return {structuredContent:window.snapshot()};}};
 });await page.goto('file://'+root+'/ui/board.html');await page.waitForFunction(()=>document.getElementById('pending-count').textContent==='4');
 const ids=col=>page.locator('#'+col+' .card [data-key$=":action"]').evaluateAll(els=>els.map(e=>e.dataset.key.replace(/:action$/,'')));
 assert.deepEqual(await ids('pending'),['pending-second','pending-recent','pending-boundary','pending-unknown']);
 assert.deepEqual(await ids('done'),['done-old-dialogue-new-confirm','done-recent-dialogue']);
 await page.locator('#all-pending').check();assert.equal((await ids('pending')).length,5);assert.deepEqual(await ids('done'),['done-old-dialogue-new-confirm','done-recent-dialogue']);
 await page.locator('#all-completed').check();assert.deepEqual(await ids('done'),['done-old-dialogue-new-confirm','done-old-confirm','done-recent-dialogue']);
 await page.locator('#all-pending').uncheck();assert.equal((await ids('pending')).length,4);assert.equal((await ids('done')).length,3);
 await page.locator('#focus').selectOption('stale');assert.equal(await page.locator('#all-pending').isChecked(),true);assert.deepEqual(await ids('pending'),['pending-old']);
 await page.locator('#clear-filters').click();assert.equal(await page.locator('#all-pending').isChecked(),false);assert.equal(await page.locator('#all-completed').isChecked(),false);
 // A new real conversation changes visibility/order; editing metadata and confirmation cannot.
 await page.evaluate(()=>{window.tasks[0].lastConversationAt=Date.now()/1000;window.tasks[0].updatedAt=Date.now()/1000+5000});await page.locator('#refresh').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.equal((await ids('pending'))[0],'pending-old');
 assert.deepEqual(await page.evaluate(()=>window.tasks.map(t=>[t.id,t.review.status])),[['pending-old','pending'],['pending-boundary','pending'],['pending-recent','pending'],['pending-second','pending'],['done-old-confirm','done'],['done-recent-dialogue','done'],['done-old-dialogue-new-confirm','done'],['pending-unknown','pending']]);
 await page.evaluate(()=>{window.tasks[0].review.starred=false;window.tasks[3].review.starred=false});await page.locator('#refresh').click();await page.waitForFunction(()=>!document.getElementById('refresh').disabled);assert.deepEqual(await ids('pending'),['pending-old','pending-recent','pending-second','pending-boundary','pending-unknown']);
 await page.screenshot({path:root+'/evidence/review-window-ui.png'});
 await page.locator('#all-pending').check();await page.locator('#all-completed').check();await page.reload();await page.waitForFunction(()=>document.getElementById('pending-count').textContent==='4');assert.equal(await page.locator('#all-pending').isChecked(),false);assert.equal(await page.locator('#all-completed').isChecked(),false);
 assert.equal(await page.locator('#sort').isVisible(),false);assert.deepEqual(errors,[]);
 fs.writeFileSync(root+'/evidence/review-window-ui.json',JSON.stringify({passed:true,defaultSevenDays:true,exactBoundaryIncluded:true,independentShowAll:true,newConversationReorders:true,metadataAndConfirmationDoNotSort:true,starsPinAboveChronology:true,unknownTimeRetained:true,noStatusWrites:true,reopenDefaults:true,errors},null,2));console.log('PASS seven-day pending, independent show-all and real conversation ordering');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exitCode=1});
