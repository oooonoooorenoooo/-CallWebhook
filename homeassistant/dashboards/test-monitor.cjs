// Functional regression checks for the embedded button-card template.
// Run from repository root: node homeassistant/dashboards/test-monitor.cjs
const {spawnSync}=require('node:child_process');
const {readFileSync}=require('node:fs');
const {resolve}=require('node:path');
const vm=require('node:vm');
const assert=require('node:assert/strict');
const yaml=resolve(__dirname,'comatalarm.yaml');
const extract=spawnSync('python3',['-c',"import yaml,sys; print(yaml.safe_load(open(sys.argv[1]))['cards'][0]['custom_fields']['content'][3:-3])",yaml],{encoding:'utf8'});
assert.equal(extract.status,0,extract.stderr);
const today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Berlin'}).format(new Date());
const now=Date.now()/1000;
const sample=day=>({schema:2,generated_at:now,started_at:now,cycle_finished:now,api_status:{results:{FR24:'OK'}},pushes:{pending:0,accepted:1,expired:0},stand_model:{ready:true},devices:[],events:[],flights:[{number:'LH186',day:today,category:'active',live:{},schedule:{},subscribers:[]}],history:{day,month:today.slice(0,7),available_days:[today],flights:[],events:[]}});
const flush=()=>new Promise(r=>setImmediate(r));
function setup(api){
 const timers=new Map(),listeners={},calls=[];let id=0,markup='',renders=0;
 const card={isConnected:true,shadowRoot:{addEventListener:(kind,fn)=>listeners[kind]=fn},
   // button-card refuses bare invalidations in shouldUpdate; expose this contract.
   requestUpdate(name){if(name==='_config')queueMicrotask(draw);}};
 const context=vm.createContext({Date,Intl,Promise,encodeURIComponent,console,
   setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)});
 const render=vm.runInContext('(function(hass){'+extract.stdout+'})',context);
 function draw(){renders++;markup=render.call(card,{callApi:(method,path)=>{calls.push(path);return api(path);}});}
 draw();return {card,timers,listeners,calls,draw,get html(){return markup;},get renders(){return renders;}};
}
(async()=>{
 const c=setup(path=>Promise.resolve(sample(decodeURIComponent(path.split('day=')[1]))));
 assert.match(c.html,/Verbindung zum Pi wird hergestellt/);
 await flush();
 assert.doesNotMatch(c.html,/Verbindung zum Pi wird hergestellt/,'first response must repaint without tab navigation');
 assert.match(c.html,/LH186/);
 assert.equal(c.calls.length,1,'redrawing must not start an extra fetch');
 assert.doesNotMatch(c.html,/<details[^>]*\sopen[ >]/,'every section and flight starts collapsed');
 for(const title of ['Wartende Flüge','Aktive Flüge','Abgeschlossene Flüge','API-Status','Ereignisse & Push-Versand','Fehlerprotokoll','Geräte & Übergabe'])assert.ok(c.html.includes(title.replaceAll("&","&amp;")),title);
 c.listeners.toggle({target:{isConnected:true,matches:()=>true,dataset:{cmKey:'active'},open:true}});
 c.draw();assert.match(c.html,/data-cm-key="active" open/,'refresh must preserve expanded state');
 const chosen=today.slice(0,8)+(today.endsWith('01')?'02':'01');
 c.listeners.click({target:{closest:()=>({dataset:{cmDay:chosen}})},stopPropagation(){}});
 await flush();await flush();
 assert.ok(c.calls.at(-1).endsWith(chosen));
 assert.match(c.html,new RegExp('data-cm-day="'+chosen+'"[^>]*aria-pressed="true"'));
 assert.doesNotMatch(c.html,/Verlauf wird geladen/);
 const failed=setup(()=>Promise.reject({status_code:403}));await flush();
 assert.match(failed.html,/Administratorkonto/,'failure must replace initial loading message');
 const hung=setup(()=>new Promise(()=>{}));
 [...hung.timers.values()].find(t=>t.ms===12000).fn();await flush();
 assert.match(hung.html,/erneuter Versuch erfolgt automatisch/,'hung requests must time out visibly');
 console.log('PASS: immediate first render, collapsed sections, preserved expansion, day selection, errors and timeout');
})().catch(e=>{console.error(e);process.exitCode=1;});
