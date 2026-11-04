'use strict';
// Exercise source date labels west of UTC so date-only values cannot shift a day.
process.env.TZ='America/Chicago';

// Run with the bundled Node runtime. Exercises the demo state engine and rendered company cards.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../artifacts/sales-pipeline-updates-demo.html'), 'utf8');
const match = html.match(/<script\s+id="bianca-core"[^>]*>([\s\S]*?)<\/script>/);
assert.ok(match, 'The demo must expose its actual pure core script.');
const context = vm.createContext({});
vm.runInContext(match[1] + '\n;globalThis.BiancaCore = BiancaCore;', context);
const core = context.BiancaCore;
const json = value => JSON.parse(JSON.stringify(value));
const rowFor = (state, id) => state.rows.find(row => row.id === id);
const proposalFor = (state, id) => state.proposals.find(proposal => proposal.id === id);
const at = '2026-11-02T20:30:00.000Z';
const approve=(state,id,nextStepBullets,at)=>core.approve(state,id,{nextStepBullets,clientContextBullets:[]},at);
let passed = 0;
let failed = 0;
function test(name, run) {
  try { run(); passed++; process.stdout.write(`PASS ${name}\n`); }
  catch (error) { failed++; process.stdout.write(`FAIL ${name}\n  ${error.message}\n`); }
}

test('Fresh workspace has six pending proposals, matching company rows, and valid state', () => {
  const state = core.initial();
  assert.equal(state.proposals.length, 6);
  assert.equal(state.rows.length, 6);
  assert.ok(state.proposals.every(proposal => proposal.status === 'pending'));
  assert.deepEqual(state.proposals.map(p => p.id).sort(), state.rows.map(r => r.id).sort());
  assert.equal(core.valid(state), true);
});

test('Approval exactly replaces Last progress with previous Next step', () => {
  const before = core.initial();
  const id = 'pernod-ricard';
  const oldRow = rowFor(before, id);
  const after = core.approve(before, id, proposalFor(before, id), at);
  const updated = rowFor(after, id);
  assert.equal(updated.lastProgress, oldRow.nextStep);
  assert.notEqual(updated.lastProgress, oldRow.lastProgress);
  assert.equal(updated.lastProgress.includes(oldRow.lastProgress), false, 'Old Last progress must not be appended.');
  assert.equal(updated.nextStep, proposalFor(before, id).nextStepBullets.map(b => '- ' + b).join('\n'));
  assert.equal(updated.revision, 1);
  assert.equal(proposalFor(after, id).status, 'approved');
  assert.equal(proposalFor(after, id).edited, false);
});

test('Approval preserves every unrelated row and proposal', () => {
  const before = core.initial();
  const id = 'campari';
  const after = approve(before, id, ['Send the agreed field list.'], at);
  assert.deepEqual(json(after.rows.filter(r => r.id !== id)), json(before.rows.filter(r => r.id !== id)));
  assert.deepEqual(json(after.proposals.filter(p => p.id !== id)), json(before.proposals.filter(p => p.id !== id)));
});

test('Approval does not mutate its input workspace', () => {
  const before = core.initial();
  const snapshot = JSON.stringify(before);
  approve(before, 'campari', ['A manually approved next step.'], at);
  assert.equal(JSON.stringify(before), snapshot);
});

test('Manual edits accept common bullet markers, preserve wording, and write one dash per line', () => {
  const before = core.initial();
  const edited = core.parseBullets('  • Confirm the pilot scope.\r\n\r\n* Send the checklist.\n1. Book the kickoff.\n2) Include Tomás and Elaine.  ');
  assert.deepEqual(json(edited), ['Confirm the pilot scope.', 'Send the checklist.', 'Book the kickoff.', 'Include Tomás and Elaine.']);
  const after = approve(before, 'pernod-ricard', edited, at);
  assert.equal(rowFor(after, 'pernod-ricard').nextStep, '- Confirm the pilot scope.\n- Send the checklist.\n- Book the kickoff.\n- Include Tomás and Elaine.');
  assert.equal(proposalFor(after, 'pernod-ricard').edited, true);
  assert.equal(after.activity[0].edited, true);
});

test('Edited approval keeps an exact audit snapshot of overwritten and written cells', () => {
  const before = core.initial();
  const oldRow = rowFor(before, 'campari');
  const after = approve(before, 'campari', ['Confirm the import date.'], at);
  assert.deepEqual(json(after.activity[0].before), {lastProgress: oldRow.lastProgress, nextStep: oldRow.nextStep,clientContext:oldRow.clientContext});
  assert.deepEqual(json(after.activity[0].after), {lastProgress: oldRow.nextStep, nextStep: '- Confirm the import date.',clientContext:oldRow.clientContext});
  assert.equal(after.activity[0].at, at);
});

test('Repeated approval is idempotent and cannot rotate the sheet twice', () => {
  const initial = core.initial();
  const once = approve(initial, 'campari', ['First approved wording.'], at);
  const twice = approve(once, 'campari', ['Different duplicate wording.'], '2026-11-02T20:31:00.000Z');
  assert.equal(twice, once);
  assert.equal(twice.activity.length, 1);
  assert.equal(rowFor(twice, 'campari').revision, 1);
  assert.equal(rowFor(twice, 'campari').nextStep, '- First approved wording.');
});

test('An empty previous Next step replaces Last progress with an empty cell', () => {
  const initial = core.initial();
  rowFor(initial, 'koala-eco').nextStep = '';
  const after = approve(initial, 'koala-eco', ['Send the onboarding checklist.'], at);
  assert.equal(rowFor(after, 'koala-eco').lastProgress, '');
  assert.equal(after.activity[0].after.lastProgress, '');
});

test('Blank edits and malformed bullet arrays cannot be approved', () => {
  const before = core.initial();
  const snapshot = JSON.stringify(before);
  for (const bullets of [core.parseBullets(' \n - \n • \n * '), [], [''], ['   '], ['Valid', ' '], [null], 'A string']) {
    assert.throws(() => approve(before, 'campari', bullets, at), /at least one bullet/);
  }
  assert.equal(JSON.stringify(before), snapshot);
});

test('Ignore removes a pending card without changing any sheet cell', () => {
  const before = core.initial();
  const after = core.ignore(before, 'farmater', at);
  assert.deepEqual(json(after.rows), json(before.rows));
  assert.equal(proposalFor(after, 'farmater').status, 'ignored');
  assert.equal(after.proposals.filter(p => p.status === 'pending').length, 5);
  assert.equal(after.activity[0].kind, 'ignored');
  assert.equal(proposalFor(before, 'farmater').status, 'pending');
});

test('Repeated ignore is idempotent', () => {
  const ignored = core.ignore(core.initial(), 'farmater', at);
  assert.equal(core.ignore(ignored, 'farmater', at), ignored);
  assert.equal(ignored.activity.length, 1);
});

test('Undo ignore restores the original card and leaves sheet rows unchanged', () => {
  const before = core.initial();
  const ignored = core.ignore(before, 'farmater', at);
  const restored = core.undoIgnore(ignored, ignored.activity[0].id);
  assert.deepEqual(json(restored), json(before));
  assert.equal(core.valid(restored), true);
});

test('Undo ignore cannot revert an approved company', () => {
  const before = core.initial();
  const ignored = core.ignore(before, 'farmater', at);
  const actionId = ignored.activity[0].id;
  const restored = core.undoIgnore(ignored, actionId);
  const approved = approve(restored, 'farmater', ['Deliver the guide.'], at);
  assert.equal(core.undoIgnore(approved, actionId), approved);
  assert.equal(proposalFor(approved, 'farmater').status, 'approved');
});

test('Approving an ignored company or ignoring an approved company makes no changes', () => {
  const ignored = core.ignore(core.initial(), 'farmater', at);
  assert.equal(approve(ignored, 'farmater', ['Unexpected content.'], at), ignored);
  const approved = approve(core.initial(), 'farmater', ['Approved content.'], at);
  assert.equal(core.ignore(approved, 'farmater', at), approved);
});

test('Unknown companies cannot be approved or ignored', () => {
  assert.throws(() => approve(core.initial(), 'unknown', ['A bullet.'], at), /Company not found/);
  assert.throws(() => core.ignore(core.initial(), 'unknown', at), /Company not found/);
});

test('Normal review transitions and JSON persistence retain valid state', () => {
  let state = core.initial();
  state = approve(state, 'pernod-ricard', ['Confirm the scope.'], at);
  state = core.ignore(state, 'farmater', at);
  assert.equal(core.valid(state), true);
  assert.equal(core.valid(json(state)), true);
});

test('State validation rejects wrong schema, missing companies, invalid status, and non-text cells', () => {
  for (const mutate of [s => {s.schema = 99;}, s => {s.proposals.pop();}, s => {s.rows.pop();}, s => {s.proposals[0].status = 'sent';}, s => {s.rows[0].lastProgress = null;}, s => {s.rows[0].nextStep = 42;}, s => {s.rows[0].revision = 0.5;}]) {
    const state = core.initial();
    mutate(state);
    assert.equal(core.valid(state), false);
  }
  assert.equal(Boolean(core.valid(null)), false);
});

test('State validation rejects malformed saved activity instead of accepting data that crashes rendering', () => {
  const state = core.initial();
  state.activity = [null];
  assert.equal(core.valid(state), false, 'Local-storage state with null activity must not reach render().');
});

function fakePage(savedState){
  class Element {
    constructor(){this.textContent='';this.innerHTML='';this.value='';this.hidden=false;this.disabled=false;this.handlers={};}
    addEventListener(name,handler){this.handlers[name]=handler;}
    close(){this.open=false;}showModal(){this.open=true;}focus(){}removeAttribute(){}setAttribute(){}replaceChildren(){this.innerHTML='';}
    querySelectorAll(){return [];}querySelector(){return new Element();}
  }
  const elements=new Map(),events={},storage=new Map();
  if(savedState)storage.set('bianca.pipeline-review.demo.v3',JSON.stringify(savedState));
  const get=selector=>{if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);};
  get('#owner-filter').value='all';
  const page=vm.createContext({
    document:{querySelector:get,querySelectorAll:()=>[],addEventListener:(name,handler)=>{events[name]=handler;}},
    window:{addEventListener:(name,handler)=>{events['window:'+name]=handler;},scrollTo:()=>{}},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    setTimeout:()=>0,clearTimeout:()=>{}
  });
  for(const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g))vm.runInContext(script[1],page);
  return {get,events};
}

test('Demo navigation and actions contain no Sheet preview or bottom rotation hint',()=>{
  for(const text of ['sheet-tab','sheet-panel','sheet-rows','Current Next step → Last progress','Approved bullets → Next step'])assert.equal(html.includes(text),false,text);
  assert.equal(/Sheet preview/i.test(html),false);
  const page=fakePage();
  assert.equal(page.get('#pending-count').textContent,6);
  assert.ok(page.get('#review-list').innerHTML.includes('data-action="approve"'));
  assert.ok(page.get('#review-list').innerHTML.includes('data-action="edit"'));
  assert.ok(page.get('#review-list').innerHTML.includes('data-action="ignore"'));
});

test('Demo sources show every type and date without rendering sample quotes',()=>{
  const state=core.initial();
  state.proposals[0].sources=[
    {id:'date-notes',type:'Meeting notes',date:'2026-09-28',quote:'Hidden notes quotation.'},
    {id:'date-email',type:'Email',date:'2026-10-06',quote:'Hidden email quotation.'},
    {id:'date-calendar',type:'Calendar',date:'2026-10-07',quote:'Hidden calendar quotation.'}
  ];
  for(const edges of Object.values(state.proposals[0].bulletLineage))for(const edge of edges)edge.sourceIds=['date-notes'];
  const page=fakePage(state),card=page.get('#review-list').innerHTML;
  for(const label of ['Notes · Sep 28','Email · Oct 6','Calendar · Oct 7'])assert.ok(card.includes(label),label);
  for(const source of state.proposals[0].sources)assert.equal(card.includes(source.quote),false);
  assert.equal(card.includes('View source context'),false);
});

test('Demo Current Next steps reads the current row with escaped multiline text',()=>{
  const state=core.initial(),id='pernod-ricard';
  proposalFor(state,id).nextStep='Outdated proposal context';
  rowFor(state,id).nextStep='- Current action & details.\n- <img src=x> Follow up.';
  const card=fakePage(state).get('#review-list').innerHTML;
  const details=card.match(/<details[^>]*class="current-next-steps"[^>]*>([\s\S]*?)<\/details>/);
  assert.ok(details,'Current Next steps uses native details.');
  assert.equal(/\sopen(?:[\s=>])/.test(details[0].slice(0,details[0].indexOf('>'))),false);
  assert.ok(details[1].includes('<summary>Current Next steps</summary>'));
  assert.ok(details[1].includes('- Current action &amp; details.\n- &lt;img src=x&gt; Follow up.'));
  assert.equal(card.includes('Outdated proposal context'),false);
  assert.equal(card.includes('<img src=x>'),false);
  assert.ok(/\.current-next-steps-content\{[^}]*white-space:pre-wrap/.test(html));
});

test('Demo retains the expanded context through filters and closes it when the workspace is reset',()=>{
  const page=fakePage();
  page.events.toggle({target:{open:true,matches:()=>true,closest:()=>({dataset:{id:'pernod-ricard'}})}});
  page.get('#company-search').handlers.input();
  assert.ok(/<details[^>]*class="current-next-steps"[^>]* open>/.test(page.get('#review-list').innerHTML));
  page.get('#confirm-reset').onclick();
  assert.equal(/<details[^>]*class="current-next-steps"[^>]* open>/.test(page.get('#review-list').innerHTML),false);
});

test('Demo current row with no Next steps shows a clear empty state',()=>{
  const state=core.initial(),id='pernod-ricard';
  proposalFor(state,id).nextStep='Older context must not return';rowFor(state,id).nextStep='';
  const card=fakePage(state).get('#review-list').innerHTML;
  assert.ok(card.includes('No Next steps are currently noted.'));
  assert.equal(card.includes('Older context must not return'),false);
});


test('New schema rejects saved flat legacy proposals instead of guessing column categories',()=>{
  const legacy=core.initial();legacy.schema=1;
  assert.equal(core.valid(legacy),false);
  assert.throws(()=>core.approve(core.initial(),'campari',['A flat legacy bullet.'],at),/at least one bullet/);
  assert.equal(html.includes('bianca.pipeline-review.demo.v3'),true);
});

test('Approval routes groups to separate cells and preserves the old Client Context exactly',()=>{
  const before=core.initial(),id='pernod-ricard',old=rowFor(before,id);
  const after=core.approve(before,id,{nextStepBullets:['Blocker: Store codes are missing.'],clientContextBullets:['Buying signal: Pilot approved.']},at);
  assert.equal(rowFor(after,id).nextStep,'- Blocker: Store codes are missing.');
  assert.equal(rowFor(after,id).lastProgress,old.nextStep);
  assert.equal(rowFor(after,id).clientContext,old.clientContext+'\n- Buying signal: Pilot approved.');
  assert.equal(rowFor(after,id).nextStep.includes('Buying signal'),false);
});

test('Context-only approval changes no action or progress cell',()=>{
  const before=core.initial(),old=rowFor(before,'campari');
  const after=core.approve(before,'campari',{nextStepBullets:[],clientContextBullets:['Objection: Timing depends on budget.']},at);
  assert.equal(rowFor(after,'campari').lastProgress,old.lastProgress);
  assert.equal(rowFor(after,'campari').nextStep,old.nextStep);
  assert.equal(rowFor(after,'campari').clientContext,old.clientContext+'\n- Objection: Timing depends on budget.');
  const page=fakePage(after),history=page.get('#activity-list').innerHTML;
  assert.ok(history.includes('<strong>Client Context</strong>'));
  assert.equal(history.includes('<strong>Next Step</strong>'),false);
});

test('Next-Step-only approval leaves Client Context intact',()=>{
  const before=core.initial(),old=rowFor(before,'campari');
  const after=core.approve(before,'campari',{nextStepBullets:['Send the data fields.'],clientContextBullets:[]},at);
  assert.equal(rowFor(after,'campari').clientContext,old.clientContext);
});

test('Context append deduplicates bullets without trimming or rewriting existing context',()=>{
  const existing='  Existing introductory context.\n• Customer need: Retail reporting.\n';
  assert.equal(core.appendContext(existing,['customer need: Retail reporting.','Objection: Timing.','Objection: Timing.']),existing+'- Objection: Timing.');
  assert.equal(core.appendContext(existing,['Customer need: Retail reporting.']),existing);
});

test('Both groups empty or malformed cannot be approved',()=>{
  for(const update of [{nextStepBullets:[],clientContextBullets:[]},{nextStepBullets:['Valid'],clientContextBullets:[null]},{nextStepBullets:[],clientContextBullets:'Wrong'},{nextStepBullets:['x'.repeat(4001)],clientContextBullets:[]}]){
    assert.throws(()=>core.approve(core.initial(),'campari',update,at));
  }
});

test('Cards and editor expose separate destination groups and existing Client Context',()=>{
  const before=core.initial(),p=proposalFor(before,'pernod-ricard');
  const page=fakePage(before),cards=page.get('#review-list').innerHTML;
  assert.ok(cards.includes('Next Step · actions and blockers'));
  assert.ok(cards.includes('Client Context · needs, signals, and objections'));
  assert.ok(cards.includes('<summary>Current Client Context</summary>'));
  assert.ok(html.includes('id="edit-client-context"'));
  const article={dataset:{id:p.id}},button={dataset:{action:'edit'},closest:()=>article};
  page.events.click({target:{closest:()=>button}});
  assert.equal(page.get('#edit-content').value,p.nextStepBullets.map(b=>'- '+b).join('\n'));
  assert.equal(page.get('#edit-client-context').value,p.clientContextBullets.map(b=>'- '+b).join('\n'));
  assert.equal(page.get('#previous-client-context').textContent,rowFor(before,p.id).clientContext);
});

test('Empty current Client Context is explicit and does not revive the proposal baseline',()=>{
  const before=core.initial();before.proposals[0].clientContext='Old context';before.rows[0].clientContext='';
  const cards=fakePage(before).get('#review-list').innerHTML;
  assert.ok(cards.includes('No Client Context is currently noted.'));
  assert.equal(cards.includes('Old context'),false);
});


function clickSource(page,companyId,sourceId){const article={dataset:{id:companyId}},button={dataset:{action:'source',sourceId},closest:selector=>selector==='[data-id]'?article:null};page.events.click({target:{closest:()=>button}});}
const demoArticle=(page,id)=>page.get('#review-list').innerHTML.match(new RegExp('<article[^>]*data-id="'+id+'"[\\s\\S]*?<\\/article>'))?.[0]||'';
const demoHighlighted=(card,bulletId)=>new RegExp('<li(?=[^>]*data-bullet-id="'+bulletId+'")(?=[^>]*class="source-highlight")[^>]*>').test(card);

test('Fictional Pernod source edges explicitly link Email to its first two actions and Notes to its last action',()=>{
  const p=core.initial().proposals[0],data=core.trace(p);
  assert.equal(data.available,true);assert.equal(p.lineageVersion,1);
  assert.deepEqual(json(data.lineage.nextStep.map(edge=>edge.sourceIds)),[[p.sources[0].id],[p.sources[0].id],[p.sources[1].id]]);
  const page=fakePage();clickSource(page,p.id,p.sources[0].id);let card=demoArticle(page,p.id);
  assert.equal(demoHighlighted(card,p.bulletLineage.nextStep[0].bulletId),true);
  assert.equal(demoHighlighted(card,p.bulletLineage.nextStep[1].bulletId),true);
  assert.equal(demoHighlighted(card,p.bulletLineage.nextStep[2].bulletId),false);
  assert.ok(page.get('#announcer').textContent.includes('2 bullets highlighted from Email · Nov 2'));
  clickSource(page,p.id,p.sources[1].id);card=demoArticle(page,p.id);
  assert.equal(demoHighlighted(card,p.bulletLineage.nextStep[0].bulletId),false);
  assert.equal(demoHighlighted(card,p.bulletLineage.nextStep[2].bulletId),true);
  assert.equal(demoHighlighted(card,p.bulletLineage.clientContext[0].bulletId),true);
});

test('Source selection toggles independently without storing a decision or changing any demo cell',()=>{
  const state=core.initial(),p=state.proposals[0],page=fakePage(state);
  clickSource(page,p.id,p.sources[0].id);
  assert.equal(page.get('#pending-count').textContent,6);assert.equal(page.get('#activity-count').textContent,0);
  assert.equal(demoArticle(page,'campari').includes('class="source-highlight"'),false);
  clickSource(page,p.id,p.sources[0].id);
  assert.equal(demoArticle(page,p.id).includes('class="source-highlight"'),false);
  assert.ok(page.get('#announcer').textContent.includes('highlighting cleared'));
});

test('Demo graph validation refuses old or fabricated attribution',()=>{
  for(const mutate of [s=>{s.schema=2;},s=>{delete s.proposals[0].lineageVersion;},s=>{s.proposals[0].bulletLineage.nextStep[0].sourceIds=['invented'];},s=>{s.proposals[0].bulletLineage.nextStep[0].text='Wrong positional text';}]){
    const state=core.initial();mutate(state);assert.equal(core.valid(state),false);
  }
});

test('A manually rewritten demo approval keeps no false link to the original source',()=>{
  const before=core.initial(),after=core.approve(before,'pernod-ricard',{nextStepBullets:['A founder supplied replacement action.'],clientContextBullets:[]},at);
  const p=after.proposals.find(item=>item.id==='pernod-ricard');
  assert.deepEqual(json(p.bulletLineage.nextStep[0].sourceIds),[]);
  assert.equal(p.bulletLineage.nextStep[0].origin,'founder_edit');
  assert.equal(core.valid(after),true);
});

test('The demo keeps a selected source on a filter rerender and clears it on workspace reset',()=>{
  const p=core.initial().proposals[0],page=fakePage();clickSource(page,p.id,p.sources[0].id);
  page.get('#company-search').handlers.input();
  assert.equal(demoHighlighted(demoArticle(page,p.id),p.bulletLineage.nextStep[0].bulletId),true);
  page.get('#confirm-reset').onclick();
  assert.equal(demoArticle(page,p.id).includes('class="source-highlight"'),false);
});

process.stdout.write(`\n${passed} passed; ${failed} failed.\n`);
process.exitCode = failed ? 1 : 0;
