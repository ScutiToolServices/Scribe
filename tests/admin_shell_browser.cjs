/** Dependency-free Chromium regression check. Run: node tests/admin_shell_browser.cjs
 * Node >=22; set CHROMIUM_PATH when Chromium is not installed in Playwright's cache.
 * Only synthetic HTTP fixtures are used; no service process or saved state is opened.
 * Set ADMIN_SCREENSHOT_DIR to export authenticated fixture screenshots.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
const http = require('node:http');
const {spawn} = require('node:child_process');
const {setTimeout: delay} = require('node:timers/promises');

class BrowserPage {
  constructor(socket) {
    this.socket = socket; this.sequence = 0; this.pending = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data), waiting = this.pending.get(message.id);
      if (!waiting) return;
      this.pending.delete(message.id);
      clearTimeout(waiting.timer);
      if (message.error) waiting.reject(new Error(message.error.message)); else waiting.resolve(message.result);
    });
  }
  call(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = ++this.sequence;
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error('CDP timed out: '+method));},10000);
      this.pending.set(id, {resolve,reject,timer});
      this.socket.send(JSON.stringify({id,method,params}));
    });
  }
  async evaluate(fn, arg) {
    const result = await this.call('Runtime.evaluate', {expression:'('+fn.toString()+')('+JSON.stringify(arg)+')',returnByValue:true,awaitPromise:true});
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  async until(fn, message) {
    for(let i=0;i<100;i++){if(await this.evaluate(fn))return;await delay(50);}
    throw new Error(message);
  }
}

async function run() {
  const source = path.resolve(__dirname,'../web');
  const html = await fs.readFile(path.join(source,'index.html'));
  const script = await fs.readFile(path.join(source,'playground.js'));
  const icon = await fs.readFile(path.join(source,'icon.png'));
  const requests=[];
  let unauthorized=false, holdStatus=false, health='ready', pendingStatus=[];
  let tokenRows=[], settingValue=4;
  const slotKeys={Bard:['piper','paradee'],Scribe:['whisper'],Reader:['ocr'],Lens:['ocr'],Mimic:['kitten2','audio8']};
  const backendData=()=>({service:fixture.service,connections:[],backends:slotKeys[fixture.service].map(key=>({key,label:'Model '+key,help:'Synthetic backend',local:false,mode:'remote',url:'',token_set:false,admin:fixture.service==='Mimic'?'Model admin password':null,source:'default',example:'http://model.invalid'}))});
  const settingsData=()=>({settings:[{key:'capacity',kind:'int',value:settingValue,label:'Capacity',help:'Fixture setting',source:'default',minimum:1,maximum:100}]});
  const fixture = {
    service:'Bard', items:[], usage:{uptime_s:120,calls:17,errors:2,avg_ms:42,users:[]},
    connect:{host:'bard',port:29400,paths:[{label:'MCP',method:'POST',path:'/mcp'}]},
    playground:{service:'bard',operations:[{label:'Speak',method:'POST',route:'/api/speak',fields:[{key:'text',label:'Text',type:'textarea'}]}]}
  };
  const server=http.createServer(async(req,res)=>{
    const chunks=[];for await(const chunk of req)chunks.push(chunk);
    const body=Buffer.concat(chunks).toString();
    const record={url:req.url,method:req.method,password:req.headers['x-admin-password'],token:req.headers['x-internal-token'],body,complete:false};
    requests.push(record);res.once('finish',()=>{record.complete=true;});
    res.setHeader('Content-Type','application/json');
    if(req.url==='/admin/'){res.setHeader('Content-Type','text/html');res.end(html);return;}
    if(req.url==='/admin/playground.js'){res.setHeader('Content-Type','text/javascript');res.end(script);return;}
    if(req.url==='/admin/icon.png'){res.setHeader('Content-Type','image/png');res.end(icon);return;}
    if(req.url==='/api/speak'){res.end(JSON.stringify({message:'Fixture request completed'}));return;}
    if(req.url.startsWith('/admin/')&&unauthorized){res.statusCode=401;res.end(JSON.stringify({detail:'Fixture session expired'}));return;}
    if(req.url==='/admin/status'){if(holdStatus)pendingStatus.push(()=>res.end(JSON.stringify(fixture)));else res.end(JSON.stringify(fixture));return;}
    if(req.url==='/admin/backends'||req.url.startsWith('/admin/connections/')){res.end(JSON.stringify(backendData()));return;}
    if(req.url.startsWith('/admin/backends/')){res.end(JSON.stringify(req.url.endsWith('/test')?{ok:true,message:'Fixture connection works'}:backendData()));return;}
    if(req.url==='/admin/settings'){if(req.method==='PUT')settingValue=JSON.parse(body).values.capacity;res.end(JSON.stringify(settingsData()));return;}
    if(req.url==='/admin/tokens'){
      if(req.method==='POST'){tokenRows=[{id:'fixture-id',name:JSON.parse(body).name,created:1700000000}];res.end(JSON.stringify({token:'illustrative-public-token'}));}
      else res.end(JSON.stringify({tokens:tokenRows}));return;
    }
    if(req.url.startsWith('/admin/tokens/')){tokenRows=[];res.end('{}');return;}
    if(req.url==='/health'){
      if(health==='error'){res.statusCode=503;res.end(JSON.stringify({detail:'Synthetic unavailable'}));}
      else res.end(JSON.stringify({ok:true,ready:health==='ready'}));return;
    }
    res.statusCode=404;res.end('{}');
  });
  const profile=await fs.mkdtemp(path.join(os.tmpdir(),'tool-admin-browser-'));
  let chrome,socket;
  try {
    await new Promise((resolve,reject)=>{server.once('error',reject);server.listen(29912,'127.0.0.1',resolve);});
    const executable=process.env.CHROMIUM_PATH || path.join(os.homedir(),'AppData/Local/ms-playwright/chromium_headless_shell-1248/chrome-headless-shell-win64/chrome-headless-shell.exe');
    chrome=spawn(executable,['--headless','--no-sandbox','--disable-gpu','--remote-debugging-port=29913','--user-data-dir='+profile,'about:blank'],{windowsHide:true,stdio:'ignore'});
    let launchError;chrome.on('error',error=>{launchError=error;});
    let tabs;
    for(let i=0;i<50;i++){if(launchError)throw launchError;try{tabs=await(await fetch('http://127.0.0.1:29913/json/list')).json();break;}catch{await delay(100);}}
    assert.ok(tabs?.length,'Chromium did not start; set CHROMIUM_PATH');
    socket=new WebSocket(tabs[0].webSocketDebuggerUrl);
    await new Promise((resolve,reject)=>{socket.addEventListener('open',resolve,{once:true});socket.addEventListener('error',reject,{once:true});});
    const page=new BrowserPage(socket);
    const navigate=async()=>{
      await page.call('Page.navigate',{url:'http://127.0.0.1:29912/admin/'});
      await page.until(()=>document.readyState==='complete' && !!document.getElementById('signin-button'),'Login did not render');
    };
    const login=async()=>{
      await page.evaluate(()=>{document.getElementById('password').value='illustrative-admin';document.getElementById('signin-button').click();});
      await page.until(()=>!document.getElementById('panel')?.hidden,'Login did not complete');
    };
    const select=async id=>page.evaluate(id=>document.querySelector('[data-page="'+id+'"]').click(),id);
    const expectRequest=async(method,url,after=0)=>{
      for(let i=0;i<100;i++){const found=requests.slice(after).findLast(r=>r.method===method&&r.url===url);if(found){assert.equal(found.password,'illustrative-admin','Admin mutation authentication');return JSON.parse(found.body||'{}');}await delay(30);}
      throw new Error('Missing live request '+method+' '+url);
    };
    await navigate();await login();
    assert.equal(await page.evaluate(()=>document.querySelector('[data-page="overview"]')?.getAttribute('aria-current')),'page','Authenticated shell must open Overview');
    for(const service of Object.keys(slotKeys)){
      fixture.service=service;
      if(service!=='Bard'){await page.evaluate(()=>document.getElementById('signout').click());await navigate();await login();}
      await page.until(()=>document.getElementById('health-state')?.textContent.trim()==='Ready','Health should report Ready');
      assert.equal(await page.evaluate(()=>document.getElementById('overview-call-count').textContent.trim()),'17','Overview uses actual call count');
      assert.equal(await page.evaluate(()=>document.querySelectorAll('#backend-summary tr').length),slotKeys[service].length,'Overview lists every actual backend');
      for(const id of ['overview','playground','tokens','backends','settings','usage','connect']){
        await select(id);
        const state=await page.evaluate(()=>({pages:[...document.querySelectorAll('[data-panel]')].filter(p=>!p.hidden).map(p=>p.dataset.panel),headings:document.querySelectorAll('#panel h1').length,title:document.getElementById('page-title').textContent,active:document.querySelector('[aria-current="page"]').dataset.page,hash:location.hash}));
        assert.deepEqual(state.pages,[id],service+' one active page');assert.equal(state.headings,1);assert.ok(state.title.trim(),'Page title is populated');assert.equal(state.active,id);assert.equal(state.hash,'#'+id);
      }
      await select('playground');
      await page.evaluate(()=>{document.getElementById('pg-token').value='illustrative-caller';document.querySelector('#playground textarea').value='Keep this draft';});
      await select('settings');await page.evaluate(()=>{document.getElementById('s-capacity').value='9';});
      await select('backends');await page.evaluate(()=>{document.getElementById('conn-name').value='Draft connection';});
      await select('tokens');await page.evaluate(()=>{document.getElementById('token-name').value='Draft caller';});
      for(const id of ['playground','settings','backends','tokens'])await select(id);
      assert.deepEqual(await page.evaluate(()=>[document.querySelector('#playground textarea').value,document.getElementById('s-capacity').value,document.getElementById('conn-name').value,document.getElementById('token-name').value]),['Keep this draft','9','Draft connection','Draft caller']);
      const expanded=await page.evaluate(()=>{const b=document.getElementById('sidebar-toggle');b.focus();return b.getAttribute('aria-expanded');});
      await page.call('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',unmodifiedText:'\r',windowsVirtualKeyCode:13});
      await page.call('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
      assert.notEqual(await page.evaluate(()=>document.getElementById('sidebar-toggle').getAttribute('aria-expanded')),expanded,'Keyboard collapses sidebar');
      assert.equal(await page.evaluate(()=>document.getElementById('panel').classList.contains('sidebar-collapsed')),expanded==='true');
      for(const width of [320,375,540,768,1024]){
        await page.call('Emulation.setDeviceMetricsOverride',{width,height:900,deviceScaleFactor:1,mobile:false});
        for(const id of ['overview','playground','tokens','backends','settings','usage','connect']){
          await select(id);
          assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,service+' '+id+' overflows at '+width);
        }
      }
    }
    await select('playground');
    await page.evaluate(()=>{document.getElementById('pg-token').value='illustrative-caller';document.querySelector('#playground textarea').value='Public fixture request';document.getElementById('pg-send').click();});
    for(let i=0;i<100&&!requests.some(r=>r.url==='/api/speak');i++)await delay(30);
    const publicRequest=requests.findLast(r=>r.url==='/api/speak');
    assert.ok(publicRequest,'Playground sends a real public API request');
    assert.equal(publicRequest.token,'illustrative-caller');assert.equal(publicRequest.password,undefined,'Public API must not receive admin credentials');
    await page.evaluate(()=>{location.hash='settings';});
    await page.until(()=>!document.querySelector('[data-panel="settings"]').hidden,'Hash navigation did not open settings');
    const collapsed=await page.evaluate(()=>document.getElementById('panel').classList.contains('sidebar-collapsed'));
    await navigate();
    await page.until(()=>!document.getElementById('panel').hidden,'Remembered session did not reopen');
    assert.equal(await page.evaluate(()=>document.getElementById('panel').classList.contains('sidebar-collapsed')),collapsed,'Sidebar preference persists across reload');
    await select('settings');await page.evaluate(()=>{document.getElementById('s-capacity').value='8';document.getElementById('form').requestSubmit();});
    assert.deepEqual(await expectRequest('PUT','/admin/settings'),{values:{capacity:8}});
    await page.until(()=>!document.getElementById('save').disabled,'Settings mutation did not finish');
    // A BFCache restore retains the document, but pagehide has aborted its session controller.
    // A changed server value proves pageshow completed a fresh authenticated load.
    settingValue=7;
    const lifecycleStart=requests.length;
    await page.evaluate(()=>{
      dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
      dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
    });
    for(let i=0;i<100&&!requests.slice(lifecycleStart).some(r=>r.url==='/admin/status'&&r.complete);i++)await delay(30);
    const restoredStatus=requests.slice(lifecycleStart).find(r=>r.url==='/admin/status'&&r.complete);
    assert.ok(restoredStatus,'BFCache restore completes a new status request');
    assert.equal(restoredStatus.password,'illustrative-admin','BFCache restore retains authenticated session');
    await page.until(()=>!document.getElementById('panel').hidden&&document.getElementById('s-capacity')?.value==='7','BFCache restore did not reload usable settings');
    const restoredMutationStart=requests.length;
    await select('settings');await page.evaluate(()=>{document.getElementById('s-capacity').value='9';document.getElementById('form').requestSubmit();});
    assert.deepEqual(await expectRequest('PUT','/admin/settings',restoredMutationStart),{values:{capacity:9}},'Restored controller permits authenticated mutations');
    await page.until(()=>!document.getElementById('save').disabled,'Restored session mutation did not finish');
    if(process.env.ADMIN_SCREENSHOT_DIR){
      const screenshotDirectory=path.resolve(process.env.ADMIN_SCREENSHOT_DIR);
      await fs.mkdir(screenshotDirectory,{recursive:true});
      const capture=async(filename)=>{
        await page.evaluate(()=>document.activeElement?.blur());
        await delay(220); // Let the sidebar's 180 ms layout transition settle before capture.
        const mobile=filename.includes('mobile');
        const shot=await page.call('Page.captureScreenshot',mobile?{captureBeyondViewport:false,clip:{x:0,y:0,width:375,height:900,scale:1}}:{captureBeyondViewport:true});
        await fs.writeFile(path.join(screenshotDirectory,filename),Buffer.from(shot.data,'base64'));
      };
      await page.call('Emulation.setDeviceMetricsOverride',{width:1080,height:1000,deviceScaleFactor:1,mobile:false});
      await page.evaluate(()=>{const toggle=document.getElementById('sidebar-toggle');if(toggle.getAttribute('aria-expanded')==='false')toggle.click();});
      await select('overview');await capture('tool-overview-desktop.png');
      await page.call('Emulation.setDeviceMetricsOverride',{width:375,height:900,deviceScaleFactor:1,mobile:false});
      await page.evaluate(()=>{const toggle=document.getElementById('sidebar-toggle');if(toggle.getAttribute('aria-expanded')==='true')toggle.click();});
      await capture('tool-overview-mobile-collapsed.png');
      await page.evaluate(()=>document.getElementById('sidebar-toggle').click());
      await capture('tool-overview-mobile-expanded.png');
      await page.call('Emulation.setDeviceMetricsOverride',{width:1080,height:1000,deviceScaleFactor:1,mobile:false});
      await select('backends');await page.evaluate(()=>{document.documentElement.style.colorScheme='dark';});
      await capture('tool-connections-dark.png');
    }
    await select('tokens');await page.evaluate(()=>{document.getElementById('token-name').value='Fixture caller';document.getElementById('token-form').requestSubmit();});
    assert.equal((await expectRequest('POST','/admin/tokens')).name,'Fixture caller');
    await page.until(()=>document.getElementById('fresh-token').textContent==='illustrative-public-token','New token not shown');
    await select('backends');await page.evaluate(()=>{document.getElementById('conn-name').value='Fixture connection';document.getElementById('conn-url').value='http://fixture.invalid';document.getElementById('conn-token').value='illustrative-model';document.getElementById('conn-admin').value='illustrative-model-admin';document.getElementById('conn-form').requestSubmit();});
    assert.deepEqual(await expectRequest('PUT','/admin/connections/Fixture%20connection'),{url:'http://fixture.invalid',token:'illustrative-model',admin_password:'illustrative-model-admin'});
    await page.evaluate(()=>{document.getElementById('slot-kitten2-url').value='http://fixture.invalid';document.querySelector('#slots .slot button').click();});
    assert.equal((await expectRequest('PUT','/admin/backends/kitten2')).url,'http://fixture.invalid');
    await page.evaluate(()=>{document.getElementById('conn-token').value='illustrative-unsaved-model';document.getElementById('conn-admin').value='illustrative-unsaved-admin';document.getElementById('signout').click();});
    assert.deepEqual(await page.evaluate(()=>[document.getElementById('pg-token').value,document.getElementById('fresh-token').textContent,sessionStorage.getItem('admin')]),['','',null],'Signout clears credentials');
    assert.equal(await page.evaluate(()=>[...document.querySelectorAll('input[type="password"]')].every(input=>input.value==='')),true,'Signout clears unsaved credential fields');
    assert.equal(await page.evaluate(()=>JSON.stringify(Object.values(localStorage)).includes('illustrative')),false,'Sidebar storage contains no credentials');
    for(const value of ['missing','error']){
      health=value;await navigate();await login();
      const expected=value==='error'?'Unavailable':'Not ready';
      await page.until(()=>['Not ready','Unavailable'].includes(document.getElementById('health-state').textContent.trim()),'Health state did not resolve');
      assert.equal(await page.evaluate(()=>document.getElementById('health-state').textContent.trim()),expected,'Health errors must differ from missing model readiness');
      await page.evaluate(()=>document.getElementById('signout').click());
    }
    health='ready';await navigate();await login();
    unauthorized=true;
    await select('tokens');await page.evaluate(()=>{document.getElementById('token-name').value='Expired caller';document.getElementById('token-form').requestSubmit();});
    await page.until(()=>document.getElementById('panel').hidden,'401 did not clear session');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('admin')),null);
    unauthorized=false;holdStatus=true;
    await page.evaluate(()=>{document.getElementById('password').value='illustrative-admin';document.getElementById('signin-button').click();});
    for(let i=0;i<100&&!pendingStatus.length;i++)await delay(20);
    assert.ok(pendingStatus.length,'Fixture held auth response');
    await page.evaluate(()=>document.getElementById('signout').click());
    holdStatus=false;pendingStatus.splice(0).forEach(release=>release());await delay(250);
    assert.equal(await page.evaluate(()=>document.getElementById('panel').hidden),true,'Pending login must not resurrect signout');
    assert.equal(await page.evaluate(()=>sessionStorage.getItem('admin')),null);
    console.log('Admin shell browser checks passed: five service descriptors, seven pages, responsive layout, drafts, live mutations, BFCache restore, readiness, 401 and signout race');
  } finally {
    socket?.close();
    if(chrome && chrome.exitCode===null){chrome.kill();await new Promise(resolve=>{chrome.once('exit',resolve);setTimeout(resolve,2000);});}
    server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
    const resolvedProfile=path.resolve(profile),resolvedTemporary=path.resolve(os.tmpdir());
    assert.equal(path.dirname(resolvedProfile),resolvedTemporary,'Browser cleanup must stay directly inside the OS temporary directory');
    assert.ok(path.basename(resolvedProfile).startsWith('tool-admin-browser-'),'Browser cleanup requires its unique test profile prefix');
    await fs.rm(resolvedProfile,{recursive:true,force:true,maxRetries:10,retryDelay:100});
  }
}
run().catch(error=>{console.error(error);process.exitCode=1;});
