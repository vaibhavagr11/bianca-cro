'use strict';
// Exercise source date labels west of UTC so date-only values cannot shift a day.
process.env.TZ='America/Chicago';
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync(path.join(__dirname,'../artifacts/sales-pipeline-updates.html'),'utf8');
const coreScript=html.match(/<script id="bianca-live-core">([\s\S]*?)<\/script>/)[1];
const context=vm.createContext({crypto:{randomUUID:()=> 'new-key'}});
vm.runInContext(coreScript+'\n;globalThis.BiancaLive=BiancaLive;',context);
const core=context.BiancaLive;
const json=value=>JSON.parse(JSON.stringify(value));
const approve=(client,nextStepBullets)=>client.decide('p-1','approve',{nextStepBullets,clientContextBullets:[]});
function snapshot(status='pending'){
  return {proposals:[{id:'p-1',company:'A real company',owner:'NDL',stage:'4. Pilot',country:'ARG',routingVersion:1,nextStepBullets:['Confirm the scope.'],clientContextBullets:[],lineageVersion:1,bulletLineage:{nextStep:[{bulletId:'bullet-scope',text:'Confirm the scope.',sourceIds:['email-scope']}],clientContext:[]},sources:[{id:'email-scope',type:'Email',date:'Nov 2',subject:'Pilot scope',quote:'Please confirm the scope.'}],lastProgress:'Old progress',nextStep:'Old next step',revision:2,status}],rows:[{id:'r-1',company:'A real company',owner:'NDL',stage:'4. Pilot',lastProgress:'Old progress',nextStep:'Old next step',revision:0}],activity:[]};
}
const completed=()=>({proposals:[],rows:[{id:'r-1',company:'A real company',owner:'NDL',stage:'4. Pilot',lastProgress:'Old next step',nextStep:'- Confirm the scope.',revision:1}],activity:[{id:'a-1',proposalId:'p-1',company:'A real company',kind:'approved',edited:false,at:'2026-11-02T20:00:00Z',before:{lastProgress:'Old progress',nextStep:'Old next step'},after:{lastProgress:'Old next step',nextStep:'- Confirm the scope.'}}]});
let passed=0,failed=0;
const tests=[];
function test(name,fn){tests.push({name,fn});}

test('Live page starts without sample company data, browser storage, or hardcoded credentials',()=>{
  assert.equal(html.includes('Pernod Ricard'),false);
  assert.equal(html.includes('localStorage'),false);
  assert.equal(html.includes('sessionStorage'),false);
  assert.equal(html.includes('bianca-core'),false);
  assert.ok(html.includes('<h1>Sales Pipeline Updates</h1>'));
  assert.ok(html.includes('<option value="NDL">Nico · NDL</option>'));
  assert.ok(html.includes('id="pending-count">0'));
  for(const match of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))new Function(match[1]);
});

test('Navigation and cards omit the Sheet preview and rotation hint',()=>{
  assert.equal(/Sheet preview/i.test(html),false);
  assert.equal(html.includes('sheet-tab'),false);
  assert.equal(html.includes('sheet-panel'),false);
  assert.equal(html.includes('sheet-rows'),false);
  assert.equal(html.includes('Current Next step → Last progress'),false);
  assert.equal(html.includes('Approved bullets → Next step'),false);
});

test('MCP structured and text tool envelopes return the actual workspace',()=>{
  assert.deepEqual(json(core.unwrap({structuredContent:snapshot()})),snapshot());
  assert.deepEqual(json(core.unwrap({content:[{type:'text',text:JSON.stringify(snapshot())}]})),snapshot());
  assert.throws(()=>core.unwrap({isError:true,structuredContent:{detail:'Sheet changed; refresh.',status:409}}),error=>error.status===409&&error.message==='Sheet changed; refresh.');
  assert.throws(()=>core.unwrap({content:[{type:'text',text:'bad JSON'}]}),/unreadable response/);
});

test('Connector transport uses the declared tool bridge and rejects missing connectors',async()=>{
  assert.throws(()=>core.connectorTransport(null),/Connect the Bianca Review connector/);
  const calls=[];
  const transport=core.connectorTransport({callTool:async(name,args)=>{calls.push({name,args});return {structuredContent:snapshot()};}});
  await transport(core.TOOLS.workspace,{});
  assert.deepEqual(json(calls),[{name:'bianca_review_workspace',args:{}}]);
});

test('REST transport uses only same-origin routes and places credentials in the header',async()=>{
  const calls=[];
  const fetcher=async(url,options)=>{calls.push({url,options});return {ok:true,status:200,json:async()=>snapshot()};};
  const transport=core.restTransport(fetcher,()=> 'founder-access-secret');
  await transport(core.TOOLS.workspace,{});
  await transport(core.TOOLS.approve,{proposal_id:'id/unsafe',revision:3,idempotency_key:'intent-1',next_step_next_step_bullets:['One action'],client_context_bullets:[],client_context_bullets:[]});
  assert.equal(calls[0].url,'/review/workspace');
  assert.equal(calls[0].options.headers.Authorization,'Bearer founder-access-secret');
  assert.equal(calls[0].options.cache,'no-store');
  assert.equal(calls[0].options.credentials,'omit');
  assert.equal(calls[0].options.redirect,'error');
  assert.equal(calls[1].url,'/review/proposals/id%2Funsafe/approve');
  assert.equal(calls[1].options.body.includes('founder-access-secret'),false);
  assert.deepEqual(JSON.parse(calls[1].options.body),{revision:3,idempotency_key:'intent-1',next_step_next_step_bullets:['One action'],client_context_bullets:[],client_context_bullets:[]});
});

test('REST authentication, conflicts, and uncertain network responses are surfaced',async()=>{
  let called=false;
  await assert.rejects(core.restTransport(async()=>{called=true;},()=> '')(core.TOOLS.workspace,{}),e=>e.status===401);
  assert.equal(called,false);
  await assert.rejects(core.restTransport(async()=>({ok:false,status:409,json:async()=>({detail:'The sheet row changed.'})}),()=> 'token')(core.TOOLS.workspace,{}),e=>e.status===409&&e.message==='The sheet row changed.');
  await assert.rejects(core.restTransport(async()=>{throw Error('private network diagnostic');},()=> 'token')(core.TOOLS.workspace,{}),/Refresh to check the result/);
});

test('Workspace validation accepts every company without the brief four-card cap',()=>{
  const value=snapshot();value.proposals=Array.from({length:12},(_,i)=>({...value.proposals[0],id:'p-'+i}));
  assert.equal(core.workspace(value).proposals.length,12);
  assert.throws(()=>core.workspace({proposals:[],rows:[]}),/incomplete workspace/);
  const bad=snapshot();bad.proposals[0].nextStepBullets=[null];assert.throws(()=>core.workspace(bad),/could not be read/);
});

test('Approval waits for a confirmed write and a fresh shared snapshot before changing UI state',async()=>{
  let resolveWrite,readCount=0;const changes=[];
  const transport=async(name)=>{
    if(name===core.TOOLS.workspace)return ++readCount===1?snapshot():completed();
    return new Promise(resolve=>{resolveWrite=resolve;});
  };
  const client=core.createClient(transport,{onChange:s=>changes.push(json(s)),makeKey:()=> 'intent-1'});
  await client.refresh();const before=JSON.stringify(client.state);
  const save=approve(client,['Confirm the scope.']);
  assert.equal(JSON.stringify(client.state),before);
  assert.equal(client.isBusy('p-1'),true);
  assert.equal(changes.length,1);
  resolveWrite({status:'approved'});await save;
  assert.equal(client.state.proposals.length,0);
  assert.equal(client.state.rows[0].lastProgress,'Old next step');
  assert.equal(changes.length,2);
});

test('An unsuccessful approval keeps the card and reuses its exact idempotency key on retry',async()=>{
  let fail=true;const writes=[];
  const transport=async(name,args)=>{if(name===core.TOOLS.workspace)return snapshot();writes.push(json(args));if(fail){fail=false;throw Error('Network interrupted');}return {status:'approved'};};
  const client=core.createClient(transport,{makeKey:()=> 'stable-key'});await client.refresh();
  await assert.rejects(approve(client,['Edited action.']),/Network interrupted/);
  assert.equal(client.state.proposals[0].id,'p-1');assert.equal(client.state.rows[0].nextStep,'Old next step');
  await approve(client,['Edited action.']);
  assert.deepEqual(writes[0],writes[1]);assert.equal(writes[0].idempotency_key,'stable-key');
});

test('An uncertain request cannot be changed into a different edit without a refresh',async()=>{
  const client=core.createClient(async name=>{if(name===core.TOOLS.workspace)return snapshot();throw Error('Unknown outcome');},{makeKey:()=> 'stable-key'});
  await client.refresh();await assert.rejects(approve(client,['Original edit.']));
  await assert.rejects(approve(client,['Different edit.']),/previous request may still be saving/);
  await assert.rejects(client.decide('p-1','ignore'),/previous request may still be saving/);
});

test('A fresh pending revision allows another review after a rejected old request',async()=>{
  let revision=2,key=0;const writes=[];
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace){const s=snapshot();s.proposals[0].revision=revision;return s;}writes.push(json(args));throw new core.ReviewError('Sheet changed',409);},{makeKey:()=> 'key-'+(++key)});
  await client.refresh();await assert.rejects(approve(client,['First edit.']));revision=3;await client.refresh();
  await assert.rejects(approve(client,['New edit.']),/Sheet changed/);
  assert.equal(writes[1].revision,3);assert.notEqual(writes[0].idempotency_key,writes[1].idempotency_key);
});

test('Applying approvals recover the durable original payload even after a new page load',async()=>{
  const s=snapshot('applying');s.proposals[0].retry={idempotency_key:'durable-key',revision:1,next_step_bullets:['The originally approved edit.'],client_context_bullets:[]};const writes=[];
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return s;writes.push(json(args));return {status:'approved'};});
  await client.refresh();await approve(client,['A replacement edit must not be sent.']);
  assert.deepEqual(writes,[{proposal_id:'p-1',idempotency_key:'durable-key',revision:1,next_step_bullets:['The originally approved edit.'],client_context_bullets:[]}]);
  await assert.rejects(client.decide('p-1','ignore'),/still being confirmed/);
});

test('An applying approval missing its durable retry record cannot cause a new write',async()=>{
  let writes=0;
  const client=core.createClient(async name=>{if(name===core.TOOLS.workspace)return snapshot('applying');writes++;});await client.refresh();
  await assert.rejects(approve(client,['One action']),/still being confirmed/);assert.equal(writes,0);
});

test('Double clicks cannot send concurrent approval requests',async()=>{
  let resolveWrite,writes=0;
  const client=core.createClient(async name=>{if(name===core.TOOLS.workspace)return snapshot();writes++;return new Promise(resolve=>{resolveWrite=resolve;});});await client.refresh();
  const first=approve(client,['Confirm the scope.']);
  await assert.rejects(approve(client,['Confirm the scope.']),/already being saved/);assert.equal(writes,1);
  resolveWrite({status:'approved'});await first;
});

test('Ignore is a server decision with no approval bullets or local sheet changes',async()=>{
  const calls=[];let ignored=false;
  const client=core.createClient(async(name,args)=>{calls.push({name,args:json(args)});if(name===core.TOOLS.workspace){const s=snapshot();if(ignored)s.proposals=[];return s;}ignored=true;return {status:'ignored'};},{makeKey:()=> 'ignore-key'});
  await client.refresh();await client.decide('p-1','ignore');
  assert.equal(client.state.proposals.length,0);assert.equal(client.state.rows[0].lastProgress,'Old progress');
  assert.deepEqual(calls[1],{name:'bianca_ignore_update',args:{proposal_id:'p-1',revision:2,idempotency_key:'ignore-key'}});
});

test('A saved decision with a failed refresh is reported without fabricating a local result',async()=>{
  let reads=0;
  const client=core.createClient(async name=>{if(name===core.TOOLS.workspace){if(++reads>1)throw Error('View unavailable');return snapshot();}return {status:'approved'};});await client.refresh();
  await assert.rejects(approve(client,['Confirm the scope.']),/decision was saved, but the latest view could not load/);
  assert.equal(client.state.proposals.length,1);assert.equal(client.state.rows[0].nextStep,'Old next step');
});

test('Blank edited approvals never reach the connector',async()=>{
  let writes=0;const client=core.createClient(async name=>{if(name===core.TOOLS.workspace)return snapshot();writes++;});await client.refresh();
  await assert.rejects(approve(client,core.parseBullets(' \n - \n • ')),/at least one bullet/);assert.equal(writes,0);
});


test('A late refresh cannot put an old pending card back after a newer shared snapshot',async()=>{
  const reads=[];
  const client=core.createClient(async()=>new Promise(resolve=>reads.push(resolve)));
  const first=client.refresh(),second=client.refresh();
  reads[1](completed());await second;
  reads[0](snapshot());await first;
  assert.equal(client.state.proposals.length,0);
  assert.equal(client.state.rows[0].nextStep,'- Confirm the scope.');
});

function fakePage(connector,pathname='/artifact'){
  class Element {
    constructor(){this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.disabled=false;this.handlers={};}
    addEventListener(name,handler){this.handlers[name]=handler;}
    close(){this.open=false;}showModal(){this.open=true;}focus(){}removeAttribute(){}setAttribute(){}
    querySelectorAll(){return [];}querySelector(){return new Element();}
  }
  const elements=new Map(),events={};
  const get=selector=>{if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);};
  get('#owner-filter').value='all';
  const fakeDocument={querySelector:get,querySelectorAll:()=>[],addEventListener:(name,handler)=>{events[name]=handler;}};
  const fakeWindow={biancaConnector:connector,addEventListener:(name,handler)=>{events['window:'+name]=handler;},scrollTo:()=>{}};
  const page=vm.createContext({document:fakeDocument,window:fakeWindow,location:{pathname,protocol:'https:'},crypto:{randomUUID:()=> 'new-key'},setTimeout:()=>0,clearTimeout:()=>{},fetch:()=>{throw Error('Unexpected direct network call');}});
  for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))vm.runInContext(script[1],page);
  return {get,events};
}

test('The actual live UI starts empty and explains missing connector access',()=>{
  const page=fakePage(undefined);
  assert.equal(page.get('#pending-count').textContent,0);
  assert.ok(page.get('#review-list').innerHTML.includes('Your workspace is ready to connect.'));
  assert.equal(page.get('#review-list').innerHTML.includes('Pernod Ricard'),false);
  assert.equal(page.get('#sign-in-form').hidden,true);
});

test('Company cards display source types and dates without source quotes',async()=>{
  const s=snapshot();
  s.proposals[0].sources=[
    {type:'Meeting notes',date:'2026-09-28',quote:'Hidden notes quotation.'},
    {type:'Email',date:'2026-10-06',quote:'Hidden email quotation.'},
    {type:'Calendar',date:'2026-10-07',quote:'Hidden calendar quotation.'},
    {type:'Email',date:'',quote:'Hidden undated quotation.'},
    {type:'<img src=x>',date:'<script>',quote:'Hidden malicious quotation.'}
  ];
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  for(const label of ['Notes · Sep 28','Email · Oct 6','Calendar · Oct 7','Email · Undated'])assert.ok(card.includes(label),label);
  for(const source of s.proposals[0].sources)assert.equal(card.includes(source.quote),false);
  assert.equal(card.includes('View source context'),false);
  assert.equal(card.includes('<img src=x>'),false);
  assert.equal(card.includes('<script>'),false);
});

test('Current Next steps is a collapsed native dropdown using the latest matching company row',async()=>{
  const s=snapshot();
  s.proposals[0].nextStep='Outdated proposal context';
  s.rows.unshift({...s.rows[0],id:'unrelated',company:'Another company',nextStep:'Another company context'});
  s.rows[1].nextStep='- Current action & details.\n- <img src=x> Follow up.';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  const details=card.match(/<details[^>]*class="current-next-steps"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(details,'Current Next steps uses a native details element.');
  assert.equal(/\sopen(?:[\s=>])/.test(details[0].slice(0,details[0].indexOf('>'))),false);
  assert.ok(details[1].includes('<summary>Current Next steps</summary>'));
  assert.ok(details[1].includes('- Current action &amp; details.\n- &lt;img src=x&gt; Follow up.'));
  assert.equal(card.includes('Outdated proposal context'),false);
  assert.equal(card.includes('Another company context'),false);
  assert.equal(card.includes('<img src=x>'),false);
  assert.ok(/\.current-next-steps-content\{[^}]*white-space:pre-wrap/.test(html),'Current cell line breaks are preserved.');
});

test('An expanded Current Next steps dropdown survives a card rerender and remains read-only',async()=>{
  let calls=0;
  const page=fakePage({callTool:async()=>{calls++;return {structuredContent:snapshot()};}});
  await new Promise(resolve=>setImmediate(resolve));
  const details={open:true,matches:selector=>selector.includes('[data-current-next-steps]'),closest:()=>({dataset:{id:'p-1'}})};
  page.events.toggle({target:details});page.get('#company-search').handlers.input();
  assert.ok(/<details[^>]*class="current-next-steps"[^>]* open>/.test(page.get('#review-list').innerHTML));
  details.open=false;page.events.toggle({target:details});page.get('#company-search').handlers.input();
  assert.equal(/<details[^>]*class="current-next-steps"[^>]* open>/.test(page.get('#review-list').innerHTML),false);
  assert.equal(calls,1,'Opening context must not send an approval or alter Sheet data.');
});

test('An empty current row remains empty and does not revive an older proposal Next step',async()=>{
  const s=snapshot();s.proposals[0].nextStep='Older proposed context';s.rows[0].nextStep='';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.ok(card.includes('No Next steps are currently noted.'));
  assert.equal(card.includes('Older proposed context'),false);
});

test('Edit context matches the dropdown current row, including an empty current cell',async()=>{
  for(const current of ['- Current action & context.\n- Confirm <scope>.','']){
    const s=snapshot();s.proposals[0].nextStep='Older proposal baseline';s.rows[0].nextStep=current;
    s.rows.unshift({...s.rows[0],id:'unrelated',company:'Another company',nextStep:'Unrelated context'});
    const page=fakePage({callTool:async()=>({structuredContent:s})});
    await new Promise(resolve=>setImmediate(resolve));
    const article={dataset:{id:'p-1'}},button={dataset:{action:'edit'},closest:()=>article};
    page.events.click({target:{closest:()=>button}});
    assert.equal(page.get('#edit-dialog').open,true);
    assert.equal(page.get('#previous-next-step').textContent,current||'(empty cell)');
    assert.equal(page.get('#edit-content').value,'- Confirm the scope.','The editor still edits proposed bullets, while the context reflects the current row.');
    assert.equal(page.get('#review-list').innerHTML.includes('Older proposal baseline'),false);
  }
});

test('The hosted page offers in-memory sign-in without calling the service before sign-in',()=>{
  const page=fakePage(undefined,'/review/app');
  assert.equal(page.get('#sign-in-form').hidden,false);
  assert.equal(page.get('#pending-count').textContent,0);
});

test('A response arriving after disconnect cannot repopulate the page with company data',async()=>{
  let resolveRead;
  const page=fakePage({callTool:()=>new Promise(resolve=>{resolveRead=resolve;})});
  page.get('#disconnect').onclick();
  resolveRead({structuredContent:snapshot()});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#pending-count').textContent,0);
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  assert.equal(page.get('#connection-status').textContent,'Connection needed');
});


test('Applying cards show the backend reconciliation message as escaped text',async()=>{
  const s=snapshot('applying');
  s.proposals[0].retry={idempotency_key:'durable-key',revision:2,next_step_bullets:['Confirm the scope.'],client_context_bullets:[]};
  s.proposals[0].error='An operator must reconcile the Sheet before retrying. <img src=x>';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.ok(card.includes('An operator must reconcile the Sheet before retrying. &lt;img src=x&gt;'));
  assert.equal(card.includes('<img src=x>'),false);
});

test('An approval with a stale mirror shows a persistent full-sync notice until snapshot catches up',async()=>{
  let reads=0,confirmed=false;
  const stale=completed();stale.rows=snapshot().rows;stale.activity[0].mirror_refreshed=false;
  const page=fakePage({callTool:async(name)=>{
    if(name===core.TOOLS.workspace){reads++;return {structuredContent:!confirmed?snapshot():reads===2?stale:completed()};}
    confirmed=true;return {structuredContent:{status:'approved',mirror_refreshed:false,activity:completed().activity[0]}};
  }});
  await new Promise(resolve=>setImmediate(resolve));
  const article={dataset:{id:'p-1'}};
  const button={dataset:{action:'approve'},closest:selector=>selector==='[data-id]'?article:null};
  page.events.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#mirror-sync-notice').hidden,false);
  assert.ok(page.get('#mirror-sync-notice').textContent.includes('pipeline snapshot needs a full sync'));
  assert.ok(page.get('#toast-region').innerHTML.includes('Google Sheet updated. Pipeline snapshot needs a full sync.'));
  assert.equal(page.get('#pending-count').textContent,0,'A confirmed decision removes its card even when the pipeline mirror needs a sync.');
  assert.ok(page.get('#activity-list').innerHTML.includes('Confirm the scope.'),'Confirmed approval remains visible in the decision history.');
  await page.get('#refresh-updates').onclick();
  assert.equal(page.get('#mirror-sync-notice').hidden,true);
});

test('A historical failed mirror sync is shown when reopening an unsynced workspace',async()=>{
  const stale=completed();stale.rows=snapshot().rows;stale.activity[0].mirror_refreshed=false;
  const page=fakePage({callTool:async()=>({structuredContent:stale})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#mirror-sync-notice').hidden,false);
  assert.ok(page.get('#mirror-sync-notice').textContent.includes('A real company'));
});

test('A later full Pipeline sync resolves an older mirror warning even after legitimate manual edits',async()=>{
  const synced=completed();synced.activity[0].mirror_refreshed=false;
  synced.rows[0].lastProgress='A later human update';synced.rows[0].nextStep='A newer manually chosen action.';
  synced.rows[0].lastSyncedAt='2026-11-02T21:00:00Z';
  const page=fakePage({callTool:async()=>({structuredContent:synced})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#mirror-sync-notice').hidden,true);
});

test('A newer successful mirror supersedes an older failed approval for the same company',async()=>{
  const s=completed();s.activity[0].mirror_refreshed=true;
  s.activity.push({...s.activity[0],id:'older-approval',at:'2026-11-01T20:00:00Z',mirror_refreshed:false,after:{lastProgress:'Old values',nextStep:'Old action'}});
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#mirror-sync-notice').hidden,true);
});


test('New routing never guesses destination from legacy flat bullet text',async()=>{
  const s=snapshot();s.proposals[0].routingVersion=0;s.proposals[0].bullets=['Customer need: Do not route this as an action.'];
  delete s.proposals[0].nextStepBullets;delete s.proposals[0].clientContextBullets;
  let writes=0;
  const client=core.createClient(async(name)=>{if(name===core.TOOLS.workspace)return s;writes++;});
  await client.refresh();
  assert.deepEqual(json(client.state.proposals[0].nextStepBullets),[]);
  await assert.rejects(client.decide('p-1','approve',{nextStepBullets:['A guessed action'],clientContextBullets:[]}),/rebuild/i);
  await assert.rejects(client.decide('p-1','ignore'),/rebuild/i);
  assert.equal(writes,0);
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.ok(card.includes('Rebuild required'));
  assert.equal(card.includes('data-action="approve"'),false);
  assert.equal(card.includes('Do not route this as an action.'),false);
});

test('A typed proposal with missing groups fails closed rather than flattening old bullets',()=>{
  const s=snapshot();s.proposals[0].bullets=['Legacy'];delete s.proposals[0].clientContextBullets;
  assert.throws(()=>core.workspace(s),/could not be read/);
});

test('Approval sends independently reviewed destination groups and no flat bullets field',async()=>{
  const calls=[];
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return snapshot();calls.push(json(args));return {status:'approved'};},{makeKey:()=> 'grouped-key'});
  await client.refresh();
  await client.decide('p-1','approve',{nextStepBullets:[' Blocker: Data missing. '],clientContextBullets:[' Customer need: Retail reporting. ']});
  assert.deepEqual(calls,[{proposal_id:'p-1',revision:2,idempotency_key:'grouped-key',next_step_bullets:['Blocker: Data missing.'],client_context_bullets:['Customer need: Retail reporting.']}]);
  assert.equal('bullets' in calls[0],false);
});

test('Context-only and Next-Step-only reviews retain their empty opposite group',async()=>{
  for(const update of [{nextStepBullets:[],clientContextBullets:['Objection: Timing.']},{nextStepBullets:['Send the scope.'],clientContextBullets:[]}]){
    const calls=[];
    const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return snapshot();calls.push(json(args));return {status:'approved'};});
    await client.refresh();await client.decide('p-1','approve',update);
    assert.deepEqual(calls[0].next_step_bullets,update.nextStepBullets);
    assert.deepEqual(calls[0].client_context_bullets,update.clientContextBullets);
    assert.equal(client.state.rows[0].nextStep,'Old next step','The client never invents a Sheet rotation without a backend snapshot.');
  }
});

test('Flat, empty, malformed, or oversized groups never reach a write tool',async()=>{
  let writes=0;
  const client=core.createClient(async(name)=>{if(name===core.TOOLS.workspace)return snapshot();writes++;});
  await client.refresh();
  for(const update of [['Flat bullet'],{nextStepBullets:[],clientContextBullets:[]},{nextStepBullets:['A valid action'],clientContextBullets:[null]},{nextStepBullets:['x'.repeat(4001)],clientContextBullets:[]},{nextStepBullets:['A bullet\nwith two lines'],clientContextBullets:[]}]){
    await assert.rejects(client.decide('p-1','approve',update));
  }
  assert.equal(writes,0);
});

test('An uncertain review preserves both groups and rejects a changed Client Context on retry',async()=>{
  const writes=[];let fail=true;
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return snapshot();writes.push(json(args));if(fail){fail=false;throw Error('Unknown outcome');}return {status:'approved'};},{makeKey:()=> 'context-stable-key'});
  await client.refresh();
  const update={nextStepBullets:[],clientContextBullets:['Customer need: Reporting.']};
  await assert.rejects(client.decide('p-1','approve',update));
  await assert.rejects(client.decide('p-1','approve',{nextStepBullets:[],clientContextBullets:['Different context.']}),/previous request may still/);
  await client.decide('p-1','approve',update);
  assert.deepEqual(writes[0],writes[1]);
  assert.equal(writes[1].client_context_bullets[0],'Customer need: Reporting.');
});

test('Applying recovery reuses both durable groups after a new page load',async()=>{
  const s=snapshot('applying');s.proposals[0].retry={idempotency_key:'saved-context-key',revision:1,next_step_bullets:[],client_context_bullets:['Originally approved context.']};
  const writes=[];
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return s;writes.push(json(args));return {status:'approved'};});
  await client.refresh();await client.decide('p-1','approve',{nextStepBullets:['Do not send this new action.'],clientContextBullets:[]});
  assert.deepEqual(writes,[{proposal_id:'p-1',idempotency_key:'saved-context-key',revision:1,next_step_bullets:[],client_context_bullets:['Originally approved context.']}]);
});

test('Cards show separate destination sections and read-only current Client Context',async()=>{
  const s=snapshot();s.proposals[0].clientContextBullets=['Customer need: Retail expansion.'];s.proposals[0].clientContext='Older proposal context';
  s.rows[0].clientContext='- Current context & details.\n- <img src=x> Escaped.';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.ok(card.includes('Next Step · actions and blockers'));
  assert.ok(card.includes('Client Context · needs, signals, and objections'));
  assert.ok(card.includes('<summary>Current Client Context</summary>'));
  assert.ok(card.includes('- Current context &amp; details.\n- &lt;img src=x&gt; Escaped.'));
  assert.equal(card.includes('Older proposal context'),false);
  assert.equal(card.includes('<img src=x>'),false);
});

test('Editor submits two independently editable destination groups, including context-only approval',async()=>{
  const s=snapshot();s.proposals[0].clientContextBullets=['Customer need: Existing suggestion.'];s.proposals[0].bulletLineage.clientContext=[{bulletId:'context-original',text:'Customer need: Existing suggestion.',sourceIds:['email-scope']}];s.rows[0].clientContext='Current Sheet context';
  const writes=[];
  const page=fakePage({callTool:async(name,args)=>{if(name===core.TOOLS.workspace)return {structuredContent:s};writes.push(json(args));return {structuredContent:{status:'approved'}};}});
  await new Promise(resolve=>setImmediate(resolve));
  const article={dataset:{id:'p-1'}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  assert.equal(page.get('#previous-client-context').textContent,'Current Sheet context');
  assert.equal(page.get('#edit-client-context').value,'- Customer need: Existing suggestion.');
  page.get('#edit-content').value='';
  page.get('#edit-client-context').value='- Customer need: Manually edited context.';
  page.get('#edit-form').handlers.submit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].next_step_bullets,[]);
  assert.deepEqual(writes[0].client_context_bullets,['Customer need: Manually edited context.']);
});

test('A blank current Client Context stays blank and has a clear empty state',async()=>{
  const s=snapshot();s.proposals[0].clientContext='Older context';s.rows[0].clientContext='';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(page.get('#review-list').innerHTML.includes('No Client Context is currently noted.'));
  assert.equal(page.get('#review-list').innerHTML.includes('Older context'),false);
});

test('Mirror recovery includes Client Context, not just the action cells',async()=>{
  const s=completed();s.activity[0].mirror_refreshed=false;s.activity[0].after.clientContext='Newly approved context';s.rows[0].clientContext='Old context';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#mirror-sync-notice').hidden,false);
});

test('Context-only activity identifies Client Context without claiming Next Step changed',async()=>{
  const s=completed();s.activity[0].before.clientContext='Old context';s.activity[0].after.clientContext='Old context\n- New context';
  s.activity[0].after.nextStep=s.activity[0].before.nextStep;
  s.activity[0].after.lastProgress=s.activity[0].before.lastProgress;
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const activity=page.get('#activity-list').innerHTML;
  assert.ok(activity.includes('<strong>Client Context</strong>'));
  assert.equal(activity.includes('<strong>Next Step</strong>'),false);
});


test('A configured context replacement policy is stated honestly in the live editor',async()=>{
  const s=snapshot();s.clientContextMode='replace';
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(page.get('#context-help').textContent.includes('replace Client Context'));
});


function tracedWorkspace(){
  const s=snapshot(),p=s.proposals[0];
  p.nextStepBullets=['Review the store codes.','Send the launch checklist.'];
  p.clientContextBullets=['Buying signal: The pilot scope is confirmed.'];
  p.sources=[
    {id:'email-a',type:'Email',date:'2026-11-02',subject:'Revised store list',recordId:'r-email-a',version:'v1'},
    {id:'notes-a',type:'Meeting notes',date:'2026-11-01',subject:'Pilot planning notes',recordId:'r-notes-a',version:'v1'},
    {id:'email-b',type:'Email',date:'2026-11-02',subject:'Launch confirmation',recordId:'r-email-b',version:'v1'}
  ];
  p.bulletLineage={nextStep:[
    {bulletId:'action-codes',text:p.nextStepBullets[0],sourceIds:['email-a']},
    {bulletId:'action-checklist',text:p.nextStepBullets[1],sourceIds:['notes-a','email-b']}
  ],clientContext:[{bulletId:'context-pilot',text:p.clientContextBullets[0],sourceIds:['email-a','email-b']}]};
  return s;
}
function sourceClick(page,companyId,sourceId){
  const article={dataset:{id:companyId}},button={dataset:{action:'source',sourceId},closest:selector=>selector==='[data-id]'?article:null};
  page.events.click({target:{closest:()=>button}});
}
const articleHtml=(page,id='p-1')=>page.get('#review-list').innerHTML.match(new RegExp('<article[^>]*data-id="'+id+'"[\\s\\S]*?<\\/article>'))?.[0]||'';
const highlighted=(card,bulletId)=>new RegExp('<li(?=[^>]*data-bullet-id="'+bulletId+'")(?=[^>]*class="source-highlight")[^>]*>').test(card);

test('Lineage validates exact text and known source identities rather than label/date guesses',()=>{
  const p=tracedWorkspace().proposals[0],data=core.trace(p);
  assert.equal(data.available,true);
  assert.deepEqual(json(data.lineage.nextStep[1].sourceIds),['notes-a','email-b']);
  for(const mutate of [
    value=>delete value.lineageVersion,
    value=>{value.bulletLineage.nextStep[0].text='A different sentence with the same position';},
    value=>{value.bulletLineage.nextStep[0].sourceIds=['unknown-email'];},
    value=>{value.bulletLineage.nextStep.pop();},
    value=>{value.bulletLineage.clientContext[0].bulletId=value.bulletLineage.nextStep[0].bulletId;},
    value=>{value.bulletLineage.nextStep[0].sourceIds=[];},
    value=>{value.sources.push({...value.sources[0],subject:'A conflicting identity'});}
  ]){const value=json(p);mutate(value);assert.equal(core.trace(value).available,false);}
  const duplicate=json(p);duplicate.sources.push({...duplicate.sources[0]});
  assert.equal(core.trace(duplicate).sources.length,3,'One source version has one chip even when repeated in evidence.');
});

test('A source click highlights only its associated bullets across both destinations without a write',async()=>{
  let writes=0;
  const page=fakePage({callTool:async name=>{if(name!==core.TOOLS.workspace)writes++;return {structuredContent:tracedWorkspace()};}});
  await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-a');
  let card=articleHtml(page);
  assert.equal(highlighted(card,'action-codes'),true);
  assert.equal(highlighted(card,'action-checklist'),false);
  assert.equal(highlighted(card,'context-pilot'),true);
  assert.ok(card.includes('data-source-id="email-a" data-source-unavailable="false" aria-pressed="true"'));
  assert.ok(page.get('#announcer').textContent.includes('2 bullets highlighted from Email · Nov 2'));
  sourceClick(page,'p-1','notes-a');card=articleHtml(page);
  assert.equal(highlighted(card,'action-codes'),false);
  assert.equal(highlighted(card,'action-checklist'),true);
  assert.equal(highlighted(card,'context-pilot'),false);
  assert.ok(page.get('#announcer').textContent.includes('1 bullet highlighted from Notes · Nov 1'));
  sourceClick(page,'p-1','notes-a');card=articleHtml(page);
  assert.equal(card.includes('class="source-highlight"'),false);
  assert.ok(page.get('#announcer').textContent.includes('Source highlighting cleared'));
  assert.equal(writes,0);
});

test('Same-date source chips remain distinct native keyboard buttons with descriptive accessible names',async()=>{
  const page=fakePage({callTool:async()=>({structuredContent:tracedWorkspace()})});await new Promise(resolve=>setImmediate(resolve));
  const card=articleHtml(page);
  assert.ok(card.includes('<button type="button" class="source-label" data-action="source" data-source-id="email-a"'));
  assert.ok(card.includes('Highlight bullets from Email · Nov 2 — Revised store list (source 1)'));
  assert.ok(card.includes('Highlight bullets from Email · Nov 2 — Launch confirmation (source 3)'));
  sourceClick(page,'p-1','email-b');
  const selected=articleHtml(page);
  assert.equal(highlighted(selected,'action-codes'),false);
  assert.equal(highlighted(selected,'action-checklist'),true);
  assert.equal(highlighted(selected,'context-pilot'),true);
});

test('Source selection is independent for each company card',async()=>{
  const s=tracedWorkspace(),other=json(s.proposals[0]);other.id='p-2';other.company='A second company';s.proposals.push(other);
  const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-a');
  assert.equal(highlighted(articleHtml(page,'p-1'),'action-codes'),true);
  assert.equal(articleHtml(page,'p-2').includes('class="source-highlight"'),false);
  sourceClick(page,'p-2','notes-a');
  assert.equal(highlighted(articleHtml(page,'p-1'),'action-codes'),true);
  assert.equal(highlighted(articleHtml(page,'p-2'),'action-checklist'),true);
});

test('Refresh preserves a still-present selected source by stable identity, not array position',async()=>{
  const s=tracedWorkspace();let reads=0;
  const page=fakePage({callTool:async()=>{const current=json(s);if(++reads>1)current.proposals[0].sources.reverse();return {structuredContent:current};}});
  await new Promise(resolve=>setImmediate(resolve));sourceClick(page,'p-1','email-b');
  await page.get('#refresh-updates').onclick();
  const card=articleHtml(page);
  assert.equal(highlighted(card,'action-codes'),false);
  assert.equal(highlighted(card,'action-checklist'),true);
  assert.ok(card.includes('data-source-id="email-b" data-source-unavailable="false" aria-pressed="true"'));
});

test('Refresh clears a selected source when the new revision no longer includes it',async()=>{
  let current=tracedWorkspace();
  const page=fakePage({callTool:async()=>({structuredContent:current})});await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-a');
  current=json(current);const p=current.proposals[0];p.revision++;
  p.sources=p.sources.filter(source=>source.id!=='email-a');
  p.bulletLineage.nextStep[0].sourceIds=['email-b'];p.bulletLineage.clientContext[0].sourceIds=['email-b'];
  await page.get('#refresh-updates').onclick();
  assert.equal(articleHtml(page).includes('class="source-highlight"'),false);
  assert.ok(articleHtml(page).includes('Select a source to highlight'));
});

test('Missing or invalid lineage never fabricates source highlights and blocks a new approval',async()=>{
  for(const mutate of [p=>delete p.lineageVersion,p=>{p.bulletLineage.nextStep[0].sourceIds=['invented-source'];}]){
    const s=tracedWorkspace();mutate(s.proposals[0]);let writes=0;
    const transport=async name=>{if(name!==core.TOOLS.workspace)writes++;return s;};
    const client=core.createClient(transport);await client.refresh();
    await assert.rejects(client.decide('p-1','approve',s.proposals[0]),/restore its source links/);
    const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
    sourceClick(page,'p-1','email-a');const card=articleHtml(page);
    assert.ok(card.includes('Source tracing is unavailable'));
    assert.equal(card.includes('class="source-highlight"'),false);
    assert.ok(card.includes('Rebuild required'));
    assert.equal(writes,0);
  }
});

test('Legacy typed applying requests remain retryable while their unavailable lineage stays honest',async()=>{
  const s=snapshot('applying');delete s.proposals[0].lineageVersion;
  s.proposals[0].retry={idempotency_key:'legacy-durable-key',revision:2,next_step_bullets:['The saved manual edit.'],client_context_bullets:[]};
  const calls=[];const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return s;calls.push(json(args));return {status:'approved'};});
  await client.refresh();await client.decide('p-1','approve',{});
  assert.deepEqual(calls,[{proposal_id:'p-1',...s.proposals[0].retry}]);
  const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
  assert.ok(articleHtml(page).includes('data-action="retry"'));
  assert.ok(articleHtml(page).includes('Its original request can still be retried'));
  assert.equal(articleHtml(page).includes('class="source-highlight"'),false);
});

test('A same-length edited applying payload cannot inherit the original bullet source by position',async()=>{
  const s=snapshot('applying');s.proposals[0].retry={idempotency_key:'edited-durable-key',revision:2,next_step_bullets:['A manually changed sentence.'],client_context_bullets:[]};
  const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-scope');
  assert.equal(articleHtml(page).includes('class="source-highlight"'),false);
  assert.ok(articleHtml(page).includes('Source tracing is unavailable'));
});

test('Founder-edited bullets with explicit empty provenance do not claim support from a source',()=>{
  const p=tracedWorkspace().proposals[0];p.bulletLineage.nextStep[0].sourceIds=[];p.bulletLineage.nextStep[0].origin='founder_edit';
  const data=core.trace(p);assert.equal(data.available,true);
  assert.deepEqual(json(data.lineage.nextStep[0].sourceIds),[]);
});

test('An entirely founder-edited saved approval explains why its source chips are unavailable',async()=>{
  const s=snapshot('applying'),p=s.proposals[0];
  p.nextStepBullets=['A founder supplied action.'];
  p.bulletLineage.nextStep=[{bulletId:'founder-edit',text:p.nextStepBullets[0],sourceIds:[],origin:'founder_edit'}];
  p.retry={idempotency_key:'founder-edit-key',revision:2,next_step_bullets:[...p.nextStepBullets],client_context_bullets:[]};
  const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
  const card=articleHtml(page);
  assert.ok(card.includes('These founder-edited bullets have no source attribution.'));
  assert.ok(card.includes('data-source-unavailable="true"'));
  assert.ok(card.includes('data-action="retry"'));
});


(async()=>{for(const t of tests){try{await t.fn();passed++;process.stdout.write('PASS '+t.name+'\n');}catch(error){failed++;process.stdout.write('FAIL '+t.name+'\n  '+error.stack+'\n');}}process.stdout.write(`\n${passed} passed; ${failed} failed.\n`);process.exitCode=failed?1:0;})();
