// Use a real button-card bundle and browser, including its disabled styles and
// action-handler. Node-only template tests cannot detect these DOM interactions.
// BUTTON_CARD_BUNDLE=/path/button-card.js CHROMIUM_PATH=/path/chrome node this-file
const {chromium}=require('playwright');
const {spawnSync}=require('node:child_process');
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
   window.calls=[];window.posts=[];window.flightDeleted=false;window.arrivalTracked=false;
   const now=Date.now()/1000,today=new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Berlin'}).format(new Date());
   const card=document.createElement('button-card');
   window.testCard=card;
   card.setConfig(config);
   card.hass={states:{},config:{unit_system:{length:'km'}},locale:{language:'de'},themes:{darkMode:true},user:{is_admin:true},
    callApi:async(method,path,body)=>{
     if(method==='POST'){
      window.posts.push(body);
      if(body.action==='delete')window.flightDeleted=true;
      if(body.action==='track')window.arrivalTracked=true;
      return {ok:true,message:'Aktion gespeichert'};
     }
     window.calls.push(path);await new Promise(r=>setTimeout(r,30));
     const day=decodeURIComponent(path.split('day=')[1]);
     return {generated_at:now,cycle_started:now,cycle_finished:now,api_status:{results:{FR24:'OK'}},pushes:{pending:0,accepted:1,expired:0},stand_model:{ready:true},devices:[],events:[],flights:window.flightDeleted?[]:[{key:'a'.repeat(64),number:'LH186',day:today,category:'active',origin:'FRA',destination:'BER',live:{},schedule:{},subscribers:[]}],arrivals:{day:today,fetched_at:now,flights:[{key:'b'.repeat(64),number:'EW207',airline:'Eurowings',origin:'CGN',origin_name:'Köln',reference:now+3600,schedule:{sta:now+3600},tracked:window.arrivalTracked,available:true}]},history:{day,available_days:[today],flights:[{key:'c'.repeat(64),number:'LH194',day,origin:'FRA',destination:'BER',registration:'D-AISP',category:'completed',stand:'B06',aibt:now-3600,live:{alt:100,gspeed:20},schedule:{},subscribers:[{device:'Gerät abc',status:'Registrierung abgelaufen'}]}],events:[]} };
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
  console.log('PASS real button-card: '+(mobile?'touch':'mouse + keyboard')+', nested details, history, clean archive, add monitoring and confirmed deletion');
  await page.close();
 }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
