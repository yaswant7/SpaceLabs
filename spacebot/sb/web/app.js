/* Spacebot client.

   Three views (chat / studio / admin) behind one shell. No framework: the app is small
   enough that a render function per view plus event delegation beats a dependency.

   The one part worth reading carefully is the streaming renderer. Tokens arrive far faster
   than the eye can use, so we accumulate them and repaint on animation frames — repainting
   per token burns CPU, drops text selection, and makes the cursor jitter. */

import { render as md, escapeHtml as esc, setVerifications } from './markdown.js';
import { ICON, logo } from './icons.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
};
const jget = async u => {
  const r = await fetch(u);
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
};
const jpost = async (u, b) => {
  const r = await fetch(u, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(b || {}),
  });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
};

/** Initials for an avatar. "Raj (new hire)" is Raj, not R-N. */
function initials(name) {
  const clean = (name || '').replace(/\([^)]*\)/g, ' ').trim();
  const words = clean.split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  return (words.length === 1 ? words[0].slice(0, 2) : words[0][0] + words[1][0]).toUpperCase();
}

const state = {
  me: null,
  view: 'chat',
  convId: null,
  convs: [],
  messages: [],       // { role, content, meta }
  stream: null,
  stick: true,
  pendingDelete: null, // { conv, index, timer } while the undo toast is up
};

/* ---------------------------------------------------------------- theme -- */
function initTheme() {
  const saved = localStorage.getItem('sb-theme');
  const sys = matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  applyTheme(saved || sys, false);
}
function applyTheme(t, persist = true) {
  document.documentElement.dataset.theme = t;
  if (persist) localStorage.setItem('sb-theme', t);
  const b = $('#themeBtn');
  if (b) {
    b.innerHTML = t === 'light' ? ICON.moon : ICON.sun;
    b.title = t === 'light' ? 'Switch to dark' : 'Switch to light';
    b.setAttribute('aria-label', b.title);
  }
}

/* ----------------------------------------------------------------- boot -- */
async function boot() {
  initTheme();
  try {
    state.me = await jget('/api/me');
  } catch {
    location.href = '/login';
    return;
  }
  const path = location.pathname;
  state.view = path === '/studio' ? 'studio' : path === '/admin' ? 'admin' : 'chat';
  if (!state.me.can_author && state.view !== 'chat') {
    history.replaceState({}, '', '/');
    state.view = 'chat';
  }
  renderShell();
  loadConversations();
  renderView();
}

function renderView() {
  if (state.view === 'studio') studioView();
  else if (state.view === 'admin') adminView();
  else chatView();
}

function go(view, push = true) {
  if (push) history.pushState({ view }, '', view === 'chat' ? '/' : `/${view}`);
  state.view = view;
  paintNav();
  renderView();
}
addEventListener('popstate', () => {
  const p = location.pathname;
  go(p === '/studio' ? 'studio' : p === '/admin' ? 'admin' : 'chat', false);
});

/* ---------------------------------------------------------------- shell -- */
function renderShell() {
  const me = state.me;
  document.body.innerHTML = `
  <div class="app" id="app">
    <div class="scrim" id="scrim"></div>
    <aside class="sidebar">
      <div class="sidebar__head">
        <div class="brand">
          ${logo()}
          <div><div class="brand__name">Spacebot</div><div class="brand__sub">SPACELABS</div></div>
        </div>
        <button class="newchat" id="newChat">${ICON.plus}<span>New chat</span><kbd>⌘K</kbd></button>
      </div>
      <div class="sidebar__scroll">
        <div id="convList"></div>
        <div class="sidebar__label">Workspace</div>
        <nav id="navLinks"></nav>
      </div>
      <div class="sidebar__foot">
        <div class="avatar">${esc(initials(me.name))}</div>
        <div class="who"><b>${esc(me.name)}</b><span>${esc(me.role)} · ${esc(me.provider)}</span></div>
        <button class="iconbtn" id="themeBtn" type="button"></button>
        <a class="iconbtn" href="/logout" aria-label="Sign out" title="Sign out">${ICON.out}</a>
      </div>
    </aside>
    <main class="main">
      <div class="topbar">
        <button class="iconbtn" id="menuBtn" aria-label="Open menu">${ICON.menu}</button>
        <b>Spacebot</b>
      </div>
      <div id="view" style="display:flex;flex-direction:column;flex:1;min-height:0"></div>
    </main>
  </div>`;

  applyTheme(document.documentElement.dataset.theme, false);
  $('#newChat').onclick = () => newChat();
  $('#themeBtn').onclick = () =>
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  $('#menuBtn').onclick = () => $('#app').classList.toggle('nav-open');
  $('#scrim').onclick = () => $('#app').classList.remove('nav-open');
  paintNav();

  addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); newChat(); }
  });
  // A pending delete must not be lost silently if the tab closes — commit it.
  addEventListener('pagehide', () => commitDelete(true));
}

function paintNav() {
  const links = [['chat', 'Chat', ICON.chat]];
  if (state.me.can_author) links.push(['studio', 'Knowledge Studio', ICON.book]);
  if (state.me.is_admin) links.push(['admin', 'Model settings', ICON.gear]);
  const nav = $('#navLinks');
  if (!nav) return;
  nav.innerHTML = links.map(([v, label, icon]) =>
    `<a class="navlink${state.view === v ? ' is-active' : ''}" data-view="${v}"
        role="button" tabindex="0">${icon}${label}</a>`).join('');
  $$('[data-view]', nav).forEach(a => {
    a.onclick = () => { $('#app').classList.remove('nav-open'); go(a.dataset.view); };
  });
}

/* -------------------------------------------------------- conversations -- */
async function loadConversations() {
  try {
    state.convs = await jget('/api/conversations');
  } catch {
    // Keep whatever is on screen. Blanking the list on a transient error looked exactly
    // like "deleting one conversation wiped them all".
    return;
  }
  paintConversations();
}

/** Today / Yesterday / Previous 7 days / Older — cheaper to scan than a flat list. */
function bucketOf(ts) {
  const d = new Date(ts * 1000);
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const days = Math.floor((today - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return 'Previous 7 days';
  return 'Older';
}

function paintConversations() {
  const box = $('#convList');
  if (!box) return;
  if (!state.convs.length) {
    box.innerHTML = '<div class="sidebar__label">Recent</div>' +
      '<div class="sidebar__empty">No conversations yet</div>';
    return;
  }
  let html = '', bucket = null;
  for (const c of state.convs) {
    const b = bucketOf(c.updated_at);
    if (b !== bucket) { bucket = b; html += `<div class="sidebar__label">${b}</div>`; }
    html += `<div class="conv${c.id === state.convId ? ' is-active' : ''}" data-conv="${c.id}"
                  role="button" tabindex="0" title="${esc(c.title || 'New chat')}">
        <span class="conv__title">${esc(c.title || 'New chat')}</span>
        <button class="conv__act" data-rename="${c.id}" title="Rename"
                aria-label="Rename conversation">${ICON.pencil}</button>
        <button class="conv__act conv__del" data-del="${c.id}" title="Delete"
                aria-label="Delete conversation">${ICON.trash}</button>
      </div>`;
  }
  box.innerHTML = html;
}

function newChat() {
  state.convId = null;
  state.messages = [];
  if (state.view !== 'chat') { go('chat'); } else { chatView(); }
  paintConversations();
  $('#q')?.focus();
}

async function openConversation(id) {
  if (state.stream) return;
  if (state.view !== 'chat') go('chat');
  state.convId = id;
  paintConversations();
  try {
    const c = await jget(`/api/conversations/${id}`);
    state.messages = c.messages || [];
  } catch {
    state.messages = [];
  }
  chatView();
}

/* Delete is optimistic with a real undo window: the row disappears at once, and the
   request only goes out once the window closes. Nothing to restore server-side, and an
   accidental click costs nothing. */
/* Rename in place. A prompt() dialog would be two lines of code, but it throws away the
   thing being renamed — you lose sight of the title while typing its replacement — and it
   looks like a browser, not like this app. */
function startRename(id) {
  const row = $(`[data-conv="${CSS.escape(id)}"]`);
  const label = row && $('.conv__title', row);
  if (!label || $('.conv__input', row)) return;

  const current = (state.convs.find(c => c.id === id) || {}).title || '';
  const input = el('input', 'conv__input');
  input.value = current;
  input.setAttribute('aria-label', 'Conversation name');
  label.replaceWith(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (save) => {
    if (settled) return;
    settled = true;
    const next = input.value.trim();
    input.replaceWith(label);
    if (!save || !next || next === current) return;

    const conv = state.convs.find(c => c.id === id);
    const before = conv ? conv.title : '';
    if (conv) { conv.title = next; paintConversations(); }   // optimistic
    try {
      await jpost(`/api/conversations/${encodeURIComponent(id)}/rename`, { title: next });
    } catch {
      if (conv) { conv.title = before; paintConversations(); }
      toast('Could not rename that conversation.');
    }
  };

  input.onkeydown = e => {
    e.stopPropagation();
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  };
  input.onblur = () => finish(true);
  input.onclick = e => e.stopPropagation();      // don't open the conversation underneath
}

function deleteConversation(id) {
  commitDelete();                       // only one pending delete at a time
  const index = state.convs.findIndex(c => c.id === id);
  if (index < 0) return;
  const [conv] = state.convs.splice(index, 1);
  if (state.convId === id) { state.convId = null; state.messages = []; chatView(); }
  paintConversations();

  const timer = setTimeout(() => commitDelete(), 5200);
  state.pendingDelete = { conv, index, timer };
  toast(`Deleted “${conv.title || 'New chat'}”`, 'Undo', () => {
    clearTimeout(timer);
    state.convs.splice(index, 0, conv);
    state.pendingDelete = null;
    paintConversations();
  });
}

function commitDelete(sync = false) {
  const p = state.pendingDelete;
  if (!p) return;
  state.pendingDelete = null;
  clearTimeout(p.timer);
  dismissToast();
  const url = `/api/conversations/${p.conv.id}/delete`;
  if (sync && navigator.sendBeacon) {
    navigator.sendBeacon(url, new Blob(['{}'], { type: 'application/json' }));
    return;
  }
  jpost(url).catch(() => {
    // Server refused — put it back rather than lie about what happened.
    state.convs.splice(p.index, 0, p.conv);
    paintConversations();
    toast('Could not delete that conversation.');
  });
}

let toastEl = null, toastTimer = 0;
function toast(message, actionLabel, onAction) {
  dismissToast();
  toastEl = el('div', 'toast', `<span>${esc(message)}</span>`);
  if (actionLabel) {
    const b = el('button', null, esc(actionLabel));
    b.onclick = () => { dismissToast(); onAction?.(); };
    toastEl.appendChild(b);
  }
  document.body.appendChild(toastEl);
  requestAnimationFrame(() => toastEl?.classList.add('is-in'));
  toastTimer = setTimeout(dismissToast, 5000);
}
function dismissToast() {
  clearTimeout(toastTimer);
  const t = toastEl;
  toastEl = null;
  if (!t) return;
  t.classList.remove('is-in');
  setTimeout(() => t.remove(), 200);
}

/* ----------------------------------------------------------------- chat -- */
function chatView() {
  $('#view').innerHTML = `
    <div class="thread" id="thread"><div class="thread__inner" id="threadInner"></div></div>
    <div class="composer"><div class="composer__inner">
      <div class="cbar">
        <textarea id="q" rows="1" placeholder="Ask me anything about working here…"
          aria-label="Your question"></textarea>
        <button class="send" id="send" aria-label="Send">${ICON.send}</button>
      </div>
      <div class="hintline">Spacebot answers only from what your team has documented ·
        Enter to send, Shift+Enter for a new line</div>
    </div></div>`;

  const thread = $('#thread');
  thread.addEventListener('scroll', () => {
    state.stick = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 100;
  });

  const q = $('#q');
  const autosize = () => {
    q.style.height = 'auto';
    q.style.height = Math.min(Math.max(q.scrollHeight, 38), 200) + 'px';
  };
  q.addEventListener('input', autosize);
  q.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSend(); }
  });
  $('#send').onclick = () => (state.stream ? stopStream() : onSend());

  if (state.messages.length) {
    state.messages.forEach(m => {
      if (m.role === 'user') { addUserTurn(m.content); return; }
      const body = addBotTurn();
      setVerifications(m.meta?.verifications);   // each turn cites its own workflow
      body.innerHTML = `<div class="md">${md(m.content)}</div>` + metaHtml(m.meta);
    });
    setVerifications([]);
    requestAnimationFrame(() => { thread.scrollTop = thread.scrollHeight; });
  } else {
    paintHero();
  }
  q.focus();
}

const SUGGESTIONS = [
  ['Incident', 'The rollback failed with ERR_LEASE_HELD — what do I do?'],
  ['First week', 'What do I need to do in my first week?'],
  ['Deploys', 'How do I roll back a production deploy?'],
  ['Procurement', 'How do I create a purchase order?'],
];

function paintHero() {
  const first = (state.me.name || 'there').replace(/\([^)]*\)/g, '').trim().split(/\s+/)[0];
  $('#threadInner').innerHTML = `
    <div class="hero">
      <div class="hero__top">
        ${logo('lg')}
        <h1>Hi ${esc(first)}</h1>
        <p>Ask me anything about working here. I'll walk you through it step by step —
           and tell you honestly when we haven't documented something yet.</p>
      </div>
      <div class="suggest">
        ${SUGGESTIONS.map(([label, text]) =>
          `<button data-ask="${esc(text)}"><span class="label">${esc(label)}</span>${esc(text)}</button>`
        ).join('')}
      </div>
    </div>`;
}

function addUserTurn(text) {
  $('#threadInner .hero')?.remove();
  $('#threadInner').appendChild(el('div', 'turn turn--me', `<div class="bubble">${esc(text)}</div>`));
  state.stick = true;
  scrollDown();
}

/* Regenerate rewrites the trailing exchange, so it only makes sense on the newest answer —
   both here and on the server, which drops the last assistant message. Retiring those
   buttons on older turns keeps the UI honest about what it can do. */
function retireStaleActions() {
  $$('#threadInner [data-act="regen"], #threadInner [data-act="simpler"]').forEach(b => b.remove());
}

function addBotTurn() {
  $('#threadInner .hero')?.remove();
  retireStaleActions();
  const t = el('div', 'turn turn--bot',
    `<div class="turn__av">${logo('sm')}</div><div class="turn__body"></div>`);
  $('#threadInner').appendChild(t);
  scrollDown();
  return $('.turn__body', t);
}

function scrollDown() {
  const t = $('#thread');
  if (t && state.stick) t.scrollTop = t.scrollHeight;
}

function onSend() {
  const q = $('#q');
  const text = q.value.trim();
  if (!text || state.stream) return;
  q.value = '';
  q.style.height = '38px';
  send(text);
}

function stopStream() {
  if (state.stream) { state.stream.abort(); state.stream = null; }
  setSending(false);
}

function setSending(on) {
  const b = $('#send');
  if (!b) return;
  b.innerHTML = on ? ICON.stop : ICON.send;
  b.classList.toggle('send--stop', on);
  b.setAttribute('aria-label', on ? 'Stop generating' : 'Send');
}

async function send(question, { style = '', regenerate = false } = {}) {
  if (state.stream) return;
  if (!regenerate) addUserTurn(question);

  const body = addBotTurn();
  const mdBox = el('div', 'md');
  const statusBox = el('div', 'status', '<span class="status__dot"></span><span>Thinking…</span>');
  body.appendChild(statusBox);
  body.setAttribute('aria-live', 'polite');
  body.setAttribute('aria-busy', 'true');

  setVerifications([]);        // this answer's checks arrive on the `grounding` event
  let text = '', meta = null, raf = 0, done = false;
  const paint = () => {
    raf = 0;
    mdBox.innerHTML = md(text) + (done ? '' : '<span class="cursor"></span>');
    scrollDown();
  };
  const schedule = () => { if (!raf) raf = requestAnimationFrame(paint); };

  state.stream = new AbortController();
  setSending(true);

  try {
    const resp = await fetch('/api/ask/stream', {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ question, style, regenerate, conversation_id: state.convId }),
      signal: state.stream.signal,
    });
    if (!resp.ok || !resp.body) throw new Error('stream failed');

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done: fin } = await reader.read();
      if (fin) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\n\n')) >= 0) {
        const ev = parseSSE(buf.slice(0, idx));
        buf = buf.slice(idx + 2);
        if (!ev) continue;
        if (ev.event === 'delta') {
          if (statusBox.isConnected) statusBox.replaceWith(mdBox);
          text += ev.data;
          schedule();
        } else if (ev.event === 'status') {
          if (statusBox.isConnected) $('span:last-child', statusBox).textContent = ev.data;
        } else if (ev.event === 'grounding') {
          setVerifications(ev.data.verifications);
        } else if (ev.event === 'meta') {
          meta = ev.data;
        } else if (ev.event === 'conversation') {
          const isNew = state.convId !== ev.data.id;
          state.convId = ev.data.id;
          if (isNew || !state.convs.some(c => c.id === ev.data.id)) loadConversations();
        } else if (ev.event === 'title') {
          /* The model's name for this conversation, sent after the answer. Patched in
             place rather than reloading the list, so the sidebar doesn't flicker while
             they're reading. */
          const c = state.convs.find(x => x.id === ev.data.id);
          if (c) { c.title = ev.data.title; paintConversations(); }
          else loadConversations();
        }
      }
    }
    done = true;
    if (raf) cancelAnimationFrame(raf);
    if (statusBox.isConnected) statusBox.replaceWith(mdBox);
    mdBox.innerHTML = md(text);
    body.insertAdjacentHTML('beforeend', metaHtml(meta));
    if (!regenerate) state.messages.push({ role: 'user', content: question });
    state.messages.push({ role: 'assistant', content: text, meta });
  } catch (err) {
    done = true;
    if (raf) cancelAnimationFrame(raf);
    if (statusBox.isConnected) statusBox.replaceWith(mdBox);
    mdBox.innerHTML = err.name === 'AbortError'
      ? md(text) + '<p class="hintline" style="text-align:left">Stopped.</p>'
      : '<p>Something went wrong reaching the model. Please try again.</p>';
  } finally {
    body.setAttribute('aria-busy', 'false');
    state.stream = null;
    setSending(false);
    scrollDown();
    $('#q')?.focus();
  }
}

function parseSSE(raw) {
  let event = 'message', data = '';
  for (const l of raw.split('\n')) {
    if (l.startsWith('event:')) event = l.slice(6).trim();
    else if (l.startsWith('data:')) data += l.slice(5).replace(/^ /, '');
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; } catch { return null; }
}

/* Answer footer: provenance first (what this is grounded in), then the actions. */
function metaHtml(m) {
  if (!m) return '';
  const acts = `
    <div class="actions">
      <button class="act" data-act="copy">${ICON.copy}Copy</button>
      ${m.abstained ? '' : `<button class="act" data-act="simpler">${ICON.spark}Simpler</button>`}
      <button class="act" data-act="regen">${ICON.redo}Retry</button>
      <button class="act" data-act="up" aria-label="Helpful" title="Helpful">${ICON.up}</button>
      <button class="act" data-act="down" aria-label="Not helpful" title="Not helpful">${ICON.down}</button>
    </div>`;

  if (m.abstained) {
    /* Even when we can't answer, offer the nearest thing we do hold — as a chip rather
       than as prose. Asking the model to include it in the sentence was unreliable: it
       often omitted the suggestion entirely, and when pushed to always include it, wrote
       the workflow's title as a bare line. A chip is worded correctly every time. */
    const near = (m.alternatives || [])
      .map(a => `<button class="chip" data-ask="Tell me about ${esc(a.name || '')}">${esc(a.name)} →</button>`)
      .join('');
    return `<div class="srcbar">
      <div class="srccard"><span class="badge badge--low">not documented</span>
        <span>Logged as a knowledge gap — a senior can turn this into a workflow.</span></div>
      ${near ? `<div class="chips">${near}</div>` : ''}
      ${acts}</div>`;
  }

  const w = m.workflow || {};
  const verified = w.verified_by
    ? ` · verified by <b>@${esc(w.verified_by)}</b>${w.verified_at ? ' on ' + esc(w.verified_at) : ''}`
    : '';
  const chip = t => `<button class="chip" data-ask="${esc(t)}">${esc(t)}</button>`;
  const alts = (m.alternatives || [])
    .map(a => `<button class="chip" data-ask="How do I ${esc((a.name || '').toLowerCase())}?">${esc(a.name)} →</button>`)
    .join('');
  const chips = (m.followups || []).slice(0, 3).map(chip).join('');

  return `<div class="srcbar">
    <div class="srccard">
      <span class="badge badge--${esc(m.band || 'medium')}">${esc(m.band || 'medium')} confidence</span>
      <span>Based on <b>${esc(w.name || 'a workflow')}</b>${verified}</span>
      <button class="disclose" data-act="details">Details</button>
    </div>
    <div class="details" hidden>
      <dl>
        <dt>Workflow</dt><dd>${esc(w.wf_key || '—')}</dd>
        <dt>Category</dt><dd>${esc(w.category || '—')}</dd>
        <dt>Owner</dt><dd>${w.owner ? '@' + esc(w.owner) : '—'}</dd>
        <dt>Steps</dt><dd>${w.step_count ?? '—'}</dd>
        <dt>Confidence</dt><dd>${m.confidence ?? '—'}</dd>
        <dt>Model</dt><dd>${esc(m.provider || '—')}</dd>
      </dl>
    </div>
    ${m.clarify ? `<div class="notice">${esc(m.clarify)}</div>` : ''}
    ${m.degraded ? `<div class="notice">Local model unavailable — ${esc(m.degraded)}.
       This answer was built from the stored workflow text.</div>` : ''}
    ${alts ? `<div class="chips">${alts}</div>` : ''}
    ${chips ? `<div class="chips">${chips}</div>` : ''}
    ${acts}
  </div>`;
}

/* One delegated listener covers everything, including nodes added later. */
document.addEventListener('click', e => {
  const codeBtn = e.target.closest('.copycode');
  if (codeBtn) {
    const code = codeBtn.closest('pre')?.querySelector('code');
    if (code) navigator.clipboard?.writeText(code.textContent);
    flash(codeBtn, 'Copied', 'Copy');
    return;
  }

  const ren = e.target.closest('[data-rename]');
  if (ren) { e.stopPropagation(); startRename(ren.dataset.rename); return; }

  const del = e.target.closest('[data-del]');
  if (del) { e.stopPropagation(); deleteConversation(del.dataset.del); return; }

  const conv = e.target.closest('[data-conv]');
  if (conv) { $('#app').classList.remove('nav-open'); openConversation(conv.dataset.conv); return; }

  const ask = e.target.closest('[data-ask]');
  if (ask && ask.closest('#thread')) { send(ask.dataset.ask); return; }

  const act = e.target.closest('[data-act]');
  if (!act) return;
  const turn = act.closest('.turn');
  const kind = act.dataset.act;

  if (kind === 'details') {
    const d = turn.querySelector('.details');
    d.hidden = !d.hidden;
    act.textContent = d.hidden ? 'Details' : 'Hide';
  } else if (kind === 'copy') {
    const bodyEl = turn.querySelector('.md');
    if (bodyEl) navigator.clipboard?.writeText(bodyEl.innerText.trim());
    const label = act.innerHTML;
    act.textContent = 'Copied';
    setTimeout(() => { act.innerHTML = label; }, 1400);
  } else if (kind === 'up' || kind === 'down') {
    act.classList.add('is-on');
    $$('[data-act="up"],[data-act="down"]', act.parentElement)
      .forEach(b => { if (b !== act) b.classList.remove('is-on'); });
  } else if (kind === 'simpler' || kind === 'regen') {
    const q = lastQuestion();
    const bots = $$('#threadInner .turn--bot');
    if (!q || state.stream || turn !== bots[bots.length - 1]) return;
    turn.remove();
    send(q, {
      regenerate: true,
      style: kind === 'simpler'
        ? 'Explain this even more simply and briefly, as if to someone with zero background. Use different, plainer wording than before.'
        : '',
    });
  }
});

function flash(node, temp, original) {
  node.textContent = temp;
  setTimeout(() => { node.textContent = original; }, 1400);
}

function lastQuestion() {
  const bubbles = $$('.turn--me .bubble');
  return bubbles.length ? bubbles[bubbles.length - 1].textContent : '';
}

/* --------------------------------------------------------------- studio -- */
function studioView() {
  $('#view').innerHTML = `<div class="page"><div class="page__inner">
    <h1>Knowledge Studio</h1>
    <p class="lede">Feed in what the team knows. Everything you add becomes a scoped,
      citable workflow that Spacebot can answer from.</p>
    <div class="tabs" id="stabs">
      <button data-tab="add" class="is-on">Add knowledge</button>
      <button data-tab="cat">Catalog</button>
      <button data-tab="gap">Knowledge gaps</button>
    </div>
    <div id="stab"></div>
  </div></div>`;
  $('#stabs').onclick = e => {
    const b = e.target.closest('[data-tab]');
    if (!b) return;
    $$('button', $('#stabs')).forEach(x => x.classList.toggle('is-on', x === b));
    ({ add: addTab, cat: catalogTab, gap: gapsTab })[b.dataset.tab]();
  };
  addTab();
}

let picked = [];
function addTab() {
  picked = [];
  $('#stab').innerHTML = `<div class="card">
    <h2>Material → workflow</h2>
    <p class="lede">Drop a PDF, a screenshot, a whole sequence of screenshots, a recording,
      or a transcript — any mix. They all decompose through the same pipeline into ordered,
      verifiable steps.</p>
    <div class="field-row">
      <label class="field"><span>Workflow ID</span><input id="wk" placeholder="DEPLOY.CANARY"></label>
      <label class="field"><span>Name</span><input id="wn" placeholder="Run a canary deploy"></label>
    </div>
    <div class="field-row">
      <label class="field"><span>Category</span><input id="wc" placeholder="Deployment"></label>
      <label class="field"><span>Owner</span><input id="wo" placeholder="${esc(state.me.name)}"></label>
    </div>
    <div class="drop" id="drop">
      <b>Drag files here</b> or click to browse
      <input id="fin" type="file" multiple hidden>
      <div class="filelist" id="flist"></div>
    </div>
    <label class="field" style="margin-top:15px"><span>…or paste a transcript / runbook</span>
      <textarea id="wt" placeholder="Paste notes, a call transcript, or a runbook here"></textarea></label>
    <div class="btnrow"><button class="btn" id="goIngest">Ingest &amp; build draft</button></div>
    <div class="result" id="iout"></div>
  </div>`;

  const fin = $('#fin'), drop = $('#drop');
  drop.onclick = e => { if (e.target !== fin) fin.click(); };
  fin.onchange = () => showFiles(fin.files);
  drop.ondragover = e => { e.preventDefault(); drop.classList.add('is-over'); };
  drop.ondragleave = () => drop.classList.remove('is-over');
  drop.ondrop = e => {
    e.preventDefault(); drop.classList.remove('is-over'); showFiles(e.dataTransfer.files);
  };
  $('#goIngest').onclick = runIngest;
}

function showFiles(list) {
  picked = [...list];
  $('#flist').innerHTML = picked.map(f =>
    `<div>${esc(f.name)} · ${Math.max(1, Math.round(f.size / 1024))} KB</div>`).join('');
}

async function runIngest() {
  const wk = $('#wk').value.trim();
  const out = $('#iout');
  if (!wk) { out.innerHTML = '<span class="err">A workflow ID is required.</span>'; return; }
  if (!picked.length && !$('#wt').value.trim()) {
    out.innerHTML = '<span class="err">Add at least one file, or paste some text.</span>'; return;
  }
  const fd = new FormData();
  fd.append('wf_key', wk);
  fd.append('name', $('#wn').value);
  fd.append('category', $('#wc').value);
  fd.append('owner', $('#wo').value);
  fd.append('text', $('#wt').value);
  picked.forEach(f => fd.append('file', f, f.name));

  $('#goIngest').disabled = true;
  out.innerHTML = '<span class="status"><span class="status__dot"></span>Uploading…</span>';
  try {
    const r = await (await fetch('/api/ingest', { method: 'POST', body: fd })).json();
    if (r.error) { out.innerHTML = `<span class="err">${esc(r.error)}</span>`; return; }
    await pollJob(r.job_id);
  } catch {
    out.innerHTML = '<span class="err">Upload failed.</span>';
  } finally {
    $('#goIngest').disabled = false;
  }
}

async function pollJob(id) {
  const out = $('#iout');
  for (;;) {
    const j = await jget('/api/jobs/' + id).catch(() => null);
    if (!j || !j.status) { out.innerHTML = '<span class="err">Job not found.</span>'; return; }
    if (j.status === 'failed') {
      out.innerHTML = `<span class="err">Failed: ${esc(j.error || j.stage || 'unknown')}</span>`; return;
    }
    if (j.status === 'drafted') {
      const r = j.result || {};
      out.innerHTML = `
        <div class="wfrow">
          <div class="wfrow__top"><span class="badge badge--high">draft ready</span>
            <b>${esc(r.name || r.wf_key)}</b> <span class="wfkey">${esc(r.wf_key)}</span></div>
          <div class="wfrow__meta">${r.step_count} steps from ${r.segment_count} segments ·
            ${r.images_attached} image(s) attached
            ${r.secrets_flagged ? ` · ${r.secrets_flagged} possible secret(s) flagged` : ''}</div>
          ${r.uncertain?.length ? `<div class="wfrow__meta">Confirm: ${r.uncertain.map(esc).join('; ')}</div>` : ''}
          ${r.notes?.length ? `<div class="wfrow__meta">Awaiting capability: ${r.notes.map(esc).join('; ')}</div>` : ''}
          <div class="wfrow__meta">Status <b>in review</b> — not answerable in chat until published.</div>
        </div>
        <div class="btnrow"><button class="btn" id="pub">Publish</button></div>`;
      $('#pub').onclick = async () => {
        await jpost(`/api/workflows/${encodeURIComponent(r.wf_key)}/publish`);
        out.innerHTML = '<span class="ok">Published — Spacebot can answer from this now.</span>';
      };
      return;
    }
    out.innerHTML = `<span class="status"><span class="status__dot"></span>${esc(j.stage || j.status)}…</span>`;
    await new Promise(r => setTimeout(r, 700));
  }
}

const ago = ts => {
  if (!ts) return '';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
};

async function catalogTab() {
  const box = $('#stab');
  box.innerHTML = '<div class="card"><div class="empty">Loading…</div></div>';
  const c = await jget('/api/catalog').catch(() => []);
  const live = c.filter(w => w.status === 'published').length;

  box.innerHTML = `<div class="card">
    <h2>${c.length} entries · ${live} answering questions</h2>
    <p class="lede">Everything published here is something Spacebot can answer from. If an
      answer was wrong, this is where you fix or remove the source.</p>
    ${c.length ? c.map(w => `<div class="wfrow" data-row="${esc(w.wf_key)}">
      <div class="wfrow__top">
        <b>${esc(w.name)}</b><span class="wfkey">${esc(w.wf_key)}</span>
        ${w.status === 'published'
          ? '<span class="badge badge--high">published</span>'
          : `<span class="badge badge--medium">${esc(w.status)}</span>
             <button class="act" data-pub="${esc(w.wf_key)}">Publish</button>`}
        <span class="wfrow__acts">
          <button class="act" data-view-wf="${esc(w.wf_key)}">View</button>
          <button class="act" data-edit="${esc(w.wf_key)}">Edit</button>
          <button class="act act--danger" data-del="${esc(w.wf_key)}">Delete</button>
        </span>
      </div>
      <div class="wfrow__meta">${esc(w.category || '—')} · @${esc(w.owner || '—')} ·
        ${w.step_count} steps · ${w.asset_count} assets${
          w.updated_by ? ` · edited by ${esc(w.updated_by)} ${ago(w.updated_at)}` : ''}</div>
      <div class="wfdetail" hidden></div>
    </div>`).join('') : '<div class="empty">Nothing yet. Add knowledge on the first tab.</div>'}
  </div>`;

  const rowFor = key => $(`[data-row="${CSS.escape(key)}"]`, box);

  $$('[data-pub]', box).forEach(b => {
    b.onclick = async () => {
      b.disabled = true;
      await jpost(`/api/workflows/${encodeURIComponent(b.dataset.pub)}/publish`);
      catalogTab();
    };
  });

  $$('[data-view-wf]', box).forEach(b => {
    b.onclick = async () => {
      const key = b.dataset.viewWf, pane = $('.wfdetail', rowFor(key));
      if (!pane.hidden) { pane.hidden = true; return; }
      pane.hidden = false;
      pane.innerHTML = '<div class="empty">Loading…</div>';
      const w = await jget(`/api/workflows/${encodeURIComponent(key)}`).catch(() => null);
      if (!w) { pane.innerHTML = '<div class="empty">Could not load that entry.</div>'; return; }
      pane.innerHTML = `
        ${w.summary ? `<p>${esc(w.summary)}</p>` : ''}
        <dl class="kv">
          <dt>Added by</dt><dd>${esc(w.created_by || '—')}</dd>
          <dt>Last edited</dt><dd>${w.updated_by ? esc(w.updated_by) + ' · ' + ago(w.updated_at) : '—'}</dd>
          <dt>Subjects</dt><dd>${(w.subjects || []).map(esc).join(', ') || '—'}</dd>
        </dl>
        ${(w.steps || []).length ? `<ol class="wfsteps">${w.steps.map(s =>
          `<li><b>${esc(s.title || '')}</b>${s.body ? ' — ' + esc(s.body) : ''}</li>`).join('')}</ol>` : ''}
        ${(w.faqs || []).length ? `<div class="wffaqs">${w.faqs.map(f =>
          `<div><b>${esc(f.question)}</b><div>${esc(f.answer)}</div></div>`).join('')}</div>` : ''}`;
    };
  });

  $$('[data-edit]', box).forEach(b => {
    b.onclick = async () => {
      const key = b.dataset.edit, pane = $('.wfdetail', rowFor(key));
      pane.hidden = false;
      const w = await jget(`/api/workflows/${encodeURIComponent(key)}`).catch(() => null);
      if (!w) { pane.innerHTML = '<div class="empty">Could not load that entry.</div>'; return; }
      pane.innerHTML = `
        <div class="field-row">
          <label class="field"><span>Name</span><input id="e-name" value="${esc(w.name || '')}"></label>
          <label class="field"><span>Category</span><input id="e-cat" value="${esc(w.category || '')}"></label>
        </div>
        <label class="field"><span>Owner</span><input id="e-own" value="${esc(w.owner || '')}"></label>
        <label class="field"><span>Summary — this is what retrieval matches against</span>
          <textarea id="e-sum">${esc(w.summary || '')}</textarea></label>
        <div class="btnrow">
          <button class="btn" id="e-save">Save</button>
          <button class="btn btn--ghost" id="e-cancel">Cancel</button>
        </div>
        <p class="lede">Steps come from the source document and aren't editable here — to
          change what it says, re-ingest the corrected file so the answer keeps its
          provenance.</p>`;
      $('#e-cancel', pane).onclick = () => { pane.hidden = true; };
      $('#e-save', pane).onclick = async () => {
        $('#e-save', pane).disabled = true;
        await jpost(`/api/workflows/${encodeURIComponent(key)}/update`, {
          name: $('#e-name', pane).value.trim(),
          category: $('#e-cat', pane).value.trim(),
          owner: $('#e-own', pane).value.trim(),
          summary: $('#e-sum', pane).value.trim(),
        });
        catalogTab();
      };
    };
  });

  $$('[data-del]', box).forEach(b => {
    b.onclick = async () => {
      const key = b.dataset.del;
      /* Deleting stops this answering questions immediately, so it asks first — and says
         what actually happens rather than "are you sure?". */
      if (!confirm(`Delete “${key}”?\n\nIt stops answering questions straight away and its `
                   + `steps, files and search index are removed. This can't be undone.`)) return;
      b.disabled = true;
      await jpost(`/api/workflows/${encodeURIComponent(key)}/delete`);
      catalogTab();
    };
  });
}

async function gapsTab() {
  const g = await jget('/api/gaps').catch(() => []);
  $('#stab').innerHTML = `<div class="card">
    <h2>Asked, but we couldn't answer</h2>
    <p class="lede">Every one of these is a workflow worth writing. Newest first.</p>
    ${g.length ? g.map(x => `<div class="gaprow"><q>${esc(x.question)}</q>
      <div>${esc(x.status)}</div></div>`).join('')
      : '<div class="empty">No gaps yet — ask something undocumented in chat to see one appear.</div>'}
  </div>`;
}

/* ---------------------------------------------------------------- admin -- */
function adminView() {
  $('#view').innerHTML = `<div class="page"><div class="page__inner">
    <h1>Administration</h1>
    <div class="tabs" id="atabs">
      <button data-atab="over" class="is-on">Overview</button>
      <button data-atab="model">Model settings</button>
    </div>
    <div id="atab"></div>
  </div></div>`;
  $('#atabs').onclick = e => {
    const b = e.target.closest('[data-atab]');
    if (!b) return;
    $$('button', $('#atabs')).forEach(x => x.classList.toggle('is-on', x === b));
    ({ over: overviewTab, model: modelTab })[b.dataset.atab]();
  };
  overviewTab();
}

/* What's actually happening: what people ask, which knowledge answers them, who changed
   what. Deliberately one screen — an audit nobody reads is worse than none. */
async function overviewTab() {
  const box = $('#atab');
  box.innerHTML = '<div class="card"><div class="empty">Loading…</div></div>';
  const o = await jget('/api/admin/overview').catch(() => null);
  if (!o) { box.innerHTML = '<div class="card"><div class="empty">Could not load.</div></div>'; return; }

  const stat = (n, label, hint = '') =>
    `<div class="stat"><div class="stat__n">${n}</div><div class="stat__l">${esc(label)}</div>
     ${hint ? `<div class="stat__h">${esc(hint)}</div>` : ''}</div>`;

  const ACTION_WORD = {
    ingested: 'added', published: 'published', edited: 'edited',
    deleted: 'deleted', reindexed: 'reindexed',
  };

  box.innerHTML = `
    <div class="statrow">
      ${stat(o.published, 'answering questions', `${o.workflows} total entries`)}
      ${stat(o.asks, `questions asked`, `last ${o.days} days`)}
      ${stat(o.answer_rate === null ? '—' : o.answer_rate + '%', 'answered',
             `${o.abstained} had nothing on file`)}
      ${stat(o.gaps_open, 'open gaps', 'worth writing up')}
    </div>

    <div class="card">
      <h2>Most used knowledge</h2>
      <p class="lede">How often each entry was behind an answer, last ${o.days} days. These
        are the ones worth keeping accurate.</p>
      ${o.top_artifacts.length ? `<div class="bars">${o.top_artifacts.map(a => `
        <div class="bar">
          <div class="bar__label">${esc(a.name)}</div>
          <div class="bar__track"><div class="bar__fill" style="width:${
            Math.round(100 * a.uses / o.top_artifacts[0].uses)}%"></div></div>
          <div class="bar__n">${a.uses}</div>
        </div>`).join('')}</div>`
        : '<div class="empty">No answers yet — ask something in chat.</div>'}
    </div>

    ${o.unused.length ? `<div class="card">
      <h2>Published, never used</h2>
      <p class="lede">Live, but has never been behind an answer. Either nobody needs it, or
        nobody phrases their question the way it's written.</p>
      ${o.unused.map(u => `<div class="wfrow"><div class="wfrow__top">
        <b>${esc(u.name)}</b><span class="wfkey">${esc(u.wf_key)}</span></div>
        <div class="wfrow__meta">@${esc(u.owner || '—')}</div></div>`).join('')}
    </div>` : ''}

    <div class="card">
      <h2>Knowledge changes</h2>
      <p class="lede">Who added, published, edited or removed knowledge. Newest first.</p>
      ${o.recent_activity.length ? `<table class="audit"><tbody>${o.recent_activity.map(a => `
        <tr>
          <td class="audit__who">${esc(a.actor || '—')}</td>
          <td><span class="badge badge--${a.action === 'deleted' ? 'low' : 'medium'}">${
            esc(ACTION_WORD[a.action] || a.action)}</span></td>
          <td class="audit__what"><span class="wfkey">${esc(a.wf_key || '—')}</span>
            ${a.detail ? `<span class="audit__detail">${esc(a.detail)}</span>` : ''}</td>
          <td class="audit__when">${esc(ago(a.at))}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty">Nothing changed yet.</div>'}
    </div>

    <div class="card">
      <h2>Latest questions</h2>
      <p class="lede">What people are actually asking, and whether we had an answer.</p>
      ${o.recent_questions.length ? `<table class="audit"><tbody>${o.recent_questions.map(q => `
        <tr>
          <td class="audit__who">${esc(q.asked_by || '—')}</td>
          <td class="audit__what"><q>${esc(q.question)}</q></td>
          <td>${q.abstained
            ? '<span class="badge badge--low">nothing on file</span>'
            : '<span class="badge badge--high">answered</span>'}</td>
          <td class="audit__when">${esc(ago(q.created_at))}</td>
        </tr>`).join('')}</tbody></table>`
        : '<div class="empty">No questions yet.</div>'}
    </div>`;
}

async function modelTab() {
  const s = await jget('/api/settings');
  $('#atab').innerHTML = `
    <p class="lede">Local-first. Spacebot runs on your own Ollama by default — free, private,
      nothing leaves this machine. Point it at a hosted model instead if you'd rather.</p>

    <div class="card">
      <h2>Provider</h2>
      <label class="field"><span>Which model serves answers</span>
        <select id="prov">
          <option value="auto">Auto — use local Ollama if it's running</option>
          <option value="ollama">Ollama (local)</option>
          <option value="anthropic">Anthropic (Claude)</option>
          <option value="openai">OpenAI / compatible</option>
          <option value="mock">Offline heuristic (no model)</option>
        </select></label>

      <div id="ollamaBlock">
        <label class="field"><span>Ollama base URL</span>
          <input id="obase" placeholder="http://localhost:11434"></label>
        <label class="field"><span>Model</span>
          <input id="model" placeholder="llama3.2:3b"></label>
        <div class="modelgrid" id="models"></div>
      </div>

      <div class="field-row" style="margin-top:16px">
        <label class="field"><span>Anthropic API key</span>
          <input id="ak" type="password" placeholder="${esc(s.anthropic_api_key_masked || 'not set')}"></label>
        <label class="field"><span>OpenAI API key</span>
          <input id="ok" type="password" placeholder="${esc(s.openai_api_key_masked || 'not set')}"></label>
      </div>

      <div class="btnrow">
        <button class="btn" id="save">Save changes</button>
        <button class="btn btn--ghost" id="test">Test connection</button>
      </div>
      <div class="result" id="aout"></div>
    </div>

    <div class="card">
      <h2>Currently resolving to</h2>
      <div class="details" style="margin:0"><dl>
        <dt>Provider</dt><dd>${esc(s.resolved_provider)}</dd>
        <dt>Compose model</dt><dd>${esc(s.compose_model || '—')}</dd>
        <dt>Router model</dt><dd>${esc(s.route_model || '—')}</dd>
        <dt>Ollama</dt><dd>${s.ollama_reachable
          ? `reachable at ${esc(s.ollama_base_url)}`
          : `<span style="color:var(--warn)">unreachable at ${esc(s.ollama_base_url)}</span>`}</dd>
      </dl></div>
      ${s.resolved_provider === 'mock'
        ? `<div class="notice" style="margin-top:14px">No model is connected, so Spacebot is
             running its offline heuristic: routing and citations still work, but answers are
             stitched from stored text rather than written fresh. Start Ollama
             (<code>ollama serve</code>) and pull a model to get real answers.</div>` : ''}
    </div>`;

  $('#prov').value = s.llm_provider || 'auto';
  $('#obase').value = s.ollama_base_url || '';
  $('#model').value = s.compose_model || '';
  paintModels(s.ollama_installed || [], s.compose_model);

  const syncBlocks = () => {
    const p = $('#prov').value;
    $('#ollamaBlock').style.display = (p === 'ollama' || p === 'auto') ? '' : 'none';
  };
  $('#prov').onchange = syncBlocks;
  syncBlocks();

  $('#save').onclick = async () => {
    const model = $('#model').value.trim();
    const body = {
      llm_provider: $('#prov').value,
      ollama_base_url: $('#obase').value.trim(),
      anthropic_api_key: $('#ak').value.trim(),
      openai_api_key: $('#ok').value.trim(),
    };
    if (model) { body.route_model = model; body.compose_model = model; }
    try {
      const r = await jpost('/api/settings', body);
      $('#aout').innerHTML = `<span class="ok">Saved — now resolving to
        <b>${esc(r.resolved_provider)}</b> (${esc(r.compose_model)})</span>`;
      state.me.provider = r.resolved_provider;
      $('.who span').textContent = `${state.me.role} · ${r.resolved_provider}`;
    } catch {
      $('#aout').innerHTML = '<span class="err">Could not save settings.</span>';
    }
  };

  $('#test').onclick = async () => {
    const out = $('#aout');
    out.innerHTML = '<span class="status"><span class="status__dot"></span>Testing…</span>';
    const h = await jget('/api/model/health').catch(() => ({ ok: false, error: 'request failed' }));
    if (h.reachable) {
      out.innerHTML = `<span class="ok">Reachable at ${esc(h.base)}</span>
        <div style="margin-top:6px;color:var(--text-mut)">Installed: ${(h.models || []).map(esc).join(', ') || '(none)'}</div>
        ${h.model_ready
          ? `<div class="ok" style="margin-top:4px">${esc(h.compose_model)} is ready</div>`
          : `<div style="color:var(--warn);margin-top:4px">${esc(h.compose_model)} isn't pulled —
              run <code>ollama pull ${esc(h.compose_model || '')}</code></div>`}`;
    } else if (h.ok) {
      out.innerHTML = `<span class="ok">${esc(h.provider || 'ok')}</span>`;
    } else {
      out.innerHTML = `<span class="err">${esc(h.error || 'unreachable')}</span>
        <div style="margin-top:6px;color:var(--text-mut)">${esc(h.hint || '')}</div>`;
    }
  };
}

function paintModels(models, current) {
  const box = $('#models');
  if (!box) return;
  box.innerHTML = models.length
    ? models.map(m => `<button class="modeltag${m === current ? ' is-on' : ''}"
        data-model="${esc(m)}">${esc(m)}</button>`).join('')
    : '<span style="color:var(--text-faint);font-size:12.5px">No models installed yet — run <code>ollama pull llama3.2:3b</code></span>';
  $$('[data-model]', box).forEach(b => {
    b.onclick = () => {
      $('#model').value = b.dataset.model;
      $$('.modeltag', box).forEach(x => x.classList.toggle('is-on', x === b));
    };
  });
}

boot();
