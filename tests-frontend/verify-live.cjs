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
  assert.ok(html.includes('<h1 id="workspace-title">Sales Pipeline Updates</h1>'));
  assert.ok(html.includes('<option value="NDL">Nicolás de León · NDL</option>'));
  assert.ok(html.includes('<option value="MG">Matias · MG</option>'));
  assert.ok(html.includes('data-owner="MG"'));
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

test('A saved decision with a failed refresh clears the stale workspace',async()=>{
  let reads=0;
  const client=core.createClient(async name=>{if(name===core.TOOLS.workspace){if(++reads>1)throw Error('View unavailable');return snapshot();}return {status:'approved'};});await client.refresh();
  await assert.rejects(approve(client,['Confirm the scope.']),/decision was saved, but the latest view could not load/);
  assert.deepEqual(json(client.state),{proposals:[],rows:[],activity:[]});
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

function fakePage(connector,pathname='/artifact',{search='',fetcher,sessionFetcher,warRoom=false}={}){
  class Element {
    constructor(){this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.disabled=false;this.handlers={};this.attributes={};}
    addEventListener(name,handler){this.handlers[name]=handler;}
    dispatchEvent(event){this.handlers[event.type]?.(event);}
    replaceChildren(){this.textContent='';this.innerHTML='';}
    close(){this.open=false;}showModal(){this.open=true;}focus(){this.focused=true;}
    removeAttribute(name){delete this.attributes[name];delete this[name];}
    setAttribute(name,value){this.attributes[name]=String(value);if(name==='id')this.id=String(value);}
    getAttribute(name){return this.attributes[name]??null;}
    querySelectorAll(){return [];}querySelector(){return new Element();}
  }
  const elements=new Map(),events={},timers=new Map();let timerId=0;
  const get=selector=>{
    if(selector==='dialog[open]')return get('#edit-dialog').open?get('#edit-dialog'):null;
    if(!elements.has(selector)){
      const element=new Element();elements.set(selector,element);
      if(selector.startsWith('#')){
        const tag=html.match(new RegExp('<[^>]+\\bid="'+selector.slice(1)+'"[^>]*>'))?.[0]||'';
        for(const match of tag.matchAll(/([\w:-]+)="([^"]*)"/g))element.setAttribute(match[1],match[2]);
        element.hidden=/\shidden(?:\s|>)/.test(tag);
      }
    }
    return elements.get(selector);
  };
  get('#owner-filter').value='all';
  const queryAll=selector=>selector==='#workspace-tabs [role=tab]'?['#pipeline-workspace-tab','#daily-brief-tab'].map(get):selector==='#pipeline-tabs [role=tab]'?['#review-tab','#activity-tab'].map(get):[];
  const fakeDocument={querySelector:get,querySelectorAll:queryAll,addEventListener:(name,handler)=>{
    const previous=events[name];
    events[name]=previous?event=>{previous(event);handler(event);}:handler;
  }};
  if(warRoom)fakeDocument.body={dataset:{theme:'war-room'}};
  const location={pathname,protocol:'https:',hostname:'hub.test',search,href:'https://hub.test'+pathname+search};
  const fakeWindow={biancaConnector:connector,addEventListener:(name,handler)=>{events['window:'+name]=handler;},scrollTo:()=>{},history:{replaceState:(_state,_unused,url)=>{const next=new URL(url);location.href=next.href;location.search=next.search;}}};
  const page=vm.createContext({document:fakeDocument,window:fakeWindow,location,URL,URLSearchParams,crypto:{randomUUID:()=> 'new-key'},setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},clearTimeout:id=>timers.delete(id),Event,fetch:(url,options)=>url==='/auth/session'?(sessionFetcher||(()=>Promise.resolve({ok:true,status:200,json:async()=>({mode:'token',authenticated:false})})))(url,options):(fetcher||(()=>{throw Error('Unexpected direct network call');}))(url,options)});
  for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))vm.runInContext(script[1],page);
  return {get,events,context:page,timers};
}

test('The actual live UI starts empty and explains missing connector access',()=>{
  const page=fakePage(undefined);
  assert.equal(page.get('#pending-count').textContent,0);
  assert.ok(page.get('#review-list').innerHTML.includes('Your workspace is ready to connect.'));
  assert.equal(page.get('#review-list').innerHTML.includes('Pernod Ricard'),false);
  assert.equal(page.get('#sign-in-form').hidden,true);
  assert.equal(page.get('#pipeline-sheet-link').hidden,true);
  assert.equal(page.get('#pipeline-sheet-link').href,undefined);
});

test('Supporting details stay collapsed, escaped, and outside both editing and approval',async()=>{
  const s=snapshot(),p=s.proposals[0],writes=[];
  p.supportingDetails={nextStep:['Customer review is pending.','<img src=x onerror=alert(1)> Access check.'],clientContext:['Routine usage update.',null,'']};
  const page=fakePage({callTool:async(name,args)=>{
    if(name===core.TOOLS.workspace)return {structuredContent:s};
    writes.push(json(args));return {structuredContent:{status:'approved'}};
  }});
  await new Promise(resolve=>setImmediate(resolve));
  let card=page.get('#review-list').innerHTML;
  const details=card.match(/<details class="current-next-steps supporting-details"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(details);
  assert.equal(/\sopen(?:[\s=>])/.test(details[0].slice(0,details[0].indexOf('>'))),false);
  assert.ok(details[1].includes('Other recorded details (3)'));
  assert.ok(details[1].includes('For reference; not included in this approval.'));
  assert.ok(details[1].includes('&lt;img src=x onerror=alert(1)&gt; Access check.'));
  assert.equal(card.includes('<img src=x'),false);
  assert.equal(details[1].includes('data-destination='),false,'Supporting text is not an approvable bullet.');
  const article={dataset:{id:'p-1'}};
  page.events.toggle({target:{open:true,dataset:{supportingDetails:''},matches:selector=>selector.includes('[data-supporting-details]'),closest:()=>article}});
  page.get('#company-search').handlers.input();
  card=page.get('#review-list').innerHTML;
  assert.ok(/<details class="current-next-steps supporting-details"[^>]* open>/.test(card));
  assert.equal(writes.length,0,'Opening a disclosure never makes a write.');
  page.events.click({target:{closest:()=>({dataset:{action:'edit'},closest:()=>article})}});
  assert.equal(page.get('#edit-content').value,'- Confirm the scope.');
  assert.equal(page.get('#edit-client-context').value,'');
  page.get('#edit-dialog').close();
  page.events.click({target:{closest:()=>({dataset:{action:'approve'},closest:()=>article})}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].next_step_bullets,['Confirm the scope.']);
  assert.deepEqual(writes[0].client_context_bullets,[]);
  assert.equal(JSON.stringify(writes[0]).includes('Customer review'),false);
  assert.equal('supportingDetails' in writes[0],false);
});

test('Every selected priority renders even above three, with no empty supporting disclosure',async()=>{
  const s=snapshot(),p=s.proposals[0];
  p.nextStepBullets=Array.from({length:5},(_,i)=>`Important action ${i+1}.`);
  p.clientContextBullets=Array.from({length:4},(_,i)=>`Decisive context ${i+1}.`);
  for(const [destination,field] of [['nextStep','nextStepBullets'],['clientContext','clientContextBullets']]){
    p.bulletLineage[destination]=p[field].map((text,i)=>({bulletId:destination+'-'+i,text,sourceIds:['email-scope']}));
  }
  p.supportingDetails={nextStep:[],clientContext:[]};
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  for(const text of [...p.nextStepBullets,...p.clientContextBullets])assert.ok(card.includes('>'+text+'</li>'),text);
  assert.equal(card.includes('Other recorded details'),false);
});

test('Header and cards follow the connected Pipeline Sheet and clear invalid or stale links',async()=>{
  const stageUrl='https://docs.google.com/spreadsheets/d/staging-sheet/edit?gid=0#gid=0';
  const productionUrl='https://docs.google.com/spreadsheets/d/production-sheet/edit?gid=42#gid=42';
  let current={...snapshot(),sheetUrl:stageUrl};
  const page=fakePage({callTool:async()=>({structuredContent:current})});
  await new Promise(resolve=>setImmediate(resolve));
  const link=page.get('#pipeline-sheet-link');
  assert.equal(link.hidden,false);
  assert.equal(link.href,stageUrl);
  assert.ok(page.get('#review-list').innerHTML.includes('href="'+stageUrl+'"'));

  current={...snapshot(),sheetUrl:productionUrl};
  await page.get('#refresh-updates').onclick();
  assert.equal(link.href,productionUrl);
  assert.ok(page.get('#review-list').innerHTML.includes('href="'+productionUrl+'"'));
  assert.equal(page.get('#review-list').innerHTML.includes(stageUrl),false);

  for(const sheetUrl of ['https://example.com/redirect','javascript:alert(1)',productionUrl+'\n',null]){
    current={...snapshot(),sheetUrl};
    await page.get('#refresh-updates').onclick();
    assert.equal(link.hidden,true);
    assert.equal(link.href,undefined);
    assert.equal(page.get('#review-list').innerHTML.includes('Open in Pipeline Sheet'),false);
  }
});

test('Company cards show each source with a short, escaped excerpt of at most 15 words',async()=>{
  const s=snapshot();
  const long='one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen';
  s.proposals[0].sources=[
    {id:'email-scope',type:'Meeting notes',date:'2026-09-28',subject:'Pilot notes',quote:'Notes quotation.'},
    {id:'email-review',type:'Email',date:'2026-10-06',subject:'Review request',quote:long,quotes:[long,'Second passage.']},
    {id:'calendar-kickoff',type:'Calendar',date:'2026-10-07',subject:'<script>Calendar',quote:'<img src=x> Calendar quotation.'},
    {id:'undated-email',type:'Email',date:'',subject:'Additional update',quote:''}
  ];
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.ok(card.includes('<details><summary><span>Pilot notes</span><span class="src">Source: Notes, Sep 28</span></summary><div class="ctx"><p>&ldquo;Notes quotation.&rdquo;</p></div></details>'));
  assert.ok(card.includes('<span>Review request</span><span class="src">Source: Email, Oct 6</span></summary><div class="ctx"><p>&ldquo;one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen ...&rdquo;</p><p>&ldquo;Second passage.&rdquo;</p></div>'));
  assert.equal(card.includes('sixteen'),false,'An excerpt stops at 15 words.');
  assert.ok(card.includes('<span>&lt;script&gt;Calendar</span><span class="src">Source: Calendar, Oct 7</span></summary><div class="ctx"><p>&ldquo;&lt;img src=x&gt; Calendar quotation.&rdquo;</p></div>'));
  assert.equal(card.includes('<img src=x>'),false);
  // A source with no passage is a plain line, never an empty dropdown.
  assert.ok(card.includes('<li class="plain"><span>Additional update</span>'));
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
  assert.ok(details[1].includes('<summary>Current Sheet cells<span class="cells-hint">Next step, Last progress and Client Context as they are now</span></summary>'));
  assert.ok(details[1].includes('<h4>Next step (now)</h4>'));
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

test('The pending card, editor and approval use the same loaded deadline',async()=>{
  const body='Next step: The coordinator should keep following up with Morgan, since the team is waiting for a response.';
  const formatted=body+' (complete by Nov 21, 2026)';
  for(const flow of ['card','editor','clear-date']){
    const s=snapshot(),p=s.proposals[0];
    p.nextStepBullets=['Next step: The coordinator should keep following up with Morgan by Nov 21, since the team is waiting for a response.'];
    p.bulletLineage.nextStep[0].text=p.nextStepBullets[0];
    p.reviewNextStepBullets=[formatted];
    const writes=[];
    const page=fakePage({callTool:async(name,args)=>{
      if(name===core.TOOLS.workspace)return {structuredContent:s};
      writes.push(json(args));
      return {structuredContent:{status:'approved'}};
    }});
    await new Promise(resolve=>setImmediate(resolve));
    const card=page.get('#review-list').innerHTML;
    assert.ok(card.includes(`<li data-bullet-id="bullet-scope">${formatted}</li>`));
    assert.equal(card.includes(p.nextStepBullets[0]),false);
    assert.equal(writes.length,0);
    const article={dataset:{id:p.id}},button={dataset:{action:flow==='card'?'approve':'edit'},closest:()=>article};
    page.events.click({target:{closest:()=>button}});
    if(flow!=='card'){
      const editor=page.get('#ns-rows').innerHTML;
      assert.ok(editor.includes('placeholder="dd/mm/yyyy" value="21/11/2026"'));
      assert.ok(editor.includes(`>${body}</textarea>`));
      assert.equal(editor.includes('(complete by'),false);
      assert.ok(page.get('#ns-preview').innerHTML.includes(formatted));
      if(flow==='clear-date'){
        const row={dataset:{row:'0'}};
        page.events.input({target:{closest:()=>row,classList:{contains:key=>key==='ns-date'},value:''}});
      }
      page.get('#edit-form').handlers.submit({preventDefault(){}});
    }
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(writes.length,1);
    assert.deepEqual(writes[0].next_step_bullets,[flow==='clear-date'?body:formatted]);
  }
});

test('Keep + add sends only the new steps with the append mode and leaves the current Sheet bullets to the server',async()=>{
  const s=snapshot(),p=s.proposals[0],formatted='Confirm the scope. (complete by Nov 21, 2026)';
  s.rows[0].nextStep='- Received the previous proposal on Nov 8.';
  p.nextStepBullets=['Confirm the scope by Nov 21.','Confirm the scope on Nov 21.'];
  p.reviewNextStepBullets=[formatted,formatted];
  p.bulletLineage.nextStep=p.nextStepBullets.map((text,i)=>({bulletId:'scope-'+i,text,sourceIds:['email-scope']}));
  const writes=[];
  const page=fakePage({callTool:async(name,args)=>{
    if(name===core.TOOLS.workspace)return {structuredContent:s};
    writes.push(json(args));
    return {structuredContent:{status:'approved'}};
  }});
  await new Promise(resolve=>setImmediate(resolve));
  page.events.click({target:{closest:selector=>selector.includes('[data-mode-pick]')?{dataset:{modePick:'append',modeFor:p.id}}:null}});
  const article={dataset:{id:p.id}},button={dataset:{action:'approve'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);
  // H-16 (11/17/2026): the page used to re-send the current Sheet bullets; the server now appends, so only new steps go.
  assert.deepEqual(writes[0].next_step_bullets,[formatted]);
  assert.equal(writes[0].next_step_mode,'append');
  assert.ok(page.get('#review-list').innerHTML.includes('Last progress does not change.'));
});

test('Replace stays the default and sends no mode field, so an older service accepts it',async()=>{
  const s=snapshot(),writes=[];
  const page=fakePage({callTool:async(name,args)=>{if(name===core.TOOLS.workspace)return {structuredContent:s};writes.push(json(args));return {structuredContent:{status:'approved'}};}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(page.get('#review-list').innerHTML.includes('The old Next Step moves to Last progress.'));
  const article={dataset:{id:'p-1'}},button={dataset:{action:'approve'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(writes,[{proposal_id:'p-1',revision:2,idempotency_key:'new-key',next_step_bullets:['Confirm the scope.'],client_context_bullets:[]}]);
});

test('The write mode is part of the request: validated, kept on an uncertain retry and replayed from a saved approval',async()=>{
  assert.throws(()=>core.groups({nextStepBullets:['A'],clientContextBullets:[],nextStepMode:'merge'}),/replace Next Step or add/);
  assert.deepEqual(json(core.groups({nextStepBullets:['A'],clientContextBullets:[],nextStepMode:'append'})),{nextStepBullets:['A'],clientContextBullets:[],nextStepMode:'append'});
  // Nothing to add means nothing to append: a context-only approval never carries the mode.
  assert.deepEqual(json(core.groups({nextStepBullets:[],clientContextBullets:['C'],nextStepMode:'append'})),{nextStepBullets:[],clientContextBullets:['C']});
  assert.deepEqual(json(core.groups({nextStepBullets:['A'],clientContextBullets:[],nextStepMode:'replace'})),{nextStepBullets:['A'],clientContextBullets:[]});
  const s=snapshot(),writes=[];let fail=true;
  const client=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return s;writes.push(json(args));if(fail){fail=false;throw new core.ReviewError('Sheet update not confirmed.',503);}return {status:'approved'};});
  await client.refresh();
  const update={nextStepBullets:['Send scope.'],clientContextBullets:[],nextStepMode:'append'};
  await assert.rejects(client.decide('p-1','approve',update),/not confirmed/);
  // The same founder decision cannot silently become a replace while its first request is unresolved.
  await assert.rejects(client.decide('p-1','approve',{...update,nextStepMode:'replace'}),/may still be saving/);
  await client.decide('p-1','approve',update);
  assert.equal(writes.length,2);
  assert.deepEqual(writes[0],writes[1]);
  assert.equal(writes[0].next_step_mode,'append');
  const saved=snapshot('applying');saved.proposals[0].retry={idempotency_key:'durable-key',revision:1,next_step_bullets:['Send scope.'],client_context_bullets:[],next_step_mode:'append'};
  const replayed=[];
  const later=core.createClient(async(name,args)=>{if(name===core.TOOLS.workspace)return saved;replayed.push(json(args));return {status:'approved'};});
  await later.refresh();await later.decide('p-1','approve',undefined);
  assert.deepEqual(replayed,[{proposal_id:'p-1',idempotency_key:'durable-key',revision:1,next_step_bullets:['Send scope.'],client_context_bullets:[],next_step_mode:'append'}]);
  saved.proposals[0].retry.next_step_mode='merge';
  await assert.rejects(core.createClient(async()=>saved).refresh(),/administrator to check/);
});

test('The REST route carries the append mode in the approve body',async()=>{
  const calls=[];
  const transport=core.restTransport(async(url,options)=>{calls.push({url,options});return {ok:true,status:200,json:async()=>({status:'approved'})};},()=> 'founder-access-secret');
  await transport(core.TOOLS.approve,{proposal_id:'p-1',revision:3,idempotency_key:'intent-1',next_step_bullets:['One action'],client_context_bullets:[],next_step_mode:'append'});
  assert.equal(calls[0].url,'/review/proposals/p-1/approve');
  assert.deepEqual(JSON.parse(calls[0].options.body),{revision:3,idempotency_key:'intent-1',next_step_bullets:['One action'],client_context_bullets:[],next_step_mode:'append'});
});

test('The editor says what each mode will do in the Sheet and Save & approve sends the chosen mode',async()=>{
  assert.equal(html.includes('Either way the previous Next Step is copied to Last progress'),false);
  const s=snapshot(),p=s.proposals[0],writes=[];
  s.rows[0].nextStep='* confirm the  SCOPE.\n-pusheado 2-nov';
  p.nextStepBullets=['Confirm the scope.','Send the proposal.'];
  p.bulletLineage.nextStep=p.nextStepBullets.map((text,i)=>({bulletId:'scope-'+i,text,sourceIds:['email-scope']}));
  const page=fakePage({callTool:async(name,args)=>{if(name===core.TOOLS.workspace)return {structuredContent:s};writes.push(json(args));return {structuredContent:{status:'approved'}};}});
  await new Promise(resolve=>setImmediate(resolve));
  const article={dataset:{id:p.id}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  assert.equal(page.get('#rotation-last-progress').textContent,'the current Next Step (when Next Step bullets are approved)');
  assert.ok(page.get('#rotation-explain').textContent.includes('the previous Next Step replaces Last progress'));
  assert.ok(page.get('#ns-preview').innerHTML.startsWith('<b>Written to Next Step</b>- Confirm the scope.'));
  // The switch inside the editor has no card id of its own; it applies to the card being edited.
  page.events.click({target:{closest:selector=>selector.includes('[data-mode-pick]')?{dataset:{modePick:'append'}}:null}});
  assert.equal(page.get('#rotation-last-name').textContent,'Last progress');
  assert.ok(page.get('#rotation-last-progress').textContent.startsWith('stays as it is'));
  assert.ok(page.get('#rotation-explain').textContent.includes('Last progress does not change.'));
  const preview=page.get('#ns-preview').innerHTML;
  assert.ok(preview.startsWith('<b>Added to Next Step, below what is already in the Sheet</b>- Send the proposal.'));
  assert.equal(preview.includes('- Confirm the scope.'),false);
  assert.ok(preview.includes('1 step is already in the cell and will not be repeated.'));
  page.get('#edit-form').handlers.submit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].next_step_bullets,['Confirm the scope.','Send the proposal.']);
  assert.equal(writes[0].next_step_mode,'append');
});

test('Keep + add with no steps left is a context-only approval without a mode',async()=>{
  const s=snapshot();s.proposals[0].clientContextBullets=['Customer need: Reporting.'];s.proposals[0].bulletLineage.clientContext=[{bulletId:'context-original',text:'Customer need: Reporting.',sourceIds:['email-scope']}];
  const writes=[];
  const page=fakePage({callTool:async(name,args)=>{if(name===core.TOOLS.workspace)return {structuredContent:s};writes.push(json(args));return {structuredContent:{status:'approved'}};}});
  await new Promise(resolve=>setImmediate(resolve));
  page.events.click({target:{closest:selector=>selector.includes('[data-mode-pick]')?{dataset:{modePick:'append',modeFor:'p-1'}}:null}});
  const article={dataset:{id:'p-1'}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  page.get('#edit-content').value='';
  page.get('#edit-form').handlers.submit({preventDefault(){}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(writes.length,1);
  assert.deepEqual(writes[0].next_step_bullets,[]);
  assert.equal('next_step_mode' in writes[0],false);
});

test('An invalid completion date in an older suggestion does not prefill the field',async()=>{
  const s=snapshot(),p=s.proposals[0];
  p.nextStepBullets=['Confirm the scope. (complete by Feb 30, 2026)','Send the proposal. — due 2026-02-31'];
  p.bulletLineage.nextStep=p.nextStepBullets.map((text,i)=>({bulletId:'scope-'+i,text,sourceIds:['email-scope']}));
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const article={dataset:{id:p.id}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  assert.equal([...page.get('#ns-rows').innerHTML.matchAll(/type="text" class="ns-date"[^>]*placeholder="dd\/mm\/yyyy" value=""/g)].length,2);
});

test('The hosted page offers token sign-in only after checking the configured authentication mode',async()=>{
  const page=fakePage(undefined,'/review/app');
  assert.equal(page.get('#sign-in-form').hidden,true);
  await new Promise(resolve=>setImmediate(resolve));
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
  assert.ok(page.get('#toast-region').innerHTML.includes('Written to the Pipeline Sheet. Pipeline snapshot needs a full sync.'));
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

test('A company removed from the sales workspace is also removed from its cached sync notice',async()=>{
  const stale=completed();stale.rows=snapshot().rows;stale.activity[0].mirror_refreshed=false;
  let current=stale;
  const page=fakePage({callTool:async()=>({structuredContent:current})});
  await new Promise(resolve=>setImmediate(resolve));
  assert.ok(page.get('#mirror-sync-notice').textContent.includes('A real company'));
  current={proposals:[],rows:[],activity:[]};
  await page.get('#refresh-updates').onclick();
  assert.equal(page.get('#mirror-sync-notice').textContent,'');
  assert.equal(page.get('#activity-list').innerHTML.includes('A real company'),false);
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
  assert.ok(card.includes('Suggested next steps'));
  assert.ok(card.includes('data-destination="nextStep"'));
  assert.ok(card.includes('data-destination="clientContext"'));
  assert.ok(card.includes('<summary>Current Sheet cells<span class="cells-hint">Next step, Last progress and Client Context as they are now</span></summary>'));
  assert.ok(card.includes('<h4>Client context (now)</h4>'));
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
  assert.equal(core.trace(duplicate).sources.length,3,'A repeated source version has one source identity.');
});

test('The source-selection handler highlights only associated bullets without a write',async()=>{
  let writes=0;
  const page=fakePage({callTool:async name=>{if(name!==core.TOOLS.workspace)writes++;return {structuredContent:tracedWorkspace()};}});
  await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-a');
  let card=articleHtml(page);
  assert.equal(highlighted(card,'action-codes'),true);
  assert.equal(highlighted(card,'action-checklist'),false);
  assert.equal(highlighted(card,'context-pilot'),true);
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

test('Same-date sources with different subjects remain distinct lines',async()=>{
  const page=fakePage({callTool:async()=>({structuredContent:tracedWorkspace()})});await new Promise(resolve=>setImmediate(resolve));
  const card=articleHtml(page);
  assert.ok(card.includes('<li class="plain"><span>Revised store list</span><span class="src">Source: Email, Nov 2</span></li>'));
  assert.ok(card.includes('<li class="plain"><span>Launch confirmation</span><span class="src">Source: Email, Nov 2</span></li>'));
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
  assert.equal(highlighted(card,'context-pilot'),true);
});

test('Refresh clears a selected source when the new revision no longer includes it',async()=>{
  let current=tracedWorkspace();
  const page=fakePage({callTool:async()=>({structuredContent:current})});await new Promise(resolve=>setImmediate(resolve));
  sourceClick(page,'p-1','email-a');
  current=json(current);const p=current.proposals[0];p.revision++;
  p.sources=p.sources.filter(source=>source.id!=='email-a');
  p.bulletLineage.nextStep[0].sourceIds=['email-b'];p.bulletLineage.clientContext[0].sourceIds=['email-b'];
  await page.get('#refresh-updates').onclick();
  const card=articleHtml(page);
  assert.equal(card.includes('class="source-highlight"'),false);
  assert.equal(card.includes('Revised store list'),false);
  assert.ok(card.includes('Launch confirmation'));
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

test('An entirely founder-edited saved approval explains its missing attribution and remains retryable',async()=>{
  const s=snapshot('applying'),p=s.proposals[0];
  p.nextStepBullets=['A founder supplied action.'];
  p.bulletLineage.nextStep=[{bulletId:'founder-edit',text:p.nextStepBullets[0],sourceIds:[],origin:'founder_edit'}];
  p.retry={idempotency_key:'founder-edit-key',revision:2,next_step_bullets:[...p.nextStepBullets],client_context_bullets:[]};
  const page=fakePage({callTool:async()=>({structuredContent:s})});await new Promise(resolve=>setImmediate(resolve));
  const card=articleHtml(page);
  assert.ok(card.includes('These founder-edited bullets have no source attribution.'));
  assert.equal(card.includes('class="source-highlight"'),false);
  assert.ok(card.includes('data-action="retry"'));
});

test('The complete-by field reads dates day first and explains an impossible date',()=>{
  const page=fakePage(undefined);
  const day=(text)=>json(vm.runInContext(`BiancaWarRoom.parseDay(${JSON.stringify(text)},new Date(2026,10,16))`,page.context));
  for(const text of ['16/11/2026','16-11-2026','16.11.26','16/11','16 Nov','16 nov 2026','16 de noviembre de 2026','Nov 16, 2026','2026-11-16'])assert.deepEqual(day(text),{iso:'2026-11-16'},text);
  assert.deepEqual(day('05/11/2026'),{iso:'2026-11-05'});
  assert.deepEqual(day('1 dic'),{iso:'2026-12-01'});
  assert.deepEqual(day('3/2'),{iso:'2027-02-03'});
  assert.deepEqual(day(''),{iso:''});
  assert.equal(day('31/11/2026').error,'November 2026 has 30 days, so 31/11 is not a date.');
  assert.equal(day('29/2/2027').error,'February 2027 has 28 days, so 29/2 is not a date.');
  assert.equal(day('11/16/2026').error,'There is no month 16. Dates here are day/month/year, for example 16/11/2026.');
  assert.equal(day('next week').error,'Use day/month/year, for example 16/11/2026.');
  assert.equal(day('16 Smarch').error,'"Smarch" is not a month. Use day/month/year, for example 16/11/2026.');
});

test('An impossible date blocks Save and approve before any request is made',async()=>{
  const calls=[],s=snapshot();
  const page=fakePage({callTool:async(name,args)=>{calls.push(name);return {structuredContent:s};}});
  await new Promise(resolve=>setImmediate(resolve));
  const article={dataset:{id:s.proposals[0].id}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  const row={dataset:{row:'0'}};
  page.events.input({target:{closest:()=>row,classList:{contains:key=>key==='ns-date'},value:'31/11/2026'}});
  let prevented=false,stopped=false;
  page.events.submit({target:{id:'edit-form'},preventDefault(){prevented=true;},stopImmediatePropagation(){stopped=true;}});
  assert.ok(prevented&&stopped);
  assert.equal(page.get('#edit-error').textContent,'Next step 1: November 2026 has 30 days, so 31/11 is not a date.');
  assert.equal(page.get('#edit-error').hidden,false);
  assert.deepEqual(calls,[core.TOOLS.workspace]);
  // A corrected date clears the block and is written in the Sheet's unambiguous text form.
  page.events.input({target:{closest:()=>row,classList:{contains:key=>key==='ns-date'},value:'1/12/2026'}});
  prevented=false;
  page.events.submit({target:{id:'edit-form'},preventDefault(){prevented=true;},stopImmediatePropagation(){}});
  assert.equal(prevented,false);
  assert.ok(page.get('#edit-content').value.endsWith('(complete by Dec 1, 2026)'));
});

test('A status change posts the row, the status the founder saw and the new one, then reloads',async()=>{
  const requests=[];
  const fetcher=async(url,options)=>{requests.push({url,method:options.method,body:options.body&&JSON.parse(options.body),auth:options.headers.Authorization});
    return {ok:true,status:200,json:async()=>url==='/review/workspace'?{...completed(),statusOptions:['0. Paused','4. Pilot',7,'']}:{rowId:'r-1',company:'A real company',status:'0. Paused',inSalesPipeline:true}};};
  const changes=[];
  const client=core.createClient(core.restTransport(fetcher,()=> 'founder-access-secret'),{onChange:next=>changes.push(next)});
  const first=await client.refresh();
  assert.deepEqual(json(first.statusOptions),['0. Paused','4. Pilot']);
  const result=await client.setStatus('r-1','4. Pilot',' 0. Paused ');
  assert.equal(result.status,'0. Paused');
  assert.deepEqual(json(requests[1]),{url:'/review/rows/r-1/status',method:'POST',body:{expected_status:'4. Pilot',status:'0. Paused'},auth:'Bearer founder-access-secret'});
  assert.equal(requests[2].url,'/review/workspace');
  assert.equal(changes.length,2);
  await assert.rejects(client.setStatus('r-1','4. Pilot',''),/Choose a status/);
  await assert.rejects(core.restTransport(fetcher,()=> 'token')(core.TOOLS.status,{expected_status:'a',status:'b'}),/missing its identifier/);
  assert.equal(requests.length,3);
});

test('The card shows the current Sheet status with a change control that starts disabled',async()=>{
  const s=snapshot();
  s.statusOptions=['0. Paused','1. Opening','4. Pilot'];
  s.rows=[{id:'r-1',company:s.proposals[0].company,owner:'NDL',stage:'1. Opening',lastProgress:'',nextStep:'',revision:0}];
  const calls=[];
  const page=fakePage({callTool:async(name,args)=>{calls.push([name,args]);return {structuredContent:name===core.TOOLS.status?{status:'0. Paused',inSalesPipeline:true}:s};}});
  await new Promise(resolve=>setImmediate(resolve));
  const card=articleHtml(page);
  assert.ok(card.includes('<span class="stage stage-1">1. Opening</span>'));
  assert.ok(card.includes('data-status-current="1. Opening"'));
  assert.ok(card.includes('<option value="1. Opening" selected>1. Opening</option><option value="4. Pilot">4. Pilot</option>')||card.includes('<option value="0. Paused">0. Paused</option><option value="1. Opening" selected>1. Opening</option>'));
  assert.ok(card.includes('data-action="set-status" disabled'));
  // Choosing the same status does nothing; a different one sends exactly one status call.
  const select={value:'1. Opening',dataset:{statusCurrent:'1. Opening'}};
  const article={dataset:{id:s.proposals[0].id},querySelector:()=>select,querySelectorAll:()=>[]};
  const button={dataset:{action:'set-status'},closest:selector=>selector==='article'?article:selector==='[data-action]'?button:article};
  page.events.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.filter(([name])=>name===core.TOOLS.status).length,0);
  select.value='0. Paused';
  page.events.click({target:{closest:()=>button}});
  await new Promise(resolve=>setImmediate(resolve));
  const sent=calls.filter(([name])=>name===core.TOOLS.status);
  assert.deepEqual(json(sent),[[core.TOOLS.status,{row_id:'r-1',expected_status:'1. Opening',status:'0. Paused'}]]);
});

// ---- H-10 (11/17/2026): cards that only wait on the customer sit in a second agenda group
function agendaSnapshot(flags){
  const s=snapshot(),base=s.proposals[0];
  s.proposals=Object.entries(flags).map(([company,[date,waiting]],i)=>({...json(base),id:'p-'+(i+1),company,sources:[{...base.sources[0],date}],...(waiting===undefined?{}:{waiting})}));
  s.rows=s.proposals.map((p,i)=>({...json(s.rows[0]),id:'r-'+(i+1),company:p.company}));
  return s;
}
function warRoomPage(s){
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  page.context.document.body={dataset:{theme:'war-room'}};
  return page;
}
const agendaOrder=html=>[...html.matchAll(/data-select="([^"]+)"/g)].map(m=>m[1]);

test('The workspace keeps the waiting flag as a strict boolean and treats older data as needing action',()=>{
  const s=agendaSnapshot({Alpha:['2026-11-10',true],Beta:['2026-11-10',false],Gamma:['2026-11-10'],Delta:['2026-11-10','yes']});
  assert.deepEqual(core.workspace(s).proposals.map(p=>p.waiting),[true,false,false,false]);
});

test('The agenda lists cards needing action first and waiting cards in a second group, in the order Next and Prev follow',async()=>{
  // Andina is the newest, but it is only waiting on the customer, so it goes below the two action cards.
  const s=agendaSnapshot({Andina:['2026-11-16',true],Campari:['2026-11-12',false],Bimbo:['2026-11-14'],Chifles:['2026-11-01',true]});
  const page=warRoomPage(s);
  await new Promise(resolve=>setImmediate(resolve));
  let list=page.get('#review-list').innerHTML;
  assert.deepEqual(agendaOrder(list),['p-3','p-2','p-1','p-4']);
  assert.ok(list.indexOf('<span>Needs your action</span><span>2</span>')<list.indexOf('data-select="p-3"'));
  const waitingHeader=list.indexOf('<span>Waiting on them</span><span>2</span>');
  assert.ok(waitingHeader>list.indexOf('data-select="p-2"')&&waitingHeader<list.indexOf('data-select="p-1"'));
  // The first card shown is the first action card, and the counts keep their meaning: every pending card.
  assert.ok(list.includes('Card 1 of 4 ·'));
  assert.ok(list.includes('aria-labelledby="title-p-3"'));
  assert.ok(list.includes('<span>4 to review</span>'));
  assert.equal(page.get('#wr-strip-pending').textContent,4);
  assert.equal(page.get('#pending-count').textContent,4);
  assert.equal([...list.matchAll(/class="wr-row[^"]* wait"/g)].length,2);
  // Opening a waiting card says so next to its position.
  page.events.click({target:{closest:()=>({dataset:{action:'wr-select',select:'p-1'},closest:()=>null})}});
  list=page.get('#review-list').innerHTML;
  assert.ok(list.includes('Card 3 of 4 · waiting on them ·'));
  assert.ok(list.includes('aria-labelledby="title-p-1"'));
});

test('Without waiting cards the agenda is one list with no group headings',async()=>{
  const page=warRoomPage(agendaSnapshot({Andina:['2026-11-16'],Campari:['2026-11-12',false]}));
  await new Promise(resolve=>setImmediate(resolve));
  const list=page.get('#review-list').innerHTML;
  assert.deepEqual(agendaOrder(list),['p-1','p-2']);
  assert.equal(list.includes('Needs your action'),false);
  assert.equal(list.includes('Waiting on them'),false);
});

test('When every card is waiting they are still listed and reviewable',async()=>{
  const page=warRoomPage(agendaSnapshot({Andina:['2026-11-16',true]}));
  await new Promise(resolve=>setImmediate(resolve));
  const list=page.get('#review-list').innerHTML;
  assert.deepEqual(agendaOrder(list),['p-1']);
  assert.ok(list.includes('<span>Waiting on them</span><span>1</span>'));
  assert.equal(list.includes('Needs your action'),false);
  assert.ok(list.includes('data-action="approve"'));
});
// ---- H-11 (11/17/2026): the click is visibly taken until the service answers, and cannot be sent twice
function fakeArticle(id){
  const classes=new Set(),attrs={},make=(dataset={},cls=[])=>({dataset,disabled:false,classList:{contains:c=>cls.includes(c)},parentElement:null});
  const buttons={approve:make({action:'approve'}),edit:make({action:'edit'}),ignore:make({action:'ignore-menu'}),blocked:make({reviewBlocked:'true'})};
  const article={dataset:{id},classes,attrs,buttons,classList:{toggle:(c,on)=>{if(on)classes.add(c);else classes.delete(c);}},setAttribute:(k,v)=>{attrs[k]=v;},removeAttribute:k=>{delete attrs[k];},querySelectorAll:()=>Object.values(buttons),querySelector:()=>null};
  return article;
}
function pendingWrites(s){
  const writes=[],release=[];
  const page=fakePage({callTool:(name,args)=>{
    if(name===core.TOOLS.workspace)return Promise.resolve({structuredContent:s});
    writes.push({name,args:json(args)});
    return new Promise((resolve,reject)=>release.push({resolve:()=>resolve({structuredContent:{status:'approved'}}),reject}));
  }});
  const article=fakeArticle('p-1');
  page.context.document.querySelectorAll=selector=>selector==='article[data-id]'?[article]:[];
  return {page,writes,release,article};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('Approve shows Saving to Sheet on the card until the service answers and a second click sends nothing',async()=>{
  assert.ok(html.includes('<span class="btn-saving">Saving to Sheet...</span>'));
  assert.ok(html.includes('.review-card.is-saving .btn-saving{display:inline-flex}'));
  const s=snapshot(),{page,writes,release,article}=pendingWrites(s);
  await tick();
  const button={dataset:{action:'approve'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  page.events.click({target:{closest:()=>button}});
  await tick();
  assert.equal(writes.length,1);
  assert.deepEqual([...article.classes].sort(),['is-busy','is-saving']);
  assert.equal(article.attrs['aria-busy'],'true');
  assert.ok(Object.values(article.buttons).every(b=>b.disabled===true));
  assert.equal(page.get('#announcer').textContent,'Saving A real company to the Pipeline Sheet...');
  // A re-render while it is in flight (a filter change, a mode pick) keeps the card busy.
  page.get('#owner-filter').handlers.change();
  assert.ok(article.classes.has('is-saving'));
  page.events.click({target:{closest:()=>button}});
  assert.equal(writes.length,1);
  // When the service refuses, the card is usable again and still shows the reason.
  release[0].reject(new core.ReviewError('Sheet update not confirmed.',503));
  await tick();
  assert.equal(article.classes.size,0);
  assert.equal('aria-busy' in article.attrs,false);
  assert.equal(article.buttons.approve.disabled,false);
  assert.equal(article.buttons.blocked.disabled,true);
  page.events.click({target:{closest:()=>button}});
  await tick();
  assert.equal(writes.length,2);
  assert.deepEqual(writes[0],writes[1]);
});

test('Ignore marks the card busy without claiming a Sheet write',async()=>{
  const s=snapshot(),{page,writes,article}=pendingWrites(s);
  await tick();
  const button={dataset:{action:'ignore'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  page.events.click({target:{closest:()=>button}});
  await tick();
  assert.equal(writes.length,1);
  assert.equal(writes[0].name,core.TOOLS.ignore);
  assert.deepEqual([...article.classes],['is-busy']);
});

test('Save and approve shows Saving to Sheet in the editor, cannot be submitted twice and recovers after a failure',async()=>{
  assert.ok(/\.dialog-actions\{[^}]*position:sticky;bottom:0/.test(html));
  const s=snapshot(),{page,writes,release,article}=pendingWrites(s);
  await tick();
  page.events.click({target:{closest:()=>({dataset:{action:'edit'},closest:()=>article})}});
  const save=page.get('#save-approve'),form=page.get('#edit-form');
  assert.equal(save.textContent,'Save & approve');
  assert.equal(save.disabled,false);
  form.handlers.submit({preventDefault(){}});
  form.handlers.submit({preventDefault(){}});
  await tick();
  assert.equal(writes.length,1);
  assert.equal(save.textContent,'Saving to Sheet...');
  assert.equal(save.disabled,true);
  assert.ok(article.classes.has('is-saving'));
  release[0].reject(new core.ReviewError('Sheet update not confirmed.',503));
  await tick();
  assert.equal(save.textContent,'Save & approve');
  assert.equal(save.disabled,false);
  assert.equal(page.get('#edit-error').textContent,'Sheet update not confirmed.');
  assert.equal(page.get('#edit-dialog').open,true);
  assert.equal(article.classes.size,0);
  // The same edit can then be sent again and reuses its request key.
  form.handlers.submit({preventDefault(){}});
  await tick();
  assert.equal(writes.length,2);
  assert.deepEqual(writes[0],writes[1]);
});

test('Disconnecting while a save is in flight leaves no stuck saving state',async()=>{
  const s=snapshot(),{page,article}=pendingWrites(s);
  await tick();
  page.events.click({target:{closest:()=>({dataset:{action:'edit'},closest:()=>article})}});
  page.get('#edit-form').handlers.submit({preventDefault(){}});
  await tick();
  assert.equal(page.get('#save-approve').disabled,true);
  page.get('#disconnect').onclick();
  assert.equal(page.get('#save-approve').disabled,false);
  assert.equal(page.get('#save-approve').textContent,'Save & approve');
});
test('Messages of one thread on one day show as a single What\'s new line',async()=>{
  const s=snapshot();
  s.proposals[0].sources=[
    {id:'m-1',type:'Email',date:'2026-11-16',subject:'Re: Ramiro <> Allison',quote:'First passage.'},
    {id:'m-2',type:'Email',date:'2026-11-16',subject:'Re: Ramiro <> Allison',quote:'Second passage.'},
    {id:'n-1',type:'Meeting notes',date:'2026-11-05',subject:'Freestyle Snacks',quote:'Notes passage.'}
  ];
  const page=fakePage({callTool:async()=>({structuredContent:s})});
  await new Promise(resolve=>setImmediate(resolve));
  const card=page.get('#review-list').innerHTML;
  assert.equal(card.split('<span>Re: Ramiro &lt;&gt; Allison</span>').length-1,1);
  assert.ok(card.includes('<div class="ctx"><p>&ldquo;First passage.&rdquo;</p><p>&ldquo;Second passage.&rdquo;</p></div>'));
  assert.ok(card.includes('<span>Freestyle Snacks</span><span class="src">Source: Notes, Nov 5</span></summary><div class="ctx"><p>&ldquo;Notes passage.&rdquo;</p></div>'));
});


test('Matias has an MG section before assignment and sees only his assigned companies',async()=>{
  const data=snapshot();
  data.proposals.push({...data.proposals[0],id:'mg-1',company:'Matias prospect',owner:'MG'});
  data.rows.push({...data.rows[0],id:'mg-row',company:'Matias prospect',owner:'MG'});
  const page=fakePage({callTool:async()=>({structuredContent:data})});
  await new Promise(resolve=>setImmediate(resolve));
  page.get('#owner-filter').value='MG';
  page.get('#owner-filter').handlers.change();
  assert.ok(page.get('#review-list').innerHTML.includes('Matias prospect'));
  assert.ok(page.get('#review-list').innerHTML.includes('Matias'));
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  page.get('#owner-filter').value='NDL';
  page.get('#owner-filter').handlers.change();
  assert.ok(page.get('#review-list').innerHTML.includes('A real company'));
  assert.equal(page.get('#review-list').innerHTML.includes('Matias prospect'),false);
  const emptyPage=fakePage({callTool:async()=>({structuredContent:snapshot()})});
  await new Promise(resolve=>setImmediate(resolve));
  emptyPage.get('#owner-filter').value='MG';
  emptyPage.get('#owner-filter').handlers.change();
  assert.ok(emptyPage.get('#review-list').innerHTML.includes('No companies match'));
});

const briefId='81a993d4-f4fb-4d76-9fa8-c4b1a1fcb55e';
const savedBrief=()=>({id:briefId,for_date:'2026-11-19',subject:'Pipeline brief · Nov 19',html:'<table><tr><td>First row</td></tr><tr><td>Final available row</td></tr></table>',full_available:true,created_at:'2026-11-19T12:00:00Z'});

test('Daily Brief REST reads share header authentication and use fixed same-origin routes',async()=>{
  const calls=[];
  const transport=core.restTransport(async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>savedBrief()};},()=> 'founder-token');
  await transport(core.TOOLS.brief,{});
  await transport(core.TOOLS.brief,{brief_id:briefId});
  assert.deepEqual(calls.map(c=>c.url),['/review/briefs/latest','/review/briefs/'+briefId]);
  for(const call of calls){assert.equal(call.options.headers.Authorization,'Bearer founder-token');assert.equal(call.options.method,'GET');assert.equal(call.options.cache,'no-store');assert.equal(call.options.credentials,'omit');}
  await assert.rejects(transport(core.TOOLS.brief,{brief_id:'https://other.test/private'}),/link is invalid/);
  assert.equal(calls.length,2);
});

test('A normal Hub login opens Pipeline Updates and makes no Daily Brief request',async()=>{
  const calls=[];
  const page=fakePage(undefined,'/review/app',{fetcher:async(url)=>{calls.push(url);return {ok:true,json:async()=>snapshot()};}});
  page.get('#access-token').value='founder-token';
  await page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  assert.deepEqual(calls,['/review/workspace']);
  assert.equal(page.get('#pipeline-workspace-tab').getAttribute('aria-selected'),'true');
  assert.equal(page.get('#pipeline-workspace-panel').hidden,false);
  assert.equal(page.get('#daily-brief-panel').hidden,true);
});

test('An emailed Daily Brief link survives login and reads the pinned brief independently of the Sheet',async()=>{
  const calls=[],data=savedBrief();
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief&brief='+briefId,fetcher:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>data};}});
  assert.equal(page.get('#daily-brief-tab').getAttribute('aria-selected'),'true');
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#sign-in-form').hidden,false);
  assert.equal(calls.length,0);
  page.get('#access-token').value='founder-token';
  await page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/review/briefs/'+briefId);
  assert.equal(calls[0].options.headers.Authorization,'Bearer founder-token');
  assert.equal(page.get('#daily-brief-frame').hidden,false);
  assert.ok(page.get('#daily-brief-frame').srcdoc.includes(data.html));
  assert.equal(page.get('#daily-brief-frame').getAttribute('sandbox'),'allow-popups allow-popups-to-escape-sandbox');
  assert.equal(page.get('#daily-brief-frame').getAttribute('referrerpolicy'),'no-referrer');
  assert.equal(page.context.location.search,'?tab=daily-brief&brief='+briefId);
  assert.equal(page.context.location.href.includes('founder-token'),false);
  assert.equal(page.get('#connection-panel').hidden,true);
});

test('Daily Brief remains readable when the Pipeline Updates Sheet read fails',async()=>{
  const calls=[];
  const page=fakePage({callTool:async(name,args)=>{calls.push({name,args});if(name===core.TOOLS.workspace)throw new core.ReviewError('Sheet unavailable',503);return {structuredContent:savedBrief()};}});
  await new Promise(resolve=>setImmediate(resolve));
  page.get('#daily-brief-tab').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls.map(c=>c.name),[core.TOOLS.workspace,core.TOOLS.brief]);
  assert.equal(page.get('#daily-brief-frame').hidden,false);
  assert.equal(page.get('#connection-status').textContent,'Connected');
});

test('Workspace navigation and Pipeline Updates navigation keep independent selection',()=>{
  const page=fakePage(undefined);
  page.get('#activity-tab').onclick();
  page.get('#daily-brief-tab').onclick();
  assert.equal(page.get('#activity-tab').getAttribute('aria-selected'),'true');
  assert.equal(page.get('#activity-panel').hidden,false);
  assert.equal(page.get('#pipeline-workspace-panel').hidden,true);
  page.get('#daily-brief-tab').onkeydown({key:'ArrowLeft',preventDefault(){}});
  assert.equal(page.get('#pipeline-workspace-panel').hidden,false);
  assert.equal(page.get('#activity-tab').getAttribute('aria-selected'),'true');
  assert.equal(page.get('#review-tab').getAttribute('aria-selected'),'false');
});

test('A Daily Brief response arriving after disconnect cannot restore its contents',async()=>{
  let resolveRead;
  const page=fakePage({callTool:()=>new Promise(resolve=>{resolveRead=resolve;})},'/artifact',{search:'?tab=daily-brief'});
  page.get('#disconnect').onclick();
  resolveRead({structuredContent:savedBrief()});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#daily-brief-frame').hidden,true);
  assert.equal(page.get('#daily-brief-frame').srcdoc,undefined);
  assert.equal(page.get('#connection-status').textContent,'Connection needed');
});

test('Changing login credentials invalidates a pending Daily Brief response',async()=>{
  let resolveFirst;
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief',fetcher:async(_url,options)=>{
    if(options.headers.Authorization==='Bearer first-token')return new Promise(resolve=>{resolveFirst=resolve;});
    return {ok:true,json:async()=>({...savedBrief(),html:'<p>Second login brief</p>'})};
  }});
  page.get('#access-token').value='first-token';
  const first=page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  page.get('#access-token').value='second-token';
  await page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  resolveFirst({ok:true,json:async()=>savedBrief()});
  await first;
  assert.ok(page.get('#daily-brief-frame').srcdoc.includes('Second login brief'));
  assert.equal(page.get('#daily-brief-frame').srcdoc.includes('Final available row'),false);
});

test('A missing current Daily Brief has a visible empty state and no earlier content',async()=>{
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief',fetcher:async()=>({ok:false,status:404,json:async()=>({detail:'No brief for today'})})});
  page.get('#access-token').value='founder-token';
  await page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  assert.ok(page.get('#brief-status').textContent.includes('Today’s Daily Brief is not available yet'));
  assert.equal(page.get('#brief-status').hidden,false);
  assert.equal(page.get('#daily-brief-frame').hidden,true);
  assert.equal(page.get('#connection-panel').hidden,true);
});

test('Legacy archived briefs show their saved rows and disclose unavailable full content',async()=>{
  const page=fakePage({callTool:async()=>({structuredContent:{...savedBrief(),full_available:false}})},'/artifact',{search:'?tab=daily-brief'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(page.get('#brief-meta').hidden,false);
  assert.ok(page.get('#brief-meta').textContent.includes('rows saved with its email'));
  page.events['window:pagehide']();
  assert.equal(page.get('#daily-brief-frame').hidden,true);
  assert.equal(page.get('#daily-brief-frame').srcdoc,undefined);
});

test('War Room keyboard actions cannot activate a hidden card while Daily Brief is selected',()=>{
  const page=fakePage(undefined,'/artifact',{search:'?tab=daily-brief',warRoom:true});
  let hiddenCardReads=0;
  const get=page.context.document.querySelector;
  page.context.document.querySelector=selector=>{if(selector==='.wr-main .review-card')hiddenCardReads++;return get(selector);};
  for(const key of ['a','e','i','j','k'])page.events.keydown({key,target:{tagName:'BUTTON'},preventDefault(){}});
  assert.equal(hiddenCardReads,0);
});

const signedInSession=()=>({mode:'google',authenticated:true,actor:'RG',email:'ramiro@bianca-ai.com',expires_at:new Date(Date.now()+12*60*60*1000).toISOString(),csrf_token:'session-csrf-value'});
const authResponse=data=>({ok:true,status:200,json:async()=>data});
const flush=()=>new Promise(resolve=>setImmediate(resolve));

test('Session REST transport uses same-origin cookies and CSRF for writes without a bearer token',async()=>{
  const calls=[];
  const transport=core.restTransport(async(url,options)=>{calls.push({url,options});return authResponse(snapshot());},()=> 'csrf-only',{sessionAuth:true});
  await transport(core.TOOLS.workspace,{});
  await transport(core.TOOLS.brief,{brief_id:briefId});
  await transport(core.TOOLS.approve,{proposal_id:'p-1',revision:2,idempotency_key:'decision',next_step_bullets:['Confirm.'],client_context_bullets:[]});
  await transport(core.TOOLS.ignore,{proposal_id:'p-1',revision:2,idempotency_key:'ignore'});
  await transport(core.TOOLS.status,{row_id:'r-1',expected_status:'2. In Talks',status:'3. Paused'});
  for(const {options} of calls){assert.equal(options.credentials,'same-origin');assert.equal(options.headers.Authorization,undefined);assert.equal(options.cache,'no-store');assert.equal(options.redirect,'error');}
  for(const {options} of calls.slice(0,2))assert.equal(options.headers['X-CSRF-Token'],undefined);
  for(const {options} of calls.slice(2))assert.equal(options.headers['X-CSRF-Token'],'csrf-only');
});

test('Expired session errors clear the UI even if the server response is unreadable',async()=>{
  let expired=0;
  const transport=core.restTransport(async()=>({ok:false,status:401,json:async()=>{throw Error('not JSON');}}),()=> 'csrf',{sessionAuth:true,onAuthError:()=>expired++});
  await assert.rejects(transport(core.TOOLS.workspace,{}),error=>error.status===401);
  assert.equal(expired,1);
});

test('Google mode hides token login and preserves a pinned Daily Brief in the sign-in link',async()=>{
  let reads=0;
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief&brief='+briefId,sessionFetcher:async()=>authResponse({mode:'google',authenticated:false}),fetcher:async()=>{reads++;throw Error('No business requests before login');}});
  await flush();
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.equal(page.get('#sign-in-form').hidden,true);
  assert.equal(page.get('#account-status').hidden,true);
  assert.equal(page.get('#disconnect').hidden,true);
  const login=new URL(page.get('#google-sign-in').href,'https://hub.test');
  assert.equal(login.pathname,'/auth/google/login');
  assert.equal(login.searchParams.get('return_to'),'/review/app?tab=daily-brief&brief='+briefId);
  page.get('#access-token').value='old-founder-token';
  await page.get('#sign-in-form').handlers.submit({preventDefault(){}});
  assert.equal(reads,0,'Even a synthetic token form submit cannot bypass Google mode.');
});

test('Hosted Google mode never falls back to an injected Claude connector',async()=>{
  let connectorCalls=0;
  const page=fakePage({callTool:async()=>{connectorCalls++;return snapshot();}},'/review/app',{sessionFetcher:async()=>authResponse({mode:'google',authenticated:false})});
  await flush();await page.get('#refresh-updates').onclick();
  assert.equal(connectorCalls,0);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
});

test('Authentication discovery fails closed and can be retried without exposing token login',async()=>{
  let available=false;
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>available?authResponse({mode:'google',authenticated:false}):{ok:false,status:503}});
  await flush();
  assert.equal(page.get('#sign-in-form').hidden,true);
  assert.equal(page.get('#google-sign-in-panel').hidden,true);
  assert.equal(page.get('#auth-retry').hidden,false);
  assert.ok(page.get('#connection-error').textContent.includes('temporarily unavailable'));
  available=true;await page.get('#auth-retry').onclick();
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.equal(page.get('#auth-retry').hidden,true);
});

test('A valid Google session restores the Hub automatically without sliding its expiry',async()=>{
  const auth=signedInSession(),calls=[];
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async(url,options)=>{calls.push({url,options});return authResponse(auth);},fetcher:async(url,options)=>{calls.push({url,options});return authResponse(snapshot());}});
  await flush();
  assert.deepEqual(calls.map(c=>c.url),['/auth/session','/review/workspace']);
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(calls[1].options.headers.Authorization,undefined);
  assert.equal(page.get('#connection-panel').hidden,true);
  assert.equal(page.get('#account-status').hidden,false);
  assert.ok(page.get('#account-status').textContent.includes(auth.email));
  assert.ok(page.get('#account-status').textContent.includes('Sign-in expires'));
  assert.equal(page.get('#disconnect').textContent,'Sign out');
  const timer=[...page.timers.values()].find(t=>t.ms>40000000);
  assert.ok(timer&&timer.ms<=43200000);
  await page.get('#refresh-updates').onclick();
  assert.equal([...page.timers.values()].filter(t=>t.ms>40000000).length,1,'Refreshing does not reset the session timer.');
  assert.equal([...page.timers.values()].find(t=>t.ms>40000000),timer);
});

test('Google session login opens a saved brief without reading the Pipeline Sheet',async()=>{
  const calls=[];
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief&brief='+briefId,sessionFetcher:async()=>authResponse(signedInSession()),fetcher:async(url,options)=>{calls.push({url,options});return authResponse(savedBrief());}});
  await flush();
  assert.deepEqual(calls.map(c=>c.url),['/review/briefs/'+briefId]);
  assert.equal(calls[0].options.credentials,'same-origin');
  assert.equal(page.get('#daily-brief-frame').hidden,false);
  assert.ok(page.get('#daily-brief-frame').srcdoc.includes('Final available row'));
});

test('A Google approval uses the session CSRF and preserves the existing review contract',async()=>{
  const calls=[],auth=signedInSession();
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>authResponse(auth),fetcher:async(url,options)=>{calls.push({url,options});return authResponse(url==='/review/workspace'?snapshot():{status:'approved'});}});
  await flush();
  const button={dataset:{action:'approve'},closest:()=>({dataset:{id:'p-1'}})};
  page.events.click({target:{closest:()=>button}});await flush();
  const write=calls.find(c=>c.options.method==='POST');
  assert.equal(write.url,'/review/proposals/p-1/approve');
  assert.equal(write.options.headers['X-CSRF-Token'],auth.csrf_token);
  assert.equal(write.options.headers.Authorization,undefined);
  assert.deepEqual(JSON.parse(write.options.body),{revision:2,idempotency_key:'new-key',next_step_bullets:['Confirm the scope.'],client_context_bullets:[]});
});

test('Absolute expiry clears cards, open edits, account identity and pending writes from the page',async()=>{
  let reads=0;
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>authResponse(signedInSession()),fetcher:async()=>{reads++;return authResponse(snapshot());}});
  await flush();
  page.events.click({target:{closest:()=>({dataset:{action:'edit'},closest:()=>({dataset:{id:'p-1'}})})}});
  assert.equal(page.get('#edit-dialog').open,true);
  assert.ok(page.get('#edit-content').value.includes('Confirm the scope'));
  [...page.timers.values()].find(t=>t.ms>40000000).fn();
  assert.equal(page.get('#pending-count').textContent,0);
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  assert.equal(page.get('#edit-dialog').open,false);
  assert.equal(page.get('#edit-content').value,'');
  assert.equal(page.get('#ns-rows').innerHTML,'');
  assert.equal(page.get('#account-status').hidden,true);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.ok(page.get('#connection-error').textContent.includes('session has ended'));
  assert.equal(reads,1);
});

test('Session rejection on a write clears cached company data and offers Google sign-in',async()=>{
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>authResponse(signedInSession()),fetcher:async(url)=>url==='/review/workspace'?authResponse(snapshot()):{ok:false,status:403,json:async()=>({detail:'Invalid CSRF'})}});
  await flush();
  page.events.click({target:{closest:()=>({dataset:{action:'approve'},closest:()=>({dataset:{id:'p-1'}})})}});await flush();
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  assert.equal(page.get('#pending-count').textContent,0);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.equal(page.get('#sign-in-form').hidden,true);
});

test('Logout revokes the server session with CSRF and clears the Daily Brief immediately',async()=>{
  const calls=[];let finishLogout;
  const auth=signedInSession();
  const page=fakePage(undefined,'/review/app',{search:'?tab=daily-brief',sessionFetcher:async()=>authResponse(auth),fetcher:async(url,options)=>{calls.push({url,options});if(url==='/auth/logout')return new Promise(resolve=>{finishLogout=resolve;});return authResponse(savedBrief());}});
  await flush();const signingOut=page.get('#disconnect').onclick();
  assert.equal(page.get('#daily-brief-frame').srcdoc,undefined);
  assert.equal(page.get('#disconnect').disabled,true);
  const call=calls.find(c=>c.url==='/auth/logout');
  assert.equal(call.options.method,'POST');assert.equal(call.options.headers['X-CSRF-Token'],auth.csrf_token);assert.equal(call.options.credentials,'same-origin');
  finishLogout({ok:true,status:204});await signingOut;
  assert.equal(page.get('#account-status').hidden,true);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.ok(page.get('#connection-error').textContent.includes('Signed out'));
});

test('An uncertain logout keeps its retry available without restoring company data',async()=>{
  let fail=true,logouts=0;
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>authResponse(signedInSession()),fetcher:async url=>{if(url==='/auth/logout'){logouts++;if(fail)throw Error('offline');return {ok:true,status:204};}return authResponse(snapshot());}});
  await flush();await page.get('#disconnect').onclick();
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  assert.ok(page.get('#connection-error').textContent.includes('could not be confirmed'));
  assert.equal(page.get('#disconnect').hidden,false);
  assert.equal(page.get('#disconnect').disabled,false);
  assert.equal(page.get('#google-sign-in-panel').hidden,true);
  fail=false;await page.get('#disconnect').onclick();
  assert.equal(logouts,2);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
});

test('Back-forward cache restoration checks the server instead of reusing old session data',async()=>{
  let loggedIn=true,sessionReads=0;
  const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>{sessionReads++;return authResponse(loggedIn?signedInSession():{mode:'google',authenticated:false});},fetcher:async()=>authResponse(snapshot())});
  await flush();assert.ok(page.get('#review-list').innerHTML.includes('A real company'));
  page.events['window:pagehide']();
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
  assert.equal(page.get('#account-status').hidden,true);
  loggedIn=false;page.events['window:pageshow']({persisted:true});await flush();
  assert.equal(sessionReads,2);
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.equal(page.get('#review-list').innerHTML.includes('A real company'),false);
});

test('A session probe arriving after page exit cannot reattach the prior account',async()=>{
  let finishSession,businessReads=0;
  const page=fakePage(undefined,'/review/app',{sessionFetcher:()=>new Promise(resolve=>{finishSession=resolve;}),fetcher:async()=>{businessReads++;return authResponse(snapshot());}});
  page.events['window:pagehide']();finishSession(authResponse(signedInSession()));await flush();
  assert.equal(businessReads,0);
  assert.equal(page.get('#account-status').hidden,true);
  assert.equal(page.get('#sign-in-form').hidden,true);
});

test('Expired or malformed restored sessions fail closed before business reads',async()=>{
  for(const auth of [{...signedInSession(),expires_at:'bad-date'},{...signedInSession(),csrf_token:''},{...signedInSession(),expires_at:new Date(Date.now()-1).toISOString()}]){
    let reads=0;
    const page=fakePage(undefined,'/review/app',{sessionFetcher:async()=>authResponse(auth),fetcher:async()=>{reads++;return authResponse(snapshot());}});
    await flush();assert.equal(reads,0);assert.equal(page.get('#sign-in-form').hidden,true);assert.equal(page.get('#account-status').hidden,true);
  }
});

test('Login errors use trusted explanations and remove the error from the browser URL',async()=>{
  for(const reason of ['not_allowed','identity_mismatch','<script>alert(1)</script>']){
    const page=fakePage(undefined,'/review/app',{search:'?auth_error='+encodeURIComponent(reason)+'&tab=daily-brief&brief='+briefId,sessionFetcher:async()=>authResponse({mode:'google',authenticated:false})});
    await flush();assert.equal(page.get('#connection-error').hidden,false);assert.equal(page.get('#connection-error').textContent.includes('<script>'),false);assert.equal(page.context.location.search.includes('auth_error'),false);assert.ok(page.context.location.search.includes(briefId));assert.equal(page.get('#google-sign-in-panel').hidden,false);
  }
});

test('A delayed unauthorized workspace response cannot strand a pending logout',async()=>{
  let finishRead,finishLogout,loggedIn=true,sessionReads=0;
  const page=fakePage(undefined,'/review/app',{
    sessionFetcher:async()=>{sessionReads++;return authResponse(loggedIn?signedInSession():{mode:'google',authenticated:false});},
    fetcher:async url=>url==='/auth/logout'?new Promise(resolve=>{finishLogout=resolve;}):new Promise(resolve=>{finishRead=resolve;})
  });
  await flush();
  const signingOut=page.get('#disconnect').onclick();
  finishRead({ok:false,status:401,json:async()=>({detail:'Session ended'})});await flush();
  loggedIn=false;finishLogout({ok:true,status:204});await signingOut;
  assert.equal(page.get('#google-sign-in-panel').hidden,false);
  assert.equal(page.get('#sign-in-form').hidden,true);
  assert.equal(page.get('#disconnect').hidden,true);
  assert.ok(page.get('#connection-error').textContent.includes('Signed out'));
  await page.get('#refresh-updates').onclick();
  assert.equal(sessionReads,2,'Session checks are not permanently blocked by an abandoned logout.');
});

test('An old transport unauthorized response cannot expire a newly restored session',async()=>{
  let finishOldRead,reads=0;
  const page=fakePage(undefined,'/review/app',{
    sessionFetcher:async()=>authResponse({...signedInSession(),email:reads?'new-account@bianca-ai.com':'old-account@bianca-ai.com'}),
    fetcher:async()=>++reads===1?new Promise(resolve=>{finishOldRead=resolve;}):authResponse(snapshot())
  });
  await flush();
  page.events['window:pagehide']();page.events['window:pageshow']({persisted:true});await flush();
  assert.ok(page.get('#account-status').textContent.includes('new-account@bianca-ai.com'));
  assert.ok(page.get('#review-list').innerHTML.includes('A real company'));
  finishOldRead({ok:false,status:401,json:async()=>({detail:'Old session ended'})});await flush();
  assert.equal(page.get('#account-status').hidden,false);
  assert.ok(page.get('#account-status').textContent.includes('new-account@bianca-ai.com'));
  assert.equal(page.get('#connection-panel').hidden,true);
  assert.ok(page.get('#review-list').innerHTML.includes('A real company'));
});

(async()=>{for(const t of tests){try{await t.fn();passed++;process.stdout.write('PASS '+t.name+'\n');}catch(error){failed++;process.stdout.write('FAIL '+t.name+'\n  '+error.stack+'\n');}}process.stdout.write(`\n${passed} passed; ${failed} failed.\n`);process.exitCode=failed?1:0;})();
