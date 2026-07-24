"""Spacebot POC web server — pure stdlib, no framework.

  python3 seed.py && python3 server.py     # http://localhost:8080

Login-based, two experiences:
  - End user  -> a clean "ask me anything" chat. Never sees the workflow catalog.
  - Author    -> Studio: add knowledge, catalog, and the demand-ranked gaps.
  - Admin     -> also Model settings (bring your own Claude / OpenAI / local key).

The end user's answer is the model's freshly-worded, simple explanation of what RAG
retrieved — not a dump of the stored text.
"""
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sb import auth, db, ingest, settings
from sb.media import blob as mblob
from sb.media import pipeline as mpipe
from sb.pipeline import ask, ask_stream
from sb.providers import get_provider

PORT = 8080

CSS = """
:root{--bg:#0b1020;--panel:#111a30;--card:#151e37;--line:#26314f;--txt:#e9eefb;--mut:#93a3c8;
--acc:#6ea8ff;--acc2:#8f7dff;--ok:#3ecf8e;--warn:#ffcc66;--bad:#ff6b81}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;font:15px/1.55 system-ui,Segoe UI,Roboto,sans-serif;background:
radial-gradient(1200px 600px at 70% -10%,#16224a 0,transparent 60%),var(--bg);color:var(--txt)}
a{color:var(--acc);text-decoration:none}
.top{display:flex;align-items:center;gap:20px;padding:12px 22px;border-bottom:1px solid var(--line);
position:sticky;top:0;background:rgba(11,16,32,.85);backdrop-filter:blur(8px);z-index:5}
.top .logo{font-weight:800;letter-spacing:.5px}.top nav a{color:var(--mut);margin-right:16px;font-weight:600}
.top nav a.on{color:var(--txt)}.top .right{margin-left:auto;color:var(--mut);font-size:13px;display:flex;gap:14px;align-items:center}
.pill{background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px 12px}
input,textarea,select{width:100%;background:#0d1428;border:1px solid var(--line);color:var(--txt);
border-radius:12px;padding:12px;font:inherit}textarea{min-height:130px;resize:vertical}
button{background:linear-gradient(180deg,var(--acc),#4d8ef0);color:#04122e;border:0;border-radius:12px;
padding:11px 18px;font-weight:700;cursor:pointer}button.ghost{background:transparent;border:1px solid var(--line);color:var(--txt);font-weight:600}
button.mini{padding:6px 12px;font-size:13px;border-radius:999px}
h2{font-size:13px;color:var(--mut);text-transform:uppercase;letter-spacing:.7px;margin:.2em 0 .7em}
.muted{color:var(--mut)}.small{font-size:13px}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;font-weight:700}
.b-high{background:rgba(62,207,142,.16);color:var(--ok)}.b-medium{background:rgba(255,204,102,.16);color:var(--warn)}
.b-low{background:rgba(255,107,129,.16);color:var(--bad)}
"""

LOGIN_HTML = """<!doctype html><meta charset=utf-8><title>Spacebot · sign in</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>""" + CSS + """
.box{max-width:380px;margin:12vh auto;padding:0 20px}.card{background:var(--card);border:1px solid var(--line);
border-radius:18px;padding:26px}.logo{font-size:26px;font-weight:800;text-align:center;margin-bottom:4px}
.row{margin-top:12px}.err{color:var(--bad);font-size:13px;margin-top:10px;min-height:16px}
.hint{margin-top:16px;font-size:12.5px;color:var(--mut);line-height:1.7}</style>
<div class=box><div class=logo>🛰 Spacebot</div>
<p class="muted small" style="text-align:center;margin-top:0">Your team's onboarding brain</p>
<div class=card>
  <div class=row><input id=email placeholder="email" autofocus></div>
  <div class=row><input id=pw type=password placeholder="password"></div>
  <div class=row><button style="width:100%" onclick=go()>Sign in</button></div>
  <div class=err id=err></div>
  <div class=hint>Demo logins:<br>👤 end user — <b>raj@spacelabs.dev</b> / raj123<br>
  ✍ author — <b>sarah@spacelabs.dev</b> / sarah123<br>🛠 admin — <b>admin@spacelabs.dev</b> / admin123</div>
</div></div>
<script>
const $=s=>document.querySelector(s);
$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')go()});
async function go(){
  $('#err').textContent='';
  const r=await fetch('/api/login',{method:'POST',headers:{'content-type':'application/json'},
    body:JSON.stringify({email:$('#email').value.trim(),password:$('#pw').value})});
  const d=await r.json();
  if(d.ok)location.href='/'; else $('#err').textContent=d.error||'Sign in failed';
}
</script>"""

APP_HTML = """<!doctype html><meta charset=utf-8><title>Spacebot</title>
<meta name=viewport content="width=device-width,initial-scale=1"><style>""" + CSS + """
.stage{max-width:860px;margin:0 auto;padding:0 18px;height:calc(100vh - 56px);display:flex;flex-direction:column}
/* chat — Claude-like */
.thread{flex:1;overflow-y:auto;padding:24px 0 8px;scroll-behavior:smooth}
.thread::-webkit-scrollbar{width:10px}.thread::-webkit-scrollbar-thumb{background:#22314f;border-radius:8px}
.hello{margin:13vh auto;text-align:center;max-width:560px}
.hello h1{font-size:30px;margin:0 0 8px;letter-spacing:-.5px}
.chip{display:inline-block;background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:9px 14px;margin:6px 5px 0;font-size:14px;cursor:pointer;transition:.15s}
.chip:hover{border-color:var(--acc);transform:translateY(-1px)}
.turn{display:flex;gap:14px;padding:12px 0;animation:rise .22s ease}
@keyframes rise{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.turn.me{justify-content:flex-end}
.turn .av{width:30px;height:30px;border-radius:9px;flex:none;display:flex;align-items:center;
justify-content:center;font-size:15px;background:linear-gradient(180deg,var(--acc2),var(--acc));color:#04122e}
.turn.me .content{max-width:78%;background:#1a2647;border:1px solid #274069;
border-radius:16px 16px 5px 16px;padding:11px 15px;white-space:pre-wrap}
.turn.bot .content{flex:1;padding-top:4px;line-height:1.72;font-size:15.3px}
.bot .content p{margin:.5em 0}.bot .content p.vok{color:var(--ok);font-size:14px;margin:.25em 0}
.bot .content h3{font-size:16px;margin:.7em 0 .3em}
.bot .content ol,.bot .content ul{margin:.4em 0;padding-left:1.35em}.bot .content li{margin:.3em 0}
.bot .content code{background:#0b1430;border:1px solid var(--line);border-radius:5px;padding:1px 6px;font:13px ui-monospace,monospace}
.bot .content pre{background:#0b1430;border:1px solid var(--line);border-radius:10px;padding:12px 14px;overflow:auto;margin:.6em 0}
.bot .content pre code{background:none;border:0;padding:0}
.cursor{display:inline-block;width:7px;height:15px;background:var(--acc);margin-left:2px;border-radius:2px;
animation:blink 1s steps(2) infinite;vertical-align:-2px}
@keyframes blink{50%{opacity:0}}
.kbox{background:rgba(255,204,102,.1);border:1px solid rgba(255,204,102,.28);border-radius:12px;padding:10px 13px;margin:10px 0}
.src{margin-top:14px;padding-top:10px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px;
display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.acts{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.acts .mini{background:transparent;border:1px solid var(--line);color:var(--mut)}
.acts .mini:hover{color:var(--txt);border-color:var(--acc)}
.composer{padding:8px 0 18px}
.cbar{display:flex;gap:8px;align-items:flex-end;background:#0d1428;border:1px solid var(--line);
border-radius:18px;padding:8px 8px 8px 16px;box-shadow:0 6px 26px rgba(0,0,0,.28)}
.cbar textarea{border:0;background:transparent;padding:8px 0;resize:none;max-height:180px;line-height:1.5}
.cbar textarea:focus{outline:none}.sendbtn{border-radius:13px;padding:10px 16px;flex:none}
.hintline{text-align:center;color:var(--mut);font-size:11.5px;margin-top:8px}
/* author views */
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:22px 0}@media(max-width:760px){.grid{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
.tabs{display:flex;gap:8px;padding:18px 0 0}.tabs button{background:transparent;border:1px solid var(--line);color:var(--mut)}
.tabs button.on{background:var(--card);color:var(--txt)}
.wf{border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}.k{font-family:ui-monospace,monospace;font-size:12px;color:var(--acc)}
.row{margin-top:10px}.gap{border-left:2px solid var(--warn);padding:6px 0 6px 12px;margin:10px 0}
.drop{border:1.5px dashed var(--line);border-radius:12px;padding:24px;text-align:center;color:var(--mut);margin-top:10px;transition:.15s}
.drop.over{border-color:var(--acc);background:rgba(110,168,255,.06)}.link{color:var(--acc);cursor:pointer}
.foot{margin-top:14px;padding-top:10px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px}
</style>
<div class=top><span class=logo>🛰 SPACEBOT</span><nav id=nav></nav>
<div class=right><span id=who></span><span class="pill" id=prov></span><a href=/logout>Sign out</a></div></div>
<div id=root></div>
<script>
const $=(s,r=document)=>r.querySelector(s);
const esc=s=>(s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const jget=async u=>(await fetch(u)).json();
const jpost=async(u,b)=>(await fetch(u,{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(b)})).json();
let ME={};

async function boot(){
  ME=await jget('/api/me');
  $('#who').textContent=ME.name;
  const author=ME.role==='author'||ME.role==='admin';
  $('#prov').textContent=author?('model: '+ME.provider):'';
  let nav='<a href=/ id=n-chat>Chat</a>';
  if(author)nav+='<a href=/studio id=n-studio>Studio</a>';
  if(ME.role==='admin')nav+='<a href=/admin id=n-admin>Settings</a>';
  $('#nav').innerHTML=nav;
  let path=location.pathname;
  if(!author&&(path==='/studio'||path==='/admin')){location.href='/';return;}
  if(path==='/studio')studio();else if(path==='/admin')admin();else chat();
  const cur=path==='/studio'?'studio':path==='/admin'?'admin':'chat';
  const a=$('#n-'+cur);if(a)a.className='on';
}

/* ---------------- CHAT (streaming, Claude-like) ---------------- */
let streaming=null,stick=true;
function chat(){
  $('#root').innerHTML=`<div class=stage>
    <div class=thread id=thread></div>
    <div class=composer>
      <div class=cbar>
        <textarea id=q rows=1 placeholder="Ask me anything…"></textarea>
        <button class=sendbtn id=sbtn onclick=onSend()>Send</button>
      </div>
      <div class=hintline>Spacebot answers only from what your team has documented · Enter to send · Shift+Enter for a new line</div>
    </div></div>`;
  greeting();
  const q=$('#q');q.focus();
  q.addEventListener('input',()=>{q.style.height='auto';q.style.height=Math.min(q.scrollHeight,180)+'px';});
  q.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();onSend();}});
  const t=$('#thread');t.addEventListener('scroll',()=>{stick=(t.scrollHeight-t.scrollTop-t.clientHeight)<90;});
}
function greeting(){
  const first=(ME.name||'there').split(' ')[0];
  $('#thread').innerHTML=`<div class=hello><h1>Hi ${esc(first)} 👋</h1>
   <p class=muted>Ask me anything about working here — I'll walk you through it, step by step.</p>
   <div>${['How do I roll back a production deploy?','What do I do in my first week?','How do I create a purchase order?']
     .map(s=>`<span class=chip onclick="ex(this.textContent)">${s}</span>`).join('')}</div></div>`;
}
window.ex=t=>{$('#q').value=t;onSend();};
function turn(who){
  if($('#thread .hello'))$('#thread').innerHTML='';
  const d=document.createElement('div');d.className='turn '+who;
  d.innerHTML=(who==='bot'?'<div class=av>🛰</div>':'')+'<div class=content></div>';
  $('#thread').appendChild(d);stick=true;scrollDown();
  return $('.content',d);
}
function scrollDown(){const t=$('#thread');if(stick&&t)t.scrollTop=t.scrollHeight;}
function setStreaming(on){const b=$('#sbtn');if(b){b.textContent=on?'Stop':'Send';b.onclick=on?stopStream:onSend;}}
function stopStream(){if(streaming){streaming.abort();streaming=null;}setStreaming(false);}
function onSend(){
  if(streaming)return;
  const qEl=$('#q');const q=qEl.value.trim();if(!q)return;
  qEl.value='';qEl.style.height='auto';
  turn('me').textContent=q;
  askStream(q,'');
}
window.simpler=q=>{if(streaming)return;askStream(q,'Explain even more simply and briefly, as if to someone with zero background. Use different, plainer wording.');};
async function askStream(q,style){
  const content=turn('bot');content.innerHTML='<span class=cursor></span>';
  let md='',meta=null;
  streaming=new AbortController();setStreaming(true);
  try{
    const resp=await fetch('/api/ask/stream',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({question:q,style}),signal:streaming.signal});
    const reader=resp.body.getReader(),dec=new TextDecoder();let buf='';
    for(;;){
      const {value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let idx;
      while((idx=buf.indexOf('\\n\\n'))>=0){
        const ev=parseSSE(buf.slice(0,idx));buf=buf.slice(idx+2);
        if(!ev)continue;
        if(ev.event==='delta'){md+=ev.data;content.innerHTML=mdToHtml(md)+' <span class=cursor></span>';scrollDown();}
        else if(ev.event==='status'){if(!md){content.innerHTML='<span class=muted>'+esc(ev.data)+'</span> <span class=cursor></span>';scrollDown();}}
        else if(ev.event==='meta')meta=ev.data;
      }
    }
    content.innerHTML=mdToHtml(md)+renderMeta(meta);
    const sb=content.querySelector('.js-simpler');if(sb)sb.onclick=()=>simpler(q);
    scrollDown();
  }catch(err){
    if(err.name==='AbortError')content.innerHTML=mdToHtml(md)+' <span class=muted>(stopped)</span>';
    else content.innerHTML='<span class=muted>Something went wrong — try again.</span>';
  }finally{streaming=null;setStreaming(false);}
}
function parseSSE(raw){
  let event='message',data='';
  for(const line of raw.split('\\n')){
    if(line.startsWith('event:'))event=line.slice(6).trim();
    else if(line.startsWith('data:'))data+=line.slice(5).replace(/^ /,'');
  }
  if(!data)return null;try{return {event,data:JSON.parse(data)};}catch(e){return null;}
}
function renderMeta(m){
  if(!m)return '';
  if(m.abstained)return `<div class=src>📌 I don't have this documented yet — I've flagged it so a senior can add it. You'll be covered next time.</div>`;
  const w=m.workflow||{};
  let h=`<div class=src><span class="badge b-${m.band}">${m.band}</span>📎 Based on <b>${esc(w.name||'')}</b>${w.verified_by?` · verified by @${esc(w.verified_by)}`:''}</div>`;
  if(m.alternatives&&m.alternatives.length)h+=`<div style="margin-top:8px">${m.alternatives.map(x=>`<span class=chip onclick="ex('How do I ${esc((x.name||'').toLowerCase())}?')">${esc(x.name)} →</span>`).join('')}</div>`;
  h+=`<div class=acts><button class="mini js-simpler">↻ Explain simpler</button>
    <button class=mini onclick="copyMsg(this)">Copy</button>
    <button class=mini onclick="this.textContent='👍'">👍</button>
    <button class=mini onclick="this.textContent='👎'">👎</button></div>`;
  return h;
}
window.copyMsg=b=>{const c=b.closest('.content').cloneNode(true);
  const s=c.querySelector('.src');if(s)s.remove();const a=c.querySelector('.acts');if(a)a.remove();
  if(navigator.clipboard)navigator.clipboard.writeText(c.innerText.trim());b.textContent='Copied';};
/* tiny markdown -> html (streamed text re-rendered each delta) */
function mdToHtml(md){
  const fences=[];
  md=md.replace(/```(\\w*)\\n?([\\s\\S]*?)```/g,(m,l,c)=>{fences.push(c);return '@@FENCE'+(fences.length-1)+'@@';});
  const lines=md.split('\\n');let out=[],i=0;
  while(i<lines.length){
    const ln=lines[i];
    if(/^\\s*\\d+\\.\\s+/.test(ln)){const it=[];while(i<lines.length&&/^\\s*\\d+\\.\\s+/.test(lines[i])){it.push(lines[i].replace(/^\\s*\\d+\\.\\s+/,''));i++;}out.push('<ol>'+it.map(x=>'<li>'+inline(x)+'</li>').join('')+'</ol>');continue;}
    if(/^\\s*[-*]\\s+/.test(ln)){const it=[];while(i<lines.length&&/^\\s*[-*]\\s+/.test(lines[i])){it.push(lines[i].replace(/^\\s*[-*]\\s+/,''));i++;}out.push('<ul>'+it.map(x=>'<li>'+inline(x)+'</li>').join('')+'</ul>');continue;}
    if(/^\\s*#{1,3}\\s+/.test(ln)){out.push('<h3>'+inline(ln.replace(/^\\s*#{1,3}\\s+/,''))+'</h3>');i++;continue;}
    if(ln.trim()===''){i++;continue;}
    const para=[ln];i++;
    while(i<lines.length&&lines[i].trim()!==''&&!/^\\s*(\\d+\\.|[-*]|#{1,3})\\s+/.test(lines[i])){para.push(lines[i]);i++;}
    const text=para.join(' ');const cls=text.trim().indexOf('✓')===0?' class=vok':'';
    out.push('<p'+cls+'>'+inline(text)+'</p>');
  }
  return out.join('').replace(/@@FENCE(\\d+)@@/g,(m,n)=>'<pre><code>'+esc(fences[+n])+'</code></pre>');
}
function inline(s){
  s=esc(s);
  s=s.replace(/`([^`]+)`/g,'<code>$1</code>');
  s=s.replace(/\\*\\*([^*]+)\\*\\*/g,'<b>$1</b>');
  s=s.replace(/\\*([^*]+)\\*/g,'<i>$1</i>');
  return s;
}

/* ---------------- STUDIO (author) ---------------- */
function studio(){
  $('#root').innerHTML=`<div class=stage style="height:auto;overflow:auto">
    <div class=tabs><button class=on id=t-add onclick="stab('add')">Add knowledge</button>
      <button id=t-cat onclick="stab('cat')">Catalog</button>
      <button id=t-gap onclick="stab('gap')">Knowledge gaps</button></div>
    <div id=sview></div></div>`;
  stab('add');
}
window.stab=t=>{
  ['add','cat','gap'].forEach(x=>{const b=$('#t-'+x);if(b)b.className=x===t?'on':''});
  if(t==='add')return addView();if(t==='cat')return catView();return gapView();
};
let picked=[];
function addView(){
  $('#sview').innerHTML=`<div class=card style="margin-top:16px"><h2>Add knowledge → workflow</h2>
   <p class="muted small">Drop a PDF, one screenshot, a <b>sequence</b> of screenshots, a video, or a transcript —
     any mix. Spacebot decomposes them all into ordered steps through the same pipeline.</p>
   <div class=grid style="padding:0"><input id=wk placeholder="Workflow ID e.g. DEPLOY.CANARY"><input id=wn placeholder="Name"></div>
   <div class=grid style="padding:8px 0"><input id=wc placeholder="Category"><input id=wo placeholder="Owner (you)"></div>
   <div id=drop class=drop>Drag files here, or <label class=link>browse<input id=fin type=file multiple style="display:none"></label>
     <div id=flist class="muted small" style="margin-top:8px"></div></div>
   <textarea id=wt placeholder="…or paste a transcript / notes (optional)" style="margin-top:10px"></textarea>
   <div class=row><button onclick=ingest()>Ingest & build draft</button></div>
   <div id=iout class=row></div></div>`;
  picked=[];
  const fin=$('#fin'),drop=$('#drop');
  fin.addEventListener('change',()=>showFiles(fin.files));
  drop.addEventListener('dragover',e=>{e.preventDefault();drop.classList.add('over');});
  drop.addEventListener('dragleave',()=>drop.classList.remove('over'));
  drop.addEventListener('drop',e=>{e.preventDefault();drop.classList.remove('over');showFiles(e.dataTransfer.files);});
}
function showFiles(fl){picked=[...fl];$('#flist').innerHTML=picked.map(f=>`📎 ${esc(f.name)} <span class=muted>(${Math.round(f.size/1024)} KB)</span>`).join('<br>');}
async function ingest(){
  const wk=$('#wk').value.trim();
  if(!wk){$('#iout').innerHTML='<span class="badge b-low">Workflow ID required</span>';return;}
  if(!picked.length&&!$('#wt').value.trim()){$('#iout').innerHTML='<span class="badge b-low">Add a file or paste text</span>';return;}
  const fd=new FormData();
  fd.append('wf_key',wk);fd.append('name',$('#wn').value);fd.append('category',$('#wc').value);
  fd.append('owner',$('#wo').value);fd.append('text',$('#wt').value);
  picked.forEach(f=>fd.append('file',f,f.name));
  $('#iout').innerHTML='<span class=muted>uploading…</span>';
  const r=await fetch('/api/ingest',{method:'POST',body:fd}).then(x=>x.json());
  if(r.error){$('#iout').innerHTML=`<span class="badge b-low">${esc(r.error)}</span>`;return;}
  pollJob(r.job_id);
}
async function pollJob(id){
  const j=await jget('/api/jobs/'+id);
  if(!j||!j.status){$('#iout').innerHTML='<span class="badge b-low">job lost</span>';return;}
  if(j.status==='drafted'){
    const r=j.result||{};
    $('#iout').innerHTML=`<div class=foot>
      <span class="badge b-high">✓ draft ready</span> ${esc(r.name||r.wf_key)} — <b>${r.step_count}</b> steps from
      <b>${r.segment_count}</b> segments · ${r.images_attached} image(s) attached
      ${r.secrets_flagged?`· <span style="color:var(--warn)">⚠ ${r.secrets_flagged} secret(s) flagged</span>`:''}
      ${r.uncertain&&r.uncertain.length?'<br>⚠ confirm: '+r.uncertain.map(esc).join('; '):''}
      ${r.notes&&r.notes.length?'<br>⏳ awaiting capability: '+r.notes.map(esc).join('; '):''}
      <br><span class=muted>status: in_review — not answerable in chat until you publish.</span>
      <div class=row><button onclick="pubDraft('${esc(r.wf_key)}')">Publish</button>
        <button class=ghost onclick="stab('cat')">View catalog</button></div></div>`;
    return;
  }
  if(j.status==='failed'){$('#iout').innerHTML=`<span class="badge b-low">failed: ${esc(j.error||j.stage)}</span>`;return;}
  $('#iout').innerHTML=`<span class=muted>⏳ ${esc(j.stage||j.status)}…</span>`;
  setTimeout(()=>pollJob(id),700);
}
async function pubDraft(wk){await jpost('/api/workflows/'+wk+'/publish',{});
  $('#iout').innerHTML=`<span class="badge b-high">✓ published — now answerable in chat</span>`;}
async function catView(){
  const c=await jget('/api/catalog');
  $('#sview').innerHTML='<div class="card" style="margin-top:16px"><h2>'+c.length+' workflows</h2>'+
    c.map(w=>`<div class=wf><b>${esc(w.name)}</b> <span class=k>${esc(w.wf_key)}</span>
      ${w.status&&w.status!=='published'?`<span class="badge b-medium">${esc(w.status)}</span> <button class=mini onclick="pubCat('${esc(w.wf_key)}')">Publish</button>`:'<span class="badge b-high">published</span>'}<br>
      <span class=muted>${esc(w.category)} · @${esc(w.owner)} · ${w.step_count} steps · ${w.asset_count} assets</span></div>`).join('')+'</div>';
}
window.pubCat=async wk=>{await jpost('/api/workflows/'+wk+'/publish',{});catView();};
async function gapView(){
  const g=await jget('/api/gaps');
  $('#sview').innerHTML='<div class="card" style="margin-top:16px"><h2>What people asked that we couldn\\'t answer</h2>'+
    '<p class="muted small">Each is a chance to add a workflow. Ranked by most recent.</p>'+
    (g.length?g.map(x=>`<div class=gap>“${esc(x.question)}” <span class="muted small">· ${x.status}</span></div>`).join('')
      :'<p class=muted>No gaps yet — ask something unknown in chat to see one appear.</p>')+'</div>';
}

/* ---------------- ADMIN (settings) ---------------- */
async function admin(){
  const s=await jget('/api/settings');
  $('#root').innerHTML=`<div class=stage style="height:auto"><div class="card" style="margin-top:22px">
   <h2>Model provider</h2>
   <p class="muted small">Local-first. Point Spacebot at your own Ollama, or a hosted model.
     Only this changes — the whole pipeline stays the same.</p>
   <div class=row><select id=p>
     <option value=ollama>Ollama (local)</option>
     <option value=anthropic>Anthropic (Claude)</option>
     <option value=openai>OpenAI / compatible</option>
     <option value=mock>mock (offline heuristic)</option>
     <option value=auto>auto</option></select></div>
   <div class=row><input id=obase placeholder="Ollama base URL (default http://localhost:11434/v1)"></div>
   <div class=row><input id=model placeholder="Model, e.g. llama3.1  ·  qwen2.5  ·  mistral"></div>
   <div class=row><input id=ak placeholder="Anthropic API key (only if using Claude)"></div>
   <div class=row><input id=ok placeholder="OpenAI API key (only if using OpenAI)"></div>
   <div class=row><button onclick=saveAdmin()>Save</button>
     <button class=ghost onclick=testModel()>Test connection</button></div>
   <div class="src" style="margin-top:14px">resolving to <b>${esc(s.resolved_provider)}</b>
     · model <b>${esc(s.compose_model||'—')}</b> · ollama ${esc(s.ollama_base_url||'')}
     · claude ${esc(s.anthropic_api_key_masked||'—')} · openai ${esc(s.openai_api_key_masked||'—')}</div>
   <div id=aout class=row></div></div></div>`;
  $('#p').value=s.llm_provider||'ollama';$('#obase').value=s.ollama_base_url||'';$('#model').value=s.compose_model||'';
}
async function saveAdmin(){
  const model=$('#model').value.trim();
  const b={llm_provider:$('#p').value,ollama_base_url:$('#obase').value.trim(),
    anthropic_api_key:$('#ak').value.trim(),openai_api_key:$('#ok').value.trim()};
  if(model){b.route_model=model;b.compose_model=model;}
  const r=await jpost('/api/settings',b);
  $('#aout').innerHTML=`<span class="badge b-high">✓ saved — resolving to ${esc(r.resolved_provider)}</span>`;
  $('#prov').textContent='model: '+r.resolved_provider;
}
async function testModel(){
  $('#aout').innerHTML='<span class=muted>testing…</span>';
  const h=await jget('/api/model/health');
  if(h.ok&&h.reachable){
    $('#aout').innerHTML=`<span class="badge b-high">✓ reachable at ${esc(h.base)}</span>
      <div class="muted small" style="margin-top:6px">models: ${(h.models||[]).map(esc).join(', ')||'(none pulled yet)'}</div>
      ${h.model_ready?'<div class="small" style="color:var(--ok);margin-top:4px">✓ '+esc(h.compose_model)+' is ready</div>'
        :'<div class="small" style="color:var(--warn);margin-top:4px">⚠ '+esc(h.compose_model)+' not pulled — run: <b>ollama pull '+esc((h.compose_model||'').split(":")[0])+'</b></div>'}`;
  } else if(h.ok){$('#aout').innerHTML=`<span class="badge b-high">✓ ${esc(h.provider||'ok')}</span>`;}
  else {$('#aout').innerHTML=`<span class="badge b-low">✗ unreachable at ${esc(h.base||'')}</span>
    <div class="muted small" style="margin-top:6px">${esc(h.error||'')}<br>${esc(h.hint||'')}</div>`;}
}
boot();
</script>"""


CTYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp",
          "gif": "image/gif", "pdf": "application/pdf"}


def _cd_param(cd, key):
    m = re.search(key + r'="([^"]*)"', cd)
    return m.group(1) if m else None


def parse_multipart(body: bytes, boundary: str) -> list:
    """Minimal multipart/form-data parser (stdlib has none since cgi was removed).
    Good enough for the POC; production uploads go presigned direct-to-bucket instead."""
    parts, delim = [], b"--" + boundary.encode()
    for seg in body.split(delim):
        if not seg or seg.startswith(b"--"):
            continue
        if seg[:2] == b"\r\n":
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if b"\r\n\r\n" not in seg:
            continue
        head, data = seg.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
        cd = headers.get("content-disposition", "")
        parts.append({"name": _cd_param(cd, "name"), "filename": _cd_param(cd, "filename"),
                      "content_type": headers.get("content-type", ""), "data": data})
    return parts


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for h in (extra or []):
            self.send_header(*h)
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, loc, extra=None):
        self.send_response(302)
        self.send_header("Location", loc)
        for h in (extra or []):
            self.send_header(*h)
        self.end_headers()

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}

    def _cookie(self, name):
        raw = self.headers.get("Cookie", "") or ""
        for part in raw.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def _user(self):
        return db.get_session_user(self._cookie(auth.COOKIE))

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/login":
            return self._redirect("/") if self._user() else self._send(200, LOGIN_HTML, "text/html; charset=utf-8")
        if p == "/logout":
            tok = self._cookie(auth.COOKIE)
            if tok:
                db.delete_session(tok)
            return self._redirect("/login", [("Set-Cookie", f"{auth.COOKIE}=; Path=/; Max-Age=0")])

        user = self._user()
        if not user:
            if p.startswith("/api/"):
                return self._send(401, json.dumps({"error": "not signed in"}))
            return self._redirect("/login")

        if p in ("/", "/chat", "/studio", "/admin"):
            return self._send(200, APP_HTML, "text/html; charset=utf-8")
        if p == "/api/me":
            return self._send(200, json.dumps({**{k: user[k] for k in ("email", "name", "role")},
                                               "provider": settings.effective()["resolved_provider"]}))
        if p == "/api/catalog":
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(db.list_workflows()))
        if p == "/api/gaps":
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(db.list_gaps()))
        if p == "/api/settings":
            if not auth.is_admin(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(settings.public_view()))
        if p == "/api/model/health":
            if not auth.is_admin(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(get_provider().health()))
        if p.startswith("/api/jobs/"):
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            return self._send(200, json.dumps(db.get_job(p.rsplit("/", 1)[-1]) or {}))
        if p.startswith("/blob/"):
            key = p[len("/blob/"):]
            try:
                data = mblob.store().get(key)
            except Exception:
                return self._send(404, b"not found", "text/plain")
            ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
            return self._send(200, data, CTYPES.get(ext, "application/octet-stream"))
        return self._send(404, json.dumps({"error": "not found"}))

    # ---- POST ----
    def do_POST(self):
        p = self.path.split("?")[0]

        # multipart ingest is handled before JSON parsing (raw body + auth)
        if p == "/api/ingest":
            user = self._user()
            if not user:
                return self._send(401, json.dumps({"error": "not signed in"}))
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            ctype = self.headers.get("Content-Type", "")
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n)
            boundary = ctype.split("boundary=", 1)[-1].strip().strip('"') if "boundary=" in ctype else ""
            parts = parse_multipart(raw, boundary) if boundary else []
            fields = {pp["name"]: pp["data"].decode("utf-8", "replace")
                      for pp in parts if pp.get("name") and not pp.get("filename")}
            files = [{"filename": pp["filename"], "mime": pp["content_type"], "bytes": pp["data"]}
                     for pp in parts if pp.get("filename")]
            if fields.get("text", "").strip():
                files.append({"filename": "pasted.txt", "mime": "text/plain",
                              "bytes": fields["text"].encode("utf-8")})
            if not fields.get("wf_key", "").strip() or not files:
                return self._send(400, json.dumps({"error": "workflow id and at least one file (or text) required"}))
            job_id = mpipe.start_ingest(fields["wf_key"].strip(), fields.get("name", ""),
                                        fields.get("category", ""), fields.get("owner", "") or user["name"], files)
            return self._send(200, json.dumps({"job_id": job_id}))

        try:
            body = self._body()
        except Exception as e:
            return self._send(400, json.dumps({"error": f"bad json: {e}"}))

        if p == "/api/login":
            u = auth.authenticate(body.get("email", ""), body.get("password", ""))
            if not u:
                return self._send(200, json.dumps({"ok": False, "error": "wrong email or password"}))
            tok = db.create_session(u["id"])
            return self._send(200, json.dumps({"ok": True, "role": u["role"]}),
                              extra=[("Set-Cookie", f"{auth.COOKIE}={tok}; Path=/; HttpOnly; SameSite=Lax")])

        user = self._user()
        if not user:
            return self._send(401, json.dumps({"error": "not signed in"}))

        if p == "/api/ask":
            q = (body.get("question") or "").strip()
            if not q:
                return self._send(400, json.dumps({"error": "question required"}))
            profile = f"{user['name']} (role: {user['role']})"
            return self._send(200, json.dumps(ask(q, profile=profile, style=body.get("style", ""))))

        if p == "/api/ask/stream":
            q = (body.get("question") or "").strip()
            if not q:
                return self._send(400, json.dumps({"error": "question required"}))
            profile = f"{user['name']} (role: {user['role']})"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            def sse(event, data):
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()

            try:
                # live tokens straight from the model as they are generated
                for event, payload in ask_stream(q, profile=profile, style=body.get("style", "")):
                    sse(event, payload)
                sse("done", {})
            except (BrokenPipeError, ConnectionResetError):
                pass          # client hit Stop / navigated away
            return

        if p == "/api/feed":
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            try:
                r = ingest.structure_from_text(body.get("wf_key", "").strip(), body.get("name", ""),
                                               body.get("category", ""), body.get("owner", "") or user["name"],
                                               body.get("text", ""))
                return self._send(200, json.dumps(r))
            except Exception as e:
                return self._send(200, json.dumps({"error": str(e)}))

        if p == "/api/settings":
            if not auth.is_admin(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            for k in ("llm_provider", "anthropic_api_key", "openai_api_key", "openai_base_url",
                      "ollama_base_url", "route_model", "compose_model", "vision_model"):
                if k in body and body[k] != "":
                    db.set_setting(k, body[k])
            return self._send(200, json.dumps(settings.public_view()))

        if p.startswith("/api/workflows/") and p.endswith("/publish"):
            if not auth.can_author(user):
                return self._send(403, json.dumps({"error": "forbidden"}))
            db.set_workflow_status(p.split("/")[3], "published")
            return self._send(200, json.dumps({"ok": True}))

        return self._send(404, json.dumps({"error": "not found"}))


def main():
    db.init_db()
    n = len(db.get_catalog())
    print(f"Spacebot on http://localhost:{PORT}   ({n} workflows)")
    if n == 0 or not db.get_user_by_email("raj@spacelabs.dev"):
        print("  tip: run  python3 seed.py  first (loads workflows + demo logins)")
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
