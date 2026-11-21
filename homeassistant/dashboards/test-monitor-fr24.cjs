// Exercise the shipped explicit FR24 diagnostic UI with synthetic HTTP replies.
// No API token, provider call, or flight-state mutation is used by this test.
const {spawnSync}=require('node:child_process');
const {resolve}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const extracted=spawnSync('python3',['-c',"import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['cards'][0]['custom_fields']['content'][3:-3])",resolve(__dirname,'comatalarm.yaml')],{encoding:'utf8'});
assert.equal(extracted.status,0,extracted.stderr);
const now=Date.parse('2026-11-21T08:30:00Z'),seconds=now/1000,day='2026-11-21';
class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function reply(id='42a83e5a',status=200){
 return {ok:status===200,diagnostic:{schema:'comatalarm.fr24-events.v1',requested_at:seconds-1,completed_at:seconds,endpoint:'https://fr24api.flightradar24.com/api/historic/flight-events/light',method:'GET',params:{flight_ids:id,event_types:'gate_arrival'},http_status:status,content_type:'application/json',response:{data:[{fr24_id:id,events:[{type:'gate_arrival',timestamp:'2023-01-27T06:16:18Z',details:{gate_ident:null,gate_lat:55.61253,gate_lon:12.63116}}]}]},response_text:null,error:null,message:'Antwort abgerufen',response_truncated:false,redacted:false}};
}
function setup({api=async()=>reply(),secure=true,clipboard=async()=>{},legacyCopy=true}={}){
 const data={generated_at:seconds,started_at:seconds-3600,cycle_finished:seconds,api_status:{results:{}},pushes:{pending:0,accepted:0,expired:0},stand_model:{ready:true},devices:[],events:[],flights:[{number:'LH194',day,fr24ID:'42a83e5a',origin:'FRA',destination:'BER',category:'active',ata:seconds-600,live:{},schedule:{},subscribers:[]}],history:{day,available_days:[],flights:[],events:[]}};
 const listeners={},calls=[],downloads=[],clipboardCalls=[],timers=[],elements=[],blobs=new Map();let html='',legacyCalls=0;
 const document={activeElement:{focus(){}},body:{appendChild(){}},createElement(tag){const el={tagName:tag.toUpperCase(),style:{},focus(){},select(){this.selected=true;},setSelectionRange(){},remove(){this.removed=true;},click(){if(tag==='a')downloads.push({filename:this.download,url:this.href});}};elements.push(el);return el;},execCommand(){legacyCalls++;return legacyCopy;}};
 const card={isConnected:true,_comatData:data,_comatDay:day,_comatReceived:now,_comatFetchAt:now,shadowRoot:{addEventListener:(kind,fn)=>listeners[kind]=fn,appendChild(){}},requestUpdate(name){if(name==='_config')queueMicrotask(draw);}};
 const context=vm.createContext({Date:Clock,Intl,Promise,encodeURIComponent,console,Blob,document,isSecureContext:secure,navigator:{clipboard:{writeText:async text=>{clipboardCalls.push(text);return clipboard(text);}}},URL:{createObjectURL(blob){const id='blob:fr24-'+blobs.size;blobs.set(id,blob);return id;},revokeObjectURL(){}},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){}});
 const render=vm.runInContext('(function(hass){'+extracted.stdout+'})',context);
 function draw(){html=render.call(card,{callApi:(...args)=>{calls.push(args);return api(...args);}});}
 draw();
 return {card,calls,listeners,timers,downloads,blobs,elements,clipboardCalls,draw,get html(){return html;},get legacyCalls(){return legacyCalls;},input(id){listeners.input({target:{dataset:{cmFr24Input:'id'},value:id}});},filter(value){listeners.change({target:{dataset:{cmFr24Input:'events'},value}});},click(action,id){listeners.click({target:{closest:()=>({tagName:'BUTTON',dataset:{cmFr24:action,cmFr24Id:id}})},stopPropagation(){},preventDefault(){}});}};
}

test('entry, flight ID fill, filter changes and redraws never query the provider',async()=>{
 const c=setup();
 assert.match(c.html,/FR24-Endpunkt prüfen/);assert.match(c.html,/Noch keine Abfrage gestartet/);
 c.click('fill','42a83e5a');await flush();
 assert.equal(c.card._comatFr24Input,'42a83e5a');assert.equal(c.card._comatOpen.fr24,true);
 c.input('leg-opaque_1');c.filter('all');c.draw();c.draw();
 assert.match(c.html,/value="leg-opaque_1"/);assert.match(c.html,/value="all" selected/);
 assert.equal(c.calls.length,0);assert.equal(c.card._comatData.flights[0].aibt,undefined);
});

test('one explicit click sends ID and event filter without credentials and preserves nested gate values',async()=>{
 const expected=reply();const c=setup({api:async()=>expected});
 c.input(' 42a83e5a ');c.click('query');c.click('query');
 assert.equal(c.calls.length,1,'busy double-click does not duplicate a paid call');
 await flush();
 assert.deepEqual(plain(c.calls[0]),['POST','callwebhook/comatalarm/monitor',{action:'fr24_historic_events',fr24_id:'42a83e5a',event_types:'gate_arrival'}]);
 assert.deepEqual(plain(c.card._comatFr24Result),expected.diagnostic);
 assert.match(c.html,/HTTP 200/);assert.match(c.html,/gate_ident&quot;: null/);assert.match(c.html,/55\.61253/);assert.match(c.html,/12\.63116/);assert.match(c.html,/2023-01-27T06:16:18Z/);
 c.draw();assert.equal(c.calls.length,1,'no automatic re-query on monitor redraw');
 assert.equal(c.card._comatData.flights[0].aibt,undefined,'diagnosis never adopts a flight event');
});

test('all events is the documented all filter; later typing does not relabel the previous response',async()=>{
 const c=setup({api:async(method,path,body)=>{const r=reply(body.fr24_id);r.diagnostic.params.event_types=body.event_types;return r;}});
 c.input('leg-opaque_1');c.filter('all');c.click('query');await flush();
 assert.equal(c.calls[0][2].event_types,'all');
 c.input('another-leg');c.draw();
 assert.match(c.html,/value="another-leg"/);assert.match(c.html,/FR24-ID leg-opaque_1/);
 assert.equal(c.card._comatFr24Result.params.flight_ids,'leg-opaque_1');
});

test('invalid IDs never issue a request; Enter submits a valid ID once',async()=>{
 const c=setup();
 for(const id of ['', 'a,b', '../secret', 'id?token=x', 'x'.repeat(65)]){c.input(id);c.click('query');}
 await flush();
 assert.equal(c.calls.length,0);assert.match(c.html,/FR24-ID eingeben/);
 c.listeners.keydown({target:{dataset:{cmFr24Input:'id'},value:'42a83e5a'},key:'Enter',preventDefault(){}});await flush();
 assert.equal(c.calls.length,1);assert.match(c.html,/HTTP 200/);
});

test('provider error, non-JSON body and truncation remain visible and cannot create HTML',async()=>{
 const r=reply('42a83e5a',429);r.diagnostic.response=null;r.diagnostic.response_text='<html><img src=x onerror=alert(1)>Rate limit</html>';r.diagnostic.message='FR24 HTTP 429';r.diagnostic.response_truncated=true;r.diagnostic.redacted=true;
 const c=setup({api:async()=>r});c.input('42a83e5a');c.click('query');await flush();
 assert.match(c.html,/HTTP 429/);assert.match(c.html,/&lt;html&gt;&lt;img/);assert.doesNotMatch(c.html,/<img src=x/);
 assert.match(c.html,/wegen ihrer Größe gekürzt/);assert.match(c.html,/Zugangsdaten wurden/);
 await c.card._comatFr24Export('copy');await flush();
 const doc=JSON.parse(c.clipboardCalls[0]);assert.equal(doc.http_status,429);assert.equal(doc.response_text,r.diagnostic.response_text);
});

test('copy and JSON download contain the response plus exact request context and do not query again',async()=>{
 const c=setup();c.input('42a83e5a');c.click('query');await flush();
 c.click('copy');await flush();assert.deepEqual(JSON.parse(c.clipboardCalls[0]),reply().diagnostic);
 c.click('json');await flush();
 assert.equal(c.downloads.length,1);assert.match(c.downloads[0].filename,/^comatalarm-fr24-events-42a83e5a-.*\.json$/);
 const blob=c.blobs.get(c.downloads[0].url);assert.equal(blob.type,'application/json;charset=utf-8');
 assert.deepEqual(JSON.parse(await blob.text()),reply().diagnostic);
 assert.equal(c.calls.length,1);assert.match(c.html,/Download gestartet/);
});

test('a legitimate JSON null is displayed and exported as null',async()=>{
 const r=reply();r.diagnostic.response=null;r.diagnostic.response_is_json=true;
 const c=setup({api:async()=>r});c.input('42a83e5a');c.click('query');await flush();
 assert.match(c.html,/<pre[^>]*aria-label="FR24-Antwort">null<\/pre>/);
 assert.doesNotMatch(c.html,/Kein Antwortinhalt/);
 c.click('copy');await flush();
 const copied=JSON.parse(c.clipboardCalls[0]);assert.equal(copied.response,null);assert.equal(copied.response_is_json,true);
});

test('a paused check shows its check time and earliest retry without scheduling a provider call',async()=>{
 const r=reply();Object.assign(r.diagnostic,{http_status:null,response:null,response_is_json:false,retry_after_seconds:30,error:'provider_cooldown',message:'FR24-Abfrage pausiert'});
 const c=setup({api:async()=>r});c.input('42a83e5a');c.click('query');await flush();
 assert.match(c.html,/Keine HTTP-Antwort/);assert.match(c.html,/Prüfzeit \(Berlin\)/);assert.match(c.html,/Prüfzeit \(UTC\)/);
 assert.doesNotMatch(c.html,/Abgerufen \(Berlin\)/);
 assert.match(c.html,/Frühestens erneut abfragen \(Berlin\)/);assert.match(c.html,/2026-11-21 09:30:30/);
 c.draw();assert.equal(c.calls.length,1);
 assert.equal(c.timers.filter(t=>t.ms===30000).length,0,'retry time is informative, not an automatic call');
});

test('blocked clipboard falls back to an escaped manual JSON field',async()=>{
 const r=reply();r.diagnostic.response.extra='</textarea><img src=x onerror=alert(1)>';
 const c=setup({api:async()=>r,clipboard:async()=>{throw new Error('denied');},legacyCopy:false});
 c.input('42a83e5a');c.click('query');await flush();await c.card._comatFr24Export('copy');await flush();
 assert.equal(c.legacyCalls,1);assert.match(c.html,/data-cm-fr24-input="manual"/);assert.match(c.html,/&lt;\/textarea&gt;&lt;img/);assert.doesNotMatch(c.html,/<img src=x/);
 assert.deepEqual(JSON.parse(c.card._comatFr24Manual),r.diagnostic);
});

test('unsupported backend, unauthorized access and timeout do not masquerade as provider results',async()=>{
 for(const [api,pattern] of [[async()=>({ok:true}),/Push-Dienst 1\.2\.35.*Home-Assistant-Integration/],[async()=>{throw {status_code:403};},/Administratorkonto/]]){
  const c=setup({api});c.input('42a83e5a');c.click('query');await flush();assert.match(c.html,pattern);assert.equal(c.card._comatFr24Result,null);
 }
 const c=setup({api:()=>new Promise(()=>{})});c.input('42a83e5a');c.click('query');
 c.timers.find(t=>t.ms===25000).fn();await flush();
 assert.match(c.html,/nicht rechtzeitig/);assert.equal(c.card._comatFr24Result,null);assert.equal(c.card._comatFr24Busy,false);assert.equal(c.calls.length,1);
});
