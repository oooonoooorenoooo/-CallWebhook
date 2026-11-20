// Exercise the shipped template's projections and browser actions with bounded,
// synthetic snapshots. No provider, HA, clipboard or network access is required.
// Run: node homeassistant/dashboards/test-monitor-exports.cjs
const {spawnSync}=require('node:child_process');
const {resolve}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const {test}=require('node:test');
const extracted=spawnSync('python3',['-c',"import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['cards'][0]['custom_fields']['content'][3:-3])",resolve(__dirname,'comatalarm.yaml')],{encoding:'utf8'});
assert.equal(extracted.status,0,extracted.stderr);
const now=Date.parse('2026-11-20T00:30:00Z'),seconds=now/1000,day='2026-11-20';
class Clock extends Date{constructor(...args){super(...(args.length?args:[now]));}static now(){return now;}}
const plain=value=>JSON.parse(JSON.stringify(value));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function fixture(){
 const flight={number:'LH194',day,origin:'FRA',destination:'BER',registration:'D-AIDL',fr24ID:'3cafe123',category:'completed',ata:seconds-2400,eibt:seconds-1800,aibt:null,stand:null,gate:'B13',packets:{in:3,out:2},schedule:{sta:seconds-2700},live:{},subscribers:[]};
 const events=[
  {timestamp:seconds-30,kind:'error',flight:'LH194',device:'Gerät abc123',message:'FR24: HTTP 429; Anbieterabfrage fehlgeschlagen'},
  {timestamp:seconds-60,kind:'push_failed',flight:'LH194',device:'Gerät abc123',message:'APNs: HTTP 503; erneuter Versuch'},
  {timestamp:seconds-90,kind:'push_accepted',flight:'LH194',device:'Gerät abc123',message:'alarmLanded: Apple angenommen'},
  {timestamp:seconds-120,kind:'event',flight:'LH194',device:'Gerät abc123',message:'Landung bestätigt; AIBT noch nicht übernommen'},
  {timestamp:seconds-150,kind:'service',message:'Pi-Abfragezyklus gestartet'}
 ];
 return {generated_at:seconds-10,started_at:seconds-3600,cycle_started:seconds-20,cycle_finished:seconds-15,api_status:{checked_at:seconds-40,results:{FR24:'HTTP 429'}},pushes:{pending:1,accepted:2,expired:0},stand_model:{ready:true},devices:[],events,flights:[flight],history:{day,month:'2026-11',available_days:[day],flights:[flight],events:[{timestamp:Date.parse('2026-11-19T23:05:00Z')/1000,kind:'event',flight:'LH194',message:'Eintrag am Berliner Kalendertag'}]}};
}
function setup({data=fixture(),secure=true,clipboard=async()=>{},legacyCopy=true}={}){
 const listeners={},apiCalls=[],clipboardCalls=[],downloads=[],timers=[],elements=[];let html='',legacyCalls=0;
 const document={activeElement:{focus(){}},body:{appendChild(){}},createElement(tag){
  const el={tagName:tag.toUpperCase(),style:{},value:'',readOnly:false,focus(){},select(){this.selected=true;},setSelectionRange(){},remove(){this.removed=true;},click(){if(tag==='a')downloads.push({filename:this.download,url:this.href});}};
  elements.push(el);return el;
 },execCommand(command){assert.equal(command,'copy');legacyCalls++;return legacyCopy;}};
 const blobs=new Map(),revoked=[];
 const card={isConnected:true,_comatData:data,_comatDay:day,_comatReceived:now,_comatFetchAt:now,shadowRoot:{addEventListener:(name,fn)=>listeners[name]=fn,appendChild(){}},requestUpdate(name){if(name==='_config')queueMicrotask(draw);}};
 const context=vm.createContext({Date:Clock,Intl,Promise,encodeURIComponent,console,Blob,document,isSecureContext:secure,navigator:{clipboard:{writeText:async text=>{clipboardCalls.push(text);return clipboard(text);}}},URL:{createObjectURL(blob){const url='blob:export-'+blobs.size;blobs.set(url,blob);return url;},revokeObjectURL:url=>revoked.push(url)},setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){}});
 const render=vm.runInContext('(function(hass){'+extracted.stdout+'})',context);
 function draw(){html=render.call(card,{callApi:(...args)=>{apiCalls.push(args);return Promise.resolve(data);}});}
 draw();
 return {card,data,draw,blobs,revoked,timers,elements,downloads,clipboardCalls,apiCalls,get legacyCalls(){return legacyCalls;},get html(){return html;},click(scope,mode){listeners.click({target:{closest:()=>({tagName:'BUTTON',dataset:{cmExport:scope,cmExportMode:mode}})},stopPropagation(){},preventDefault(){}});}};
}

test('each exported log contains the same kind filter as its displayed section',()=>{
 const c=setup();
 assert.deepEqual(plain(c.card._comatBuildExport('events').events.map(e=>e.kind)),['event','service']);
 assert.deepEqual(plain(c.card._comatBuildExport('pushes').events.map(e=>e.kind)),['push_failed','push_accepted']);
 assert.deepEqual(plain(c.card._comatBuildExport('errors').events.map(e=>e.kind)),['error','push_failed']);
 assert.equal(c.card._comatBuildExport('diagnosis').event_count,5);
 for(const scope of ['events','pushes','errors','history','diagnosis'])assert.match(c.html,new RegExp('data-cm-export="'+scope+'"'));
 assert.equal(c.card._comatBuildExport('not-an-export'),null);
 assert.equal(c.apiCalls.length,0,'export controls do not add monitor/provider requests');
});

test('day exports preserve Berlin midnight, UTC, data age, period and selected-day identity',()=>{
 const c=setup(),doc=plain(c.card._comatBuildExport('history'));
 assert.equal(doc.filter.day,day);
 assert.equal(doc.timezone,'Europe/Berlin');
 assert.equal(doc.events[0].time.utc,'2026-11-19T23:05:00.000Z');
 assert.match(doc.events[0].time.berlin,/2026-11-20 00:05:00/);
 assert.equal(doc.source.snapshot_at.unix,seconds-10);
 assert.equal(doc.exported_at.unix,seconds);
 assert.equal(doc.source.stale,false);
 c.card._comatError='Unreachable';c.draw();
 assert.equal(c.card._comatBuildExport('history').source.stale,true);
 c.card._comatDay='2026-11-19';c.draw();
 assert.equal(c.card._comatBuildExport('history'),null,'do not export a previous response under a newly selected day');
 const pending=plain(c.card._comatBuildExport('diagnosis').selected_history);
 assert.deepEqual(pending,{day:'2026-11-19',loaded:false,flights:[],events:[]});
 assert.doesNotMatch(c.html,/data-cm-export="history"/);
});

test('sharing projects allowlisted fields and removes injected credentials, raw bodies and account identifiers',()=>{
 const data=fixture();
 data.api_token='PRIVATE_API_KEY';data.firebase_uid='PRIVATE_UID';data.headers={Authorization:'PRIVATE_HEADER'};
 data.api_status.results.UnknownProvider='PRIVATE_UNKNOWN_PROVIDER';
 data.flights[0].api_token='PRIVATE_FLIGHT_TOKEN';data.flights[0].live.authorization='PRIVATE_LIVE_HEADER';data.flights[0].packets.password='PRIVATE_PACKET_PASSWORD';
 data.history.private='PRIVATE_HISTORY';data.devices=[{name:'PRIVATE_DEVICE',token_present:true}];
 data.events.push(
  {timestamp:seconds,kind:'error',device:'PRIVATE_DEVICE_UID',message:'authorization=secretA password="secretB" firebase_uid=secretC api_token=secretD'},
  {timestamp:seconds,kind:'error',message:'Provider response_body={"hidden":"PRIVATE_BODY"}'},
  {timestamp:seconds,kind:'error',message:'<html>PRIVATE_HTML</html>'},
  {timestamp:seconds,kind:'error',message:'Bearer secretBearer '+ 'b'.repeat(64)+' https://service.invalid/?key=secretURL user@example.invalid'},
 );
 const c=setup({data}),doc=plain(c.card._comatBuildExport('diagnosis')),text=JSON.stringify(doc);
 assert.doesNotMatch(text,/PRIVATE_|secret[A-Z]|user@example|service\.invalid|b{64}/);
 assert.match(text,/Gerät anonymisiert/);
 assert.match(text,/Rohinhalt entfernt/);
 assert.match(text,/FR24: HTTP 429/,'the useful bounded error category survives');
 assert.equal(doc.flights[0].aibt,undefined);
 assert.equal(doc.flights[0].times.aibt,null,'EIBT is not manufactured into AIBT');
 assert.equal(doc.flights[0].stand,'','gate is not manufactured into a confirmed stand');
 assert.equal(doc.flights[0].gate,'B13');
 assert.equal(doc.flights[0].fr24_id,'3cafe123');
});

test('user click copies readable UTF-8 text and reports clipboard success without HTTP calls',async()=>{
 const c=setup();c.click('pushes','copy');await flush();
 assert.equal(c.clipboardCalls.length,1);assert.equal(c.legacyCalls,0);
 assert.match(c.clipboardCalls[0],/^ComatAlarm · Push-Versand\n/);
 assert.match(c.clipboardCalls[0],/2026-11-20.*Europe\/Berlin/s);
 assert.match(c.clipboardCalls[0],/keine iPhone-Zustellung oder Lesebestätigung/);
 assert.match(c.clipboardCalls[0],/Landung: Apple angenommen/);
 assert.doesNotMatch(c.clipboardCalls[0],/Pi-Abfragezyklus gestartet/);
 assert.match(c.html,/Push-Versand kopiert/);
 assert.equal(c.apiCalls.length,0);
});

test('clipboard denial and insecure HA origins fall back to a selected textarea',async()=>{
 for(const secure of [true,false]){
  const c=setup({secure,clipboard:async()=>{throw new Error('permission denied');}});
  await c.card._comatExport('errors','copy');await flush();
  assert.equal(c.clipboardCalls.length,secure?1:0);
  assert.equal(c.legacyCalls,1);assert.ok(c.elements.some(e=>e.tagName==='TEXTAREA'&&e.selected&&e.removed));
  assert.match(c.html,/Fehlerprotokoll kopiert/);
 }
});

test('both clipboard methods blocked show an escaped manual copy field and honest error',async()=>{
 const data=fixture();data.events[3].message='Text </textarea><img src=x onerror=alert(1)>';
 const c=setup({data,clipboard:async()=>{throw new Error('denied');},legacyCopy:false});
 await c.card._comatExport('events','copy');await flush();
 assert.match(c.html,/Automatisches Kopieren ist hier gesperrt/);
 assert.match(c.html,/<textarea class="export-manual" readonly/);
 assert.match(c.html,/&lt;\/textarea&gt;&lt;img/);
 assert.doesNotMatch(c.html,/<img src=x/);
 assert.match(c.card._comatExportManual.text,/Text <\/textarea>/,'manual text retains plain content without becoming markup');
});

test('text and JSON downloads use local UTF-8 blobs, fixed safe filenames and bounded lifetime',async()=>{
 const c=setup();
 for(const mode of ['txt','json']){
  c.click(mode==='txt'?'history':'diagnosis',mode);await flush();
  const download=c.downloads.at(-1),blob=c.blobs.get(download.url),content=await blob.text();
  assert.equal(blob.type,mode==='txt'?'text/plain;charset=utf-8':'application/json;charset=utf-8');
  assert.match(download.filename,new RegExp('^comatalarm-'+(mode==='txt'?'history-2026-11-20':'diagnosis-aktuell')+'-2026-11-20T00-30-00-000Z\\.'+mode+'$'));
  if(mode==='json'){
   const doc=JSON.parse(content);assert.equal(doc.schema,'comatalarm.diagnostics.v1');assert.equal(doc.selected_history.day,day);assert.equal(doc.event_count,5);
  }else{assert.match(content,/Ereignisse am ausgewählten Tag/);assert.match(content,/2026-11-20 00:05:00/);}
 }
 assert.equal(c.downloads.length,2);assert.equal(c.apiCalls.length,0);
 for(const timer of c.timers.filter(t=>t.ms===30000))timer.fn();
 assert.equal(c.revoked.length,2);assert.ok(c.elements.filter(e=>e.tagName==='A').every(e=>e.removed));
 assert.match(c.html,/Download gestartet/);
 assert.doesNotMatch(c.html,/Datei gespeichert/);
 await c.card._comatExport('../unsafe','txt');await c.card._comatExport('events','html');
 assert.equal(c.downloads.length,2);
});

test('empty log downloads retain filter context without inventing an error or flight',async()=>{
 const data=fixture();data.events=[];const c=setup({data});
 await c.card._comatExport('errors','txt');
 const text=await c.blobs.get(c.downloads[0].url).text();
 assert.match(text,/Einträge: 0/);assert.match(text,/Fehler und fehlgeschlagener Push-Versand/);
 assert.match(text,/Keine Einträge in dieser Auswahl/);assert.doesNotMatch(text,/LH194/);
});
