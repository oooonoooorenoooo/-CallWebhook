// Use a real button-card bundle and browser, including its disabled styles and
// action-handler. Node-only template tests cannot detect these DOM interactions.
// BUTTON_CARD_BUNDLE=/path/button-card.js CHROMIUM_PATH=/path/chrome node this-file
const {chromium}=require('playwright');
const {spawnSync}=require('node:child_process');
const {readFileSync}=require('node:fs');
const {resolve}=require('node:path');
const assert=require('node:assert/strict');
const config=JSON.parse(spawnSync('python3',['-c',"import yaml,json,sys; print(json.dumps(yaml.safe_load(open(sys.argv[1]))['cards'][0]))",process.env.MONITOR_YAML||resolve(__dirname,'comatalarm.yaml')],{encoding:'utf8'}).stdout);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH||undefined,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
 try{
 for(const mobile of [false,true]){
  const page=await browser.newPage({viewport:{width:mobile?390:1440,height:1000},isMobile:mobile,hasTouch:mobile});
  page.setDefaultTimeout(5000);
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.setContent('<html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body style="margin:0;background:#101624"></body></html>');
  await page.evaluate(()=>{window.cardHelpers={};});
  await page.addScriptTag({path:process.env.BUTTON_CARD_BUNDLE});
  await page.evaluate(config=>{
   window.calls=[];window.posts=[];window.flightDeleted=false;window.arrivalTracked=false;window.copiedText='';window.fr24Status=200;
   // Exercise the real textarea selection/click path without using the host clipboard.
   document.execCommand=command=>{
    if(command!=='copy')return false;
    const selected=window.testCard.shadowRoot.activeElement;
    if(!selected||selected.tagName!=='TEXTAREA'||selected.selectionEnd!==selected.value.length)return false;
    window.copiedText=selected.value;return true;
   };
   const now=Date.now()/1000,today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Berlin'}).format(new Date());
   const card=document.createElement('button-card');
   window.testCard=card;
   card.setConfig(config);
   card.hass={states:{},config:{unit_system:{length:'km'}},locale:{language:'de'},themes:{darkMode:true},user:{is_admin:true},
    callApi:async(method,path,body)=>{
     if(method==='POST'){
      window.posts.push(body);
      if(body.action==='fr24_historic_events'){
       await new Promise(r=>setTimeout(r,30));
       return {ok:window.fr24Status===200,diagnostic:{schema:'comatalarm.fr24-events.v1',requested_at:now,completed_at:now,endpoint:'https://fr24api.flightradar24.com/api/historic/flight-events/light',method:'GET',params:{flight_ids:body.fr24_id,event_types:body.event_types},http_status:window.fr24Status,content_type:'application/json',response:window.fr24Status===200?{data:[{fr24_id:body.fr24_id,events:[{type:'gate_arrival',timestamp:'2023-01-27T06:16:18Z',details:{gate_ident:null,gate_lat:55.61253,gate_lon:12.63116}}]}]}:{message:'Rate limit <img src=x onerror=alert(1)>'},response_text:null,error:window.fr24Status===200?null:'upstream_http',message:'FR24 HTTP '+window.fr24Status,response_truncated:false,redacted:false}};
      }
      if(body.action==='delete')window.flightDeleted=true;
      if(body.action==='track')window.arrivalTracked=true;
      return {ok:true,message:'Aktion gespeichert'};
     }
     window.calls.push(path);await new Promise(r=>setTimeout(r,30));
     const day=decodeURIComponent(path.split('day=')[1]);
     return {generated_at:now,cycle_started:now,cycle_finished:now,api_status:{results:{FR24:'OK'}},pushes:{pending:0,accepted:1,expired:0},stand_model:{ready:true},devices:[],api_token:'PRIVATE_TEST_TOKEN',events:[{timestamp:now-10,kind:'event',flight:'LH194',device:'Gerät abc123',message:'Landung bestätigt; AIBT noch nicht übernommen'},{timestamp:now-5,kind:'push_accepted',flight:'LH194',device:'Gerät abc123',message:'alarmLanded: Apple angenommen'},{timestamp:now-1,kind:'error',flight:'LH194',message:'FR24: HTTP 429; Anbieterabfrage fehlgeschlagen'}],flights:window.flightDeleted?[]:[{key:'a'.repeat(64),number:'LH186',day:today,category:'active',origin:'FRA',destination:'BER',live:{},schedule:{},subscribers:[]}],arrivals:{day:today,fetched_at:now,flights:[{key:'b'.repeat(64),number:'EW207',airline:'Eurowings',origin:'CGN',origin_name:'Köln',reference:now+3600,schedule:{sta:now+3600},tracked:window.arrivalTracked,available:true}]},history:{day,available_days:[today],flights:[{key:'c'.repeat(64),number:'LH194',day,fr24ID:'42a83e5a',origin:'FRA',destination:'BER',registration:'D-AISP',category:'completed',stand:'B06',aibt:now-3600,live:{alt:100,gspeed:20},schedule:{},subscribers:[{device:'Gerät abc',status:'Registrierung abgelaufen'}]}],events:[{timestamp:Date.parse(day+'T12:00:00Z')/1000,kind:'event',flight:'LH194',message:'Verlaufseintrag am ausgewählten Tag'}]} };
    }};
   document.body.append(card);
  },config);
  const active=page.locator('details[data-cm-key="active"]');
  await active.waitFor();
  assert.equal(await page.locator('details[open]').count(),0);
  const act=async locator=>mobile?locator.tap():locator.click();
  await act(active.locator(':scope > summary'));
  await page.waitForFunction(()=>window.testCard.shadowRoot.querySelector('[data-cm-key="active"]').open);
  const flight=active.locator('details.flight');
  await act(flight.locator(':scope > summary'));
  assert.equal(await flight.evaluate(e=>e.open),true,'nested flight opens');
  await page.evaluate(()=>window.testCard.requestUpdate('_config'));
  await page.waitForTimeout(80);
  assert.equal(await active.evaluate(e=>e.open),true,'group stays open on refresh');
  assert.equal(await flight.evaluate(e=>e.open),true,'flight stays open on refresh');
  await act(flight.locator(':scope > summary'));
  assert.equal(await flight.evaluate(e=>e.open),false,'flight closes');
  for(const key of ['waiting','completed','api','events','errors','devices','history']){
   const section=page.locator(`details[data-cm-key="${key}"]`);
   await act(section.locator(':scope > summary'));
   assert.equal(await section.evaluate(e=>e.open),true,key+' opens');
  }
  const dayButton=page.locator('button[data-cm-day]').first();
  const selected=await dayButton.getAttribute('data-cm-day');
  await act(dayButton);
  await page.waitForFunction(day=>window.calls.some(p=>p.endsWith(day)),selected);
  await page.waitForTimeout(80);
  assert.equal(await dayButton.getAttribute('aria-pressed'),'true','day selection works');
  const archive=page.locator('details[data-cm-key="history"] .flight');
  await act(archive.locator(':scope > summary'));
  assert.match(await archive.innerText(),/Abgeschlossen · im Verlauf/);
  assert.match(await archive.innerText(),/B06/);
  assert.doesNotMatch(await archive.innerText(),/Registrierung abgelaufen|Positionsdaten|Geschwindigkeit|Distanz|Bis ETA|letzter Zyklus|App-Datenabruf/);
  const readsBeforeExport=await page.evaluate(()=>window.calls.length),postsBeforeExport=await page.evaluate(()=>window.posts.length);
  await act(page.locator('button[data-cm-export="pushes"][data-cm-export-mode="copy"]'));
  await page.waitForFunction(()=>window.copiedText.includes('ComatAlarm · Push-Versand'));
  assert.match(await page.evaluate(()=>window.copiedText),/Landung: Apple angenommen/);
  assert.doesNotMatch(await page.evaluate(()=>window.copiedText),/HTTP 429/);
  assert.match(await page.locator('[data-cm-key="events"]').innerText(),/Push-Versand kopiert/);
  const historyEvents=page.locator('[data-cm-key="history-events"]');
  await act(historyEvents.locator(':scope > summary'));
  const textDownloadPromise=page.waitForEvent('download');
  await act(page.locator('button[data-cm-export="history"][data-cm-export-mode="txt"]'));
  const textDownload=await textDownloadPromise;
  assert.ok(textDownload.suggestedFilename().startsWith('comatalarm-history-'+selected+'-'));
  assert.match(readFileSync(await textDownload.path(),'utf8'),new RegExp('Ausgewählter Berliner Kalendertag · '+selected));
  const jsonDownloadPromise=page.waitForEvent('download');
  await act(page.locator('button[data-cm-export="diagnosis"][data-cm-export-mode="json"]'));
  const jsonDownload=await jsonDownloadPromise;
  const exported=readFileSync(await jsonDownload.path(),'utf8'),diagnosis=JSON.parse(exported);
  assert.equal(diagnosis.selected_history.day,selected);assert.equal(diagnosis.event_count,3);
  assert.doesNotMatch(exported,/PRIVATE_TEST_TOKEN|api_token/);
  assert.equal(await page.evaluate(()=>window.calls.length),readsBeforeExport,'exports use the loaded snapshot');
  assert.equal(await page.evaluate(()=>window.posts.length),postsBeforeExport,'export is not a monitor command');
  // The explicit FR24 check is independent of display refreshes and flight state.
  const fr24=page.locator('details[data-cm-key="fr24"]'),input=fr24.locator('input[data-cm-fr24-input="id"]');
  await act(archive.locator('button[data-cm-fr24="fill"]'));
  await page.waitForFunction(()=>window.testCard.shadowRoot.querySelector('[data-cm-key="fr24"]').open);
  assert.equal(await input.inputValue(),'42a83e5a');
  assert.equal(await page.evaluate(()=>window.posts.filter(x=>x.action==='fr24_historic_events').length),0,'filling or opening never queries FR24');
  await input.fill('42a83e5a');
  await input.evaluate(e=>{e.focus();e.setSelectionRange(2,5);window.testCard.requestUpdate('_config');});
  await page.waitForTimeout(80);
  assert.deepEqual(await input.evaluate(e=>({value:e.value,focused:e.getRootNode().activeElement===e,start:e.selectionStart,end:e.selectionEnd})),{value:'42a83e5a',focused:true,start:2,end:5},'typed ID, focus and selection survive monitor redraw');
  if(mobile)await act(fr24.locator('button[data-cm-fr24="query"]'));else await input.press('Enter');
  await page.waitForFunction(()=>window.testCard._comatFr24Result?.http_status===200);
  assert.equal(await page.evaluate(()=>window.posts.filter(x=>x.action==='fr24_historic_events').length),1);
  const raw=JSON.parse(await fr24.locator('pre').innerText());
  assert.deepEqual(raw.data[0].events[0].details,{gate_ident:null,gate_lat:55.61253,gate_lon:12.63116});
  await act(fr24.locator('button[data-cm-fr24="copy"]'));
  await page.waitForFunction(()=>window.copiedText.includes('comatalarm.fr24-events.v1'));
  const copied=JSON.parse(await page.evaluate(()=>window.copiedText));
  assert.equal(copied.http_status,200);assert.equal(copied.params.flight_ids,'42a83e5a');assert.deepEqual(copied.response,raw);
  const fr24DownloadPromise=page.waitForEvent('download');
  await act(fr24.locator('button[data-cm-fr24="json"]'));
  const fr24Download=await fr24DownloadPromise;
  assert.match(fr24Download.suggestedFilename(),/^comatalarm-fr24-events-42a83e5a-/);
  assert.deepEqual(JSON.parse(readFileSync(await fr24Download.path(),'utf8')),copied);
  await input.fill('another-leg');await page.evaluate(()=>window.testCard.requestUpdate('_config'));
  await page.waitForTimeout(80);
  assert.equal(await input.inputValue(),'another-leg');assert.match(await fr24.innerText(),/FR24-ID 42a83e5a/);
  assert.equal(await page.evaluate(()=>window.posts.filter(x=>x.action==='fr24_historic_events').length),1,'copy, download, typing and refresh do not query');
  await fr24.locator('select').selectOption('all');
  await page.evaluate(()=>{window.fr24Status=429;});
  await act(fr24.locator('button[data-cm-fr24="query"]'));
  await page.waitForFunction(()=>window.testCard._comatFr24Result?.http_status===429);
  assert.match(await fr24.innerText(),/HTTP 429/);
  assert.equal(await page.evaluate(()=>window.posts.filter(x=>x.action==='fr24_historic_events').at(-1).event_types),'all');
  assert.equal(await fr24.locator('img').count(),0,'provider text must not become HTML');
  assert.match(await fr24.locator('pre').innerText(),/Rate limit <img/);
  await fr24.screenshot({path:'/tmp/comat-fr24-'+(mobile?'touch':'desktop')+'.png'});
  await act(fr24.locator(':scope > summary'));
  const arrivals=page.locator('details[data-cm-key="arrivals"]');
  await act(arrivals.locator(':scope > summary'));
  const airline=page.locator('details[data-cm-key="airline:Eurowings"]');
  await act(airline.locator(':scope > summary'));
  await act(airline.locator('button[data-cm-action="track"]'));
  await page.waitForFunction(()=>window.posts.some(x=>x.action==='track'&&x.key==='b'.repeat(64)));
  await page.waitForFunction(()=>window.testCard.shadowRoot.querySelector('[data-cm-action="track"]').disabled);
  assert.match(await airline.innerText(),/Wird überwacht/);
  await act(flight.locator(':scope > summary'));
  await act(flight.locator('button[data-cm-action="delete"]'));
  assert.equal(await page.evaluate(()=>window.posts.filter(x=>x.action==='delete').length),0,'no delete before confirmation');
  await act(flight.locator('button[data-cm-action="cancel"]'));
  assert.equal(await flight.locator('button[data-cm-action="confirm-delete"]').count(),0);
  await act(flight.locator('button[data-cm-action="delete"]'));
  await act(flight.locator('button[data-cm-action="confirm-delete"]'));
  await page.waitForFunction(()=>window.posts.some(x=>x.action==='delete'&&x.key==='a'.repeat(64)));
  await page.waitForFunction(()=>!window.testCard.shadowRoot.querySelector('[data-cm-key="active"] .flight'));
  if(!mobile){
   const summary=active.locator(':scope > summary');await summary.focus();await page.keyboard.press('Enter');
   assert.equal(await active.evaluate(e=>e.open),false,'keyboard closes group');
  }
  assert.deepEqual(errors,[]);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'no horizontal overflow');
  await page.screenshot({path:'/tmp/comat-interaction-'+(mobile?'touch':'desktop')+'.png',fullPage:true});
  console.log('PASS real button-card: '+(mobile?'touch':'mouse + keyboard')+', nested details, history, clean archive, log exports, explicit FR24 query, typed input/focus/selection, HTTP results, raw JSON copy/download, add monitoring and confirmed deletion');
  await page.close();
 }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
