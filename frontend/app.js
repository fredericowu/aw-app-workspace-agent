/* ===========================================================================
   Command Center — Workspace Agent (UX-Proto visual reference)

   Three surfaces, one shell:
     1. Fleet    — the agent gallery. Landing view, by design.
     2. Sessions — channel rail (EVERY session) + a chat thread.
     3. System   — notification centre for sysadmin notices and agent
                   "need_human" questions, answerable inline.

   Plus a persistent IDE-style TAB STRIP across the top of the main area for
   the sessions you currently have OPEN. The relationship is a file tree vs.
   open editor tabs: the channel rail is every session that exists, the tab
   strip is the working set. Closing a tab drops it from the working set; the
   session itself is untouched and still in the rail.

   The rail's scope filter splits on WHO STARTED a session, not on its state:
   "User" is what Frederico typed himself, "Agents" is what a dispatch opened
   behind the scenes (`source` on the session, `origin` naming the caller).
   Archived and All sit alongside those two.

   Fleet is wired to the live Agents Platform over RELATIVE fetch paths
   ("api/...") — this app's own backend serves the page at its mount root,
   so those resolve under /api/apps/workspace-agent/api/*. Sessions and
   System are separate cards; their endpoints don't exist yet here, so any
   call into them degrades client-side (see sendMessage/replyTo below).
   =========================================================================== */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const S = {
  models: {},
  squads: [],
  agents: [],
  sessions: [],
  notifs: [],
  stats: {},
  ticker: [],
  workspace: {},

  view: 'fleet',
  // fleet filters
  q: '',
  squad: null,
  modelFilter: new Set(),
  runningOnly: false,
  // sessions
  sessionQ: '',
  scope: 'user',         // user | agent | archived | all
  activeSession: null,
  openTabs: [],          // session ids, in the order they were opened
  pane: 'list',
  // system
  sysFilter: 'needs',
  // overlays
  pickerPick: null,
  cmdSel: 0,
  cmdItems: [],
};

/* ---------------------------------------------------------------- helpers */

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/** Tiny inline markdown: **bold** and `code`. Escapes first. */
const md = (s) => esc(s)
  .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  .replace(/`([^`]+?)`/g, '<code>$1</code>');

/** Inline SVG icons — glyph fonts are not dependable across platforms. */
const ICON = {
  info: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.4"/></svg>',
};

const agentBy = (slug) => S.agents.find((a) => a.slug === slug);
// Each real agent carries its own hue (converted server-side from its own
// hex `color` field) — unlike the mock, this is per-agent, not per-model.
const hueOf = (agent) => (agent?.hue ?? 40);
const modelLabel = (agent) => (S.models[agent?.model]?.label ?? agent?.model ?? '—');

/** Two-letter tab badge from a slug: qa-sonnet → QS, architect → AR. */
function initials(slug) {
  const parts = String(slug || '?').split(/[-_.]/).filter(Boolean);
  const s = parts.length > 1 ? parts[0][0] + parts[1][0] : (parts[0] || '?').slice(0, 2);
  return s.toUpperCase();
}

/** Compact stamp for tight columns: "now", "12m", "3h", "2d". */
function ago(ts) {
  const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return 'now';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return d < 7 ? `${d}d` : `${Math.floor(d / 7)}w`;
}

/** Sentence form of the same — "just now" rather than the nonsense "now ago". */
function agoPhrase(ts) {
  const a = ago(ts);
  return a === 'now' ? 'just now' : `${a} ago`;
}

function clock(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function dayLabel(ts) {
  const d = new Date(ts * 1000);
  const today = new Date();
  const diff = Math.floor((today.setHours(0, 0, 0, 0) - new Date(d).setHours(0, 0, 0, 0)) / 86400000);
  if (diff <= 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

async function api(path, opts) {
  const res = await fetch(`api/${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

function toast(text, kind = '') {
  const el = document.createElement('div');
  el.className = `toast ${kind ? `toast--${kind}` : ''}`;
  el.innerHTML = `<i></i><span>${esc(text)}</span>`;
  $('#toasts').append(el);
  setTimeout(() => { el.classList.add('is-out'); setTimeout(() => el.remove(), 240); }, 2600);
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ================================================================= ROUTER */

function setView(view, { silent = false } = {}) {
  S.view = view;
  $$('.view').forEach((v) => { v.hidden = v.id !== `view-${view}`; });
  $$('.rail-btn[data-view]').forEach((b) => {
    const on = b.dataset.view === view;
    b.classList.toggle('is-active', on);
    b.setAttribute('aria-selected', String(on));
  });
  if (!silent) location.hash = view;
  renderCrumbs();
  renderTabs();
  if (view === 'fleet') renderFleet();
  if (view === 'sessions') { renderChannels(); renderThread(); }
  if (view === 'system') renderSystem();
  $('#stage').scrollTop = 0;
}

function renderCrumbs() {
  const c = $('#crumbs');
  const titles = { fleet: 'Agent fleet', sessions: 'Sessions', system: 'System' };
  let html = `<b>${titles[S.view]}</b>`;
  if (S.view === 'sessions' && S.activeSession) {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (s) {
      html = `<button data-crumb="sessions">Sessions</button><span class="sep">/</span><b>${esc(s.title)}</b>`;
    }
  }
  c.innerHTML = html;
  $$('button[data-crumb]', c).forEach((b) => b.onclick = () => {
    S.pane = 'list'; setView('sessions');
  });
}

/* =========================================================== TAB STRIP */

function openTabSessions() {
  return S.openTabs.map((id) => S.sessions.find((s) => s.id === id)).filter(Boolean);
}

function renderTabs() {
  const strip = $('#tabstripScroll');
  const tabs = openTabSessions();
  // a session could vanish under us — keep the id list honest
  if (tabs.length !== S.openTabs.length) S.openTabs = tabs.map((s) => s.id);

  const hint = $('#channelsHint');
  if (hint) {
    hint.textContent = tabs.length
      ? `${tabs.length} open as ${tabs.length === 1 ? 'tab' : 'tabs'}`
      : 'nothing open';
  }

  if (!tabs.length) {
    strip.innerHTML = `<span class="tabstrip-empty">No sessions open —
      pick one from <b>Sessions</b>, or start a new one →</span>`;
    return;
  }

  strip.innerHTML = tabs.map((s) => {
    const a = agentBy(s.agent);
    const on = S.activeSession === s.id;
    return `
      <button class="tab ${on ? 'is-active' : ''}" style="--h:${hueOf(a)}"
              data-tab="${s.id}" role="tab" aria-selected="${on}"
              title="${esc(s.title)} · ${esc(a?.name ?? s.agent)}">
        <span class="tab-badge" aria-hidden="true">${initials(s.agent)}</span>
        <span class="tab-label">
          <span class="tab-title">${esc(s.title)}</span>
          ${s.unread ? '<span class="tab-dot" title="unread"></span>' : ''}
        </span>
        <span class="tab-x" role="button" tabindex="0" data-close="${s.id}"
              aria-label="Close tab ${esc(s.title)}">✕</span>
      </button>`;
  }).join('');

  $$('.tab', strip).forEach((b) => {
    b.onclick = () => openSession(b.dataset.tab);
    // middle-click closes, same as a browser tab
    b.onauxclick = (e) => {
      if (e.button === 1) { e.preventDefault(); closeTab(b.dataset.tab); }
    };
  });
  $$('[data-close]', strip).forEach((x) => {
    x.onclick = (e) => { e.stopPropagation(); closeTab(x.dataset.close); };
    x.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault(); e.stopPropagation(); closeTab(x.dataset.close);
      }
    };
  });

  $('.tab.is-active', strip)?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function addTab(id) {
  if (!S.openTabs.includes(id)) S.openTabs.push(id);
}

function closeTab(id, { quiet = false } = {}) {
  const i = S.openTabs.indexOf(id);
  if (i < 0) return;
  S.openTabs.splice(i, 1);

  // closing the active tab hands focus to its neighbour, like an editor
  if (S.activeSession === id) {
    const next = S.openTabs[i] ?? S.openTabs[i - 1] ?? null;
    S.activeSession = next;
    S.pane = next ? 'thread' : 'list';
  }
  renderTabs();
  renderCrumbs();
  if (S.view === 'sessions') { renderChannels(); renderThread(); }
  if (!quiet) toast('Tab closed — the session is still in the list');
}

function cycleTab(dir) {
  if (S.openTabs.length < 2) return;
  const n = S.openTabs.length;
  const i = S.openTabs.indexOf(S.activeSession);
  openSession(S.openTabs[((i < 0 ? 0 : i) + dir + n) % n]);
}

/* ================================================================== FLEET */

function filteredAgents() {
  const q = S.q.trim().toLowerCase();
  return S.agents.filter((a) => {
    if (S.squad && a.squad !== S.squad) return false;
    if (S.modelFilter.size && !S.modelFilter.has(a.model)) return false;
    if (S.runningOnly && a.status !== 'running') return false;
    if (!q) return true;
    const hay = [a.name, a.slug, a.tagline, a.squad, modelLabel(a), ...(a.skills || [])]
      .join(' ').toLowerCase();
    return hay.includes(q);
  });
}

function agentCard(a) {
  return `
    <button class="card ${a.status === 'running' ? 'is-running' : ''}"
            style="--h:${hueOf(a)}" data-agent="${a.slug}"
            aria-label="${esc(a.name)} — open profile">
      <div class="card-top">
        <span class="avatar" aria-hidden="true">${a.glyph}</span>
        <span class="card-id">
          <span class="card-name">${esc(a.name)}</span>
          <span class="card-slug">${esc(a.slug)}</span>
        </span>
        <span class="status status--${a.status}"><i></i>${a.status}</span>
      </div>
      <div class="card-tag">${esc(a.tagline)}</div>
      <div class="card-foot">
        <span class="pill">${esc(modelLabel(a))}</span>
      </div>
    </button>`;
}

function renderHeroStats() {
  const st = S.stats;
  // Sessions/System aren't wired yet (separate cards) — only tiles fed by
  // real, already-cheap counts ship here; everything else degrades away
  // rather than showing a number this build cannot honestly produce.
  const cards = [
    { n: st.agents, l: 'Agents', sub: `${st.online} online` },
    { n: st.running, l: 'Running now', cls: 'stat--amber' },
  ];
  $('#heroStats').innerHTML = cards.map((c) => `
    <div class="stat ${c.cls || ''}">
      <div class="stat-n">${c.n}</div>
      <div class="stat-l">${c.l}</div>
      ${c.sub ? `<div class="stat-sub">${esc(c.sub)}</div>` : ''}
    </div>`).join('');
}

function renderChips() {
  const counts = {};
  S.agents.forEach((a) => { counts[a.squad] = (counts[a.squad] || 0) + 1; });
  $('#squadChips').innerHTML = [
    `<button class="chip ${!S.squad ? 'is-on' : ''}" data-squad="">All
       <span class="chip-n">${S.agents.length}</span></button>`,
    ...S.squads.map((s) => `
      <button class="chip ${S.squad === s.key ? 'is-on' : ''}" data-squad="${s.key}"
              title="${esc(s.blurb)}">${esc(s.label)}
        <span class="chip-n">${counts[s.key] || 0}</span></button>`),
  ].join('');

  $('#modelChips').innerHTML = Object.entries(S.models).map(([key, m]) => `
    <button class="chip chip--model ${S.modelFilter.has(key) ? 'is-on' : ''}"
            style="--h:${m.hue}" data-model="${key}">
      <span class="chip-swatch" aria-hidden="true"></span>${esc(m.label)}</button>`).join('');

  $$('#squadChips .chip').forEach((b) => b.onclick = () => {
    S.squad = b.dataset.squad || null; renderFleet();
  });
  $$('#modelChips .chip').forEach((b) => b.onclick = () => {
    const k = b.dataset.model;
    S.modelFilter.has(k) ? S.modelFilter.delete(k) : S.modelFilter.add(k);
    renderFleet();
  });
}

function renderFleet() {
  renderHeroStats();
  renderChips();

  const list = filteredAgents();
  const wrap = $('#fleetGrids');
  const empty = $('#fleetEmpty');

  $('#agentSearchClear').hidden = !S.q;
  $('#runningToggle').setAttribute('aria-pressed', String(S.runningOnly));

  if (!list.length) {
    wrap.innerHTML = '';
    empty.hidden = false;
    const why = [];
    if (S.q) why.push(`matching “${S.q}”`);
    if (S.squad) why.push(`in ${S.squad}`);
    if (S.modelFilter.size) why.push(`on ${[...S.modelFilter].map((m) => modelLabel({ model: m })).join(' / ')}`);
    if (S.runningOnly) why.push('currently running');
    $('#fleetEmptyWhy').textContent = why.length
      ? `No agent ${why.join(', ')}. The fleet has ${S.agents.length} agents in total.`
      : 'Nothing in the fleet matches the current filters.';
    return;
  }
  empty.hidden = true;

  // Group by squad, preserving the canonical squad order.
  const groups = S.squads
    .map((meta) => ({ meta, items: list.filter((a) => a.squad === meta.key) }))
    .filter((g) => g.items.length);

  wrap.innerHTML = groups.map((g) => `
    <section class="squad">
      <header class="squad-head">
        <h2>${esc(g.meta.label)}</h2>
        <span class="blurb">${esc(g.meta.blurb)}</span>
        <span class="count">${g.items.length}</span>
      </header>
      <div class="grid">${g.items.map(agentCard).join('')}</div>
    </section>`).join('');

  $$('#fleetGrids .card').forEach((c) => c.onclick = () => openSheet(c.dataset.agent));
}

/* ========================================================== AGENT SHEET */

function sessionsFor(slug) {
  return S.sessions.filter((s) => s.agent === slug)
    .sort((a, b) => b.updated_at - a.updated_at);
}

function openSheet(slug) {
  const a = agentBy(slug);
  if (!a) return;
  const mine = sessionsFor(slug);

  $('#sheetInner').innerHTML = `
    <div class="sheet-inner" style="--h:${hueOf(a)}">
      <header class="sheet-hero">
        <button class="icon-btn icon-btn--lg" id="sheetClose" aria-label="Close agent profile">✕</button>
        <div class="sheet-av" aria-hidden="true">${a.glyph}</div>
        <h2>${esc(a.name)}</h2>
        <div class="mono">${esc(a.slug)}</div>
        <div class="row">
          <span class="status status--${a.status}"><i></i>${a.status}</span>
          <span class="pill">${esc(modelLabel(a))}</span>
          <span class="tag tag--mono">${esc(a.squad)}</span>
        </div>
      </header>

      <section class="sheet-sec">
        <h3>What it does</h3>
        <p>${md(a.about)}</p>
      </section>

      <section class="sheet-sec">
        <h3>Good at</h3>
        <div class="row">
          ${a.skills.map((s) => `<span class="tag tag--mono">${esc(s)}</span>`).join('')}
        </div>
      </section>

      <section class="sheet-sec">
        <h3>Sessions with this agent · ${mine.length}</h3>
        ${mine.length ? `<div class="sheet-sessions">${mine.slice(0, 5).map((s) => `
          <button class="dnote" data-open-session="${s.id}">
            <div class="dnote-title">${esc(s.title)}</div>
            <div class="dnote-prev">${esc(lastPreview(s))}</div>
            <div class="dnote-top"><span class="mono">${agoPhrase(s.updated_at)} · ${s.messages.length} messages · ${esc(originPhrase(s))}${S.openTabs.includes(s.id) ? ' · open as a tab' : ''}</span></div>
          </button>`).join('')}</div>`
      : `<p style="color:var(--ink-3)">None yet. Starting one below is the fastest way to find out how it works.</p>`}
      </section>

      <div class="sheet-actions">
        <button class="btn btn--primary" id="sheetStart">Start a session with ${esc(a.name)}</button>
        <button class="btn btn--ghost" id="sheetCompare">See the whole ${esc(a.squad)} squad</button>
      </div>
    </div>`;

  $('#sheet').classList.add('is-open');
  $('#sheet').setAttribute('aria-hidden', 'false');
  $('#scrim').hidden = false;

  $('#sheetClose').onclick = closeSheet;
  $('#sheetStart').onclick = () => { closeSheet(); openPicker(slug); };
  $('#sheetCompare').onclick = () => {
    closeSheet(); S.squad = a.squad; S.q = ''; $('#agentSearch').value = '';
    setView('fleet'); toast(`Filtered the fleet to ${a.squad}`);
  };
  $$('[data-open-session]', $('#sheetInner')).forEach((b) => b.onclick = () => {
    closeSheet(); openSession(b.dataset.openSession);
  });
  $('#sheetClose').focus();
}

function closeSheet() {
  $('#sheet').classList.remove('is-open');
  $('#sheet').setAttribute('aria-hidden', 'true');
  if (!$('#drawer').classList.contains('is-open')) $('#scrim').hidden = true;
}

/* =============================================================== SESSIONS */

/* Scope is about WHO STARTED a session, not what state it is in — except
   that archiving takes it out of both live buckets, the way it always did.
   A session with no `source` is treated as yours; that is the safe default
   for anything created client-side. */

function scopeOf(s) {
  if (s.state === 'archived') return 'archived';
  return s.source === 'agent' ? 'agent' : 'user';
}

function inScope(s, scope) {
  return scope === 'all' || scopeOf(s) === scope;
}

const SCOPE_NOUN = {
  user: 'started by you',
  agent: 'started by an agent',
  archived: 'archived',
  all: 'in any session',
};

/** "you started this" / "dispatched by manager" — shown in the thread head. */
function originPhrase(s) {
  if (s.source !== 'agent') return 'you started this';
  return `dispatched by ${s.origin || 'another agent'}`;
}

function lastPreview(s) {
  const m = s.messages[s.messages.length - 1];
  if (!m) return 'No messages yet — say something to get going.';
  return `${m.role === 'user' ? 'You: ' : ''}${m.text.replace(/\s+/g, ' ').slice(0, 90)}`;
}

function scopedSessions() {
  const q = S.sessionQ.trim().toLowerCase();
  return S.sessions.filter((s) => {
    if (!inScope(s, S.scope)) return false;
    if (!q) return true;
    const ag = agentBy(s.agent);
    const hay = [s.title, s.target, s.origin, ag?.name, s.agent, ...s.messages.map((m) => m.text)]
      .join(' ').toLowerCase();
    return hay.includes(q);
  });
}

function renderChannels() {
  const list = scopedSessions();
  $('#railSessionCount').textContent = S.sessions.filter((s) => s.state === 'open').length;
  $$('#view-sessions .seg-btn[data-scope]').forEach((b) => {
    const sc = b.dataset.scope;
    b.classList.toggle('is-active', sc === S.scope);
    const n = $('.seg-n', b);
    if (n) n.textContent = S.sessions.filter((s) => inScope(s, sc)).length;
  });

  // The working set and the scope filter are independent, so the session you
  // are reading can sit outside the scope you are browsing. Say so and offer
  // the one click back, rather than silently highlighting nothing.
  const active = S.sessions.find((x) => x.id === S.activeSession);
  const notice = (!active || inScope(active, S.scope)) ? '' : `
    <div class="ch-note">
      <span>You're reading <b>${esc(active.title)}</b> — it is
        ${esc(SCOPE_NOUN[scopeOf(active)])}, so this filter hides it.</span>
      <button class="btn btn--sm btn--ghost" id="chShowActive">Show it</button>
    </div>`;

  const el = $('#channelsList');
  const wireNotice = () => {
    const b = $('#chShowActive');
    if (b) b.onclick = () => { S.scope = scopeOf(active); renderChannels(); };
  };

  if (!list.length) {
    const title = S.sessionQ ? 'No match'
      : S.scope === 'archived' ? 'Nothing archived'
        : S.scope === 'agent' ? 'Nothing dispatched'
          : S.scope === 'user' ? 'You have not started one yet'
            : 'No sessions yet';
    const why = S.sessionQ
      ? `Nothing ${S.scope === 'all' ? 'in any session' : `${SCOPE_NOUN[S.scope]}`} mentions “${esc(S.sessionQ)}”.`
      : S.scope === 'agent'
        ? 'Sessions one agent opens on another land here. Nothing has been dispatched yet.'
        : 'Start one and it shows up here, grouped by when you last touched it.';

    el.innerHTML = notice + `
      <div class="empty" style="padding:34px 16px;margin:8px">
        <div class="empty-mark">◎</div>
        <h3>${title}</h3>
        <p>${why}</p>
        <button class="btn btn--primary btn--sm" id="chEmptyAct">${S.sessionQ ? 'Clear search'
        : '<span class="btn-plus" aria-hidden="true">+</span> New session'}</button>
      </div>`;
    wireNotice();
    $('#chEmptyAct').onclick = () => {
      if (S.sessionQ) { S.sessionQ = ''; $('#sessionSearch').value = ''; renderChannels(); }
      else openPicker();
    };
    return;
  }

  const pinned = list.filter((s) => s.pinned);
  const rest = list.filter((s) => !s.pinned);
  const buckets = new Map();
  rest.sort((a, b) => b.updated_at - a.updated_at)
    .forEach((s) => {
      const k = dayLabel(s.updated_at);
      if (!buckets.has(k)) buckets.set(k, []);
      buckets.get(k).push(s);
    });

  const group = (title, items) => !items.length ? '' : `
    <div class="ch-group">
      <h3>${esc(title)}</h3>
      ${items.map(chRow).join('')}
    </div>`;

  el.innerHTML = notice + group('★ Pinned', pinned) +
    [...buckets.entries()].map(([k, v]) => group(k, v)).join('');

  wireNotice();
  $$('.ch', el).forEach((b) => b.onclick = () => openSession(b.dataset.session));
}

function chRow(s) {
  const a = agentBy(s.agent);
  const isOpen = S.openTabs.includes(s.id);
  return `
    <button class="ch ${S.activeSession === s.id ? 'is-active' : ''}
                   ${s.unread ? 'is-unread' : ''} ${s.pinned ? 'is-pinned' : ''}
                   ${isOpen ? 'is-open-tab' : ''}"
            style="--h:${hueOf(a)}" data-session="${s.id}"
            title="${esc(s.title)} — ${esc(originPhrase(s))}">
      <span class="ch-av" aria-hidden="true">${a?.glyph ?? '◆'}</span>
      <span class="ch-body">
        <span class="ch-row1">
          <span class="ch-title">${esc(s.title)}</span>
          <span class="ch-time">${ago(s.updated_at)}</span>
        </span>
        <span class="ch-row2">
          <span class="ch-who">${esc(s.agent)}</span>
          <span class="ch-prev">${esc(lastPreview(s))}</span>
          ${s.unread ? '<span class="ch-dot" title="unread"></span>' : ''}
        </span>
      </span>
      ${s.pinned ? '<span class="ch-star" aria-label="pinned">★</span>' : ''}
      ${isOpen ? '<span class="ch-open" title="open as a tab"></span>' : ''}
    </button>`;
}

function openSession(id) {
  const s = S.sessions.find((x) => x.id === id);
  if (!s) return;
  addTab(id);
  S.activeSession = id;
  S.pane = 'thread';
  s.unread = 0;
  if (S.view !== 'sessions') { setView('sessions'); return; }
  renderTabs();
  renderChannels();
  renderThread();
  renderCrumbs();
}

function renderThread() {
  const s = S.sessions.find((x) => x.id === S.activeSession);
  $('.split').dataset.pane = S.pane;
  if (!s) {
    $('#threadEmpty').hidden = false;
    $('#threadBody').hidden = true;
    const n = S.openTabs.length;
    $('#threadEmptyTitle').textContent = n ? 'Nothing selected' : 'No session open';
    $('#threadEmptyWhy').textContent = n
      ? `You have ${n} session${n > 1 ? 's' : ''} open as tab${n > 1 ? 's' : ''} above — `
        + 'click one to come back to it, or pick another from the list.'
      : 'Pick a session on the left and it opens as a tab above, or start a new one — '
        + "you'll go through the agent gallery so you can see who you're talking to.";
    return;
  }
  const a = agentBy(s.agent);
  $('#threadEmpty').hidden = true;
  $('#threadBody').hidden = false;
  const av = $('#threadAvatar');
  av.textContent = a?.glyph ?? '◆';
  $('#threadBody').style.setProperty('--h', hueOf(a));
  $('#threadTitle').textContent = s.title;
  $('#threadMeta').innerHTML = `
    <span class="status status--${a?.status}"><i></i>${a?.status}</span>
    <span>${esc(a?.name ?? s.agent)}</span>
    <span class="mono">${esc(modelLabel(a))}</span>
    <span class="mono" title="${s.source === 'agent' ? 'Opened by a dispatch, not typed by you' : 'You opened this one yourself'}">${esc(originPhrase(s))}</span>
    ${s.target ? `<span class="mono">target: ${esc(s.target)}</span>` : ''}
    ${s.state === 'archived' ? '<span class="tag tag--mono">archived</span>' : ''}`;

  $('#threadPin').classList.toggle('is-on', !!s.pinned);
  $('#threadPin').setAttribute('aria-label', s.pinned ? 'Unpin session' : 'Pin session');
  $('#threadArchive').setAttribute('aria-label', s.state === 'archived' ? 'Unarchive session' : 'Archive session');
  $('#composerHint').textContent = s.state === 'archived'
    ? 'This session is archived — sending a message reopens it.'
    : 'Sessions aren’t wired to a real agent run yet — this message stays in this tab only.';

  const box = $('#msgs');
  let lastDay = null;
  box.innerHTML = s.messages.map((m) => {
    const d = dayLabel(m.at);
    const sep = d !== lastDay ? `<div class="day-sep">${esc(d)}</div>` : '';
    lastDay = d;
    return sep + msgHtml(m, a);
  }).join('') || `
    <div class="empty empty--pane">
      <div class="empty-mark">◆</div>
      <h3>Nothing said yet</h3>
      <p>This session is open with ${esc(a?.name ?? s.agent)} but empty. Send the first message below.</p>
    </div>`;
  box.scrollTop = box.scrollHeight;
}

function msgHtml(m, a) {
  const mine = m.role === 'user';
  return `
    <div class="msg msg--${mine ? 'user' : 'agent'}" style="--h:${hueOf(a)}">
      <span class="msg-av" aria-hidden="true">${mine ? 'F' : (a?.glyph ?? '◆')}</span>
      <div>
        <div class="msg-who">${mine ? 'You' : esc(a?.name ?? 'Agent')}
          <span class="mono">${clock(m.at)}</span></div>
        <div class="msg-text">${md(m.text)}</div>
      </div>
    </div>`;
}

async function sendMessage(text) {
  const s = S.sessions.find((x) => x.id === S.activeSession);
  if (!s || !text.trim()) return;
  const a = agentBy(s.agent);

  s.messages.push({ role: 'user', text, kind: 'text', at: Date.now() / 1000 });
  s.updated_at = Date.now() / 1000;
  if (s.state === 'archived') { s.state = 'open'; toast('Session reopened', 'ok'); }
  renderThread();
  renderChannels();

  // typing indicator
  const box = $('#msgs');
  const pend = document.createElement('div');
  pend.className = 'msg msg--agent msg--pending';
  pend.style.setProperty('--h', hueOf(a));
  pend.innerHTML = `
    <span class="msg-av" aria-hidden="true">${a?.glyph ?? '◆'}</span>
    <div><div class="msg-who">${esc(a?.name ?? 'Agent')}
      <span class="mono">thinking…</span></div>
      <div class="msg-text"><span class="typing"><i></i><i></i><i></i></span></div></div>`;
  box.append(pend);
  box.scrollTop = box.scrollHeight;

  let reply;
  try {
    const r = await api(`sessions/${s.id}/messages`, {
      method: 'POST', body: JSON.stringify({ text }),
    });
    reply = r.reply;
  } catch (err) {
    pend.remove();
    s.messages.push({
      role: 'agent',
      text: `Sessions aren't wired to a real agent yet (\`${err.message}\`), so there is no reply to show. `
        + 'Your message is still in the thread.',
      at: Date.now() / 1000,
    });
    renderThread();
    toast('Sessions aren’t wired yet');
    return;
  }

  // stream it in so the surface feels alive
  await sleep(380);
  const target = $('.msg-text', pend);
  $('.mono', pend).textContent = clock(reply.at);
  target.innerHTML = '';
  const words = reply.text.split(' ');
  for (let i = 0; i < words.length; i += 3) {
    target.innerHTML = md(words.slice(0, i + 3).join(' '));
    box.scrollTop = box.scrollHeight;
    await sleep(26);
  }
  pend.classList.remove('msg--pending');
  if (!s.messages.includes(reply)) s.messages.push(reply);
  s.updated_at = Date.now() / 1000;
  renderThread();
  renderChannels();
  renderTabs();
}

/* ================================================================= SYSTEM */

const NOTE_ICON = { question: '?', alert: '!', approval: '⛨' };
const NOTE_KIND_LABEL = { question: 'Agent question', alert: 'Alert', approval: 'Approval' };

function sysCounts() {
  const open = S.notifs.filter((n) => n.state === 'open');
  return {
    needs: open.filter((n) => n.severity === 'action' || n.severity === 'critical').length,
    questions: open.filter((n) => n.kind === 'question').length,
    alerts: open.filter((n) => n.kind === 'alert').length,
    resolved: S.notifs.filter((n) => n.state === 'resolved').length,
    open: open.length,
  };
}

function updateBadges() {
  const c = sysCounts();
  const badge = $('#bellBadge');
  const rail = $('#railSystemBadge');
  badge.hidden = rail.hidden = !c.needs;
  badge.textContent = rail.textContent = c.needs;
  $('#nNeeds').textContent = c.needs;
  $('#nQ').textContent = c.questions;
  $('#nA').textContent = c.alerts;
  $('#nR').textContent = c.resolved;
  $('#drawerSub').textContent = c.needs
    ? `${c.needs} waiting on you · ${c.open} open`
    : `Nothing waiting on you · ${c.open} open`;
  S.stats.needs_you = c.needs;
}

function filteredNotifs() {
  const f = S.sysFilter;
  return S.notifs
    .filter((n) => {
      if (f === 'all') return true;
      if (f === 'resolved') return n.state === 'resolved';
      if (n.state !== 'open') return false;
      if (f === 'needs') return n.severity === 'action' || n.severity === 'critical';
      if (f === 'questions') return n.kind === 'question';
      if (f === 'alerts') return n.kind === 'alert';
      return true;
    })
    .sort((a, b) => b.at - a.at);
}

function renderSystem() {
  updateBadges();
  $$('#sysFilters .seg-btn').forEach((b) =>
    b.classList.toggle('is-active', b.dataset.filter === S.sysFilter));

  const c = sysCounts();
  $('#sysSummary').innerHTML = [
    { n: c.needs, l: 'Needs you', cls: c.needs ? 'stat--crit' : '' },
    { n: c.questions, l: 'Questions' },
    { n: c.alerts, l: 'Alerts' },
    { n: c.resolved, l: 'Resolved' },
  ].map((s) => `<div class="stat ${s.cls || ''}">
      <div class="stat-n">${s.n}</div><div class="stat-l">${s.l}</div></div>`).join('');

  const list = filteredNotifs();
  const el = $('#sysList');
  const empty = $('#sysEmpty');

  if (!list.length) {
    el.innerHTML = '';
    empty.hidden = false;
    const titles = {
      needs: 'Nothing waiting on you',
      questions: 'No open agent questions',
      alerts: 'No open alerts',
      resolved: 'Nothing resolved yet',
      all: 'Nothing here at all',
    };
    $('#sysEmptyTitle').textContent = titles[S.sysFilter];
    $('#sysEmptyWhy').textContent = S.sysFilter === 'resolved'
      ? 'Answered questions and acknowledged alerts collect here.'
      : 'New notices land here and ring the bell in the top bar. Nothing is hiding behind this view.';
    $('#sysShowAll').hidden = S.sysFilter === 'all';
    return;
  }
  empty.hidden = true;
  el.innerHTML = list.map(noteHtml).join('');
  wireNotes(el);
}

function noteHtml(n) {
  const a = agentBy(n.from_agent);
  const sev = n.state === 'resolved' ? 'resolved' : n.severity === 'critical' ? 'critical'
    : n.severity === 'warn' ? 'warn' : n.severity === 'info' ? 'info' : 'action';
  const ctx = n.context || {};
  return `
    <article class="note note--${sev} ${n.state === 'resolved' ? 'is-resolved' : ''}" data-note="${n.id}">
      <header class="note-head">
        <span class="note-ico" aria-hidden="true">${NOTE_ICON[n.kind]}</span>
        <div>
          <div class="note-title">${esc(n.title)}</div>
          <div class="note-sub">
            <span class="tag">${NOTE_KIND_LABEL[n.kind]}</span>
            ${n.state === 'resolved' ? '<span class="tag tag--ok">resolved</span>' : ''}
            <span>from <b style="color:var(--ink-2)">${esc(a?.name ?? n.from_agent)}</b></span>
            <span class="tag tag--mono">${esc(ctx.run ?? 'no run')}</span>
            ${ctx.card ? `<span class="tag tag--mono">card ${esc(ctx.card)}</span>` : ''}
            ${ctx.target ? `<span class="tag tag--mono">${esc(ctx.target)}</span>` : ''}
            <span style="color:var(--ink-4)">${agoPhrase(n.at)}</span>
          </div>
        </div>
        <button class="icon-btn" data-goto-agent="${n.from_agent}"
                title="Open ${esc(a?.name ?? '')}" aria-label="Open agent profile">${ICON.info}</button>
      </header>

      <div class="note-body">
        <div class="note-text">${md(n.body)}</div>

        ${n.replies.length ? `<div class="note-replies">${n.replies.map((r) => `
          <div class="rep rep--${r.role}">
            <span class="rep-who">${r.role === 'user' ? 'You' : 'Agent'}</span>
            <span>${md(r.text)}</span>
          </div>`).join('')}</div>` : ''}

        ${n.state === 'open' ? `
          <div class="note-reply">
            ${n.suggestions?.length ? `<div class="sugg">${n.suggestions.map((s) => `
              <button class="sugg-btn" data-sugg="${esc(s)}">${esc(s)}</button>`).join('')}</div>` : ''}
            <div class="reply-box">
              <textarea rows="1" data-reply-input placeholder="Reply to ${esc(a?.name ?? 'the agent')} — this goes straight back into the run…" aria-label="Reply"></textarea>
              <button class="btn btn--primary btn--sm" data-send-reply disabled>Send</button>
            </div>
            <div class="note-foot">
              <span class="spacer"></span>
              <button class="btn btn--ghost btn--sm" data-open-thread="${n.from_agent}">Open a session instead</button>
              ${n.kind === 'alert' ? '<button class="btn btn--ghost btn--sm" data-ack>Acknowledge only</button>' : ''}
            </div>
          </div>` : `
          <div class="note-foot" style="margin-top:12px">
            <span class="spacer"></span>
            <button class="btn btn--ghost btn--sm" data-reopen>Reopen</button>
          </div>`}
      </div>
    </article>`;
}

function wireNotes(root) {
  $$('.note', root).forEach((card) => {
    const id = card.dataset.note;
    const ta = $('[data-reply-input]', card);
    const send = $('[data-send-reply]', card);

    if (ta && send) {
      const sync = () => {
        send.disabled = !ta.value.trim();
        ta.style.height = 'auto';
        ta.style.height = `${Math.min(140, ta.scrollHeight)}px`;
      };
      ta.oninput = sync;
      ta.onkeydown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (ta.value.trim()) replyTo(id, ta.value); }
      };
      send.onclick = () => replyTo(id, ta.value);
      $$('[data-sugg]', card).forEach((b) => b.onclick = () => {
        ta.value = b.dataset.sugg; sync(); ta.focus();
      });
    }
    const ack = $('[data-ack]', card);
    if (ack) ack.onclick = () => replyTo(id, 'Acknowledged.');
    const reopen = $('[data-reopen]', card);
    if (reopen) reopen.onclick = () => reopenNote(id);
    $$('[data-goto-agent]', card).forEach((b) => b.onclick = () => {
      closeDrawer(); openSheet(b.dataset.gotoAgent);
    });
    $$('[data-open-thread]', card).forEach((b) => b.onclick = () => {
      closeDrawer(); openPicker(b.dataset.openThread);
    });
  });
}

async function replyTo(id, text) {
  const n = S.notifs.find((x) => x.id === id);
  if (!n || !text.trim()) return;
  const now = Date.now() / 1000;
  n.replies.push({ role: 'user', text: text.trim(), at: now });
  n.state = 'resolved';

  try {
    const r = await api(`notifications/${id}/reply`, {
      method: 'POST', body: JSON.stringify({ text: text.trim(), resolve: true }),
    });
    n.replies = r.notification.replies;
  } catch {
    // System isn't wired to a real run yet (separate card) — acknowledge
    // client-side rather than dead-ending on the missing endpoint.
    n.replies.push({
      role: 'agent',
      at: now + 1,
      text: {
        question: 'Got it — proceeding on that. I\'ll report back on the run when it lands.',
        approval: 'Recorded. Continuing the run with that decision.',
        alert: 'Acknowledged. I\'ll stop surfacing this unless the trend changes.',
      }[n.kind],
    });
  }
  toast('Reply sent back into the run', 'ok');
  renderSystem();
  renderDrawer();
  renderHeroStats();
}

async function reopenNote(id) {
  const n = S.notifs.find((x) => x.id === id);
  if (!n) return;
  n.state = 'open';
  try { await api(`notifications/${id}/reopen`, { method: 'POST' }); } catch { /* local-only note */ }
  S.sysFilter = 'needs';
  renderSystem();
  renderDrawer();
  toast('Reopened — it is waiting on you again');
}

/* ================================================================= DRAWER */

function renderDrawer() {
  updateBadges();
  const list = S.notifs.slice().sort((a, b) => {
    const rank = (n) => (n.state === 'open' ? 0 : 1);
    return rank(a) - rank(b) || b.at - a.at;
  });
  $('#drawerList').innerHTML = list.map((n) => {
    const a = agentBy(n.from_agent);
    const sev = n.state === 'resolved' ? 'resolved' : n.severity === 'critical' ? 'critical'
      : n.severity === 'warn' ? 'warn' : n.severity === 'info' ? 'info' : 'action';
    return `
      <button class="dnote dnote--${sev} ${n.state === 'resolved' ? 'is-resolved' : ''}" data-note-jump="${n.id}">
        <div class="dnote-top">
          <span class="tag">${NOTE_KIND_LABEL[n.kind]}</span>
          <span style="color:var(--ink-3)">${esc(a?.name ?? n.from_agent)}</span>
          <span class="mono">${ago(n.at)}</span>
        </div>
        <div class="dnote-title">${esc(n.title)}</div>
        <div class="dnote-prev">${esc(n.body)}</div>
        <div class="dnote-cta">${n.state === 'open' ? 'Open and reply →' : 'Resolved · view thread →'}</div>
      </button>`;
  }).join('') || `
    <div class="empty" style="padding:40px 16px">
      <div class="empty-mark">✓</div><h3>All clear</h3>
      <p>No notifications at all. Alerts and agent questions appear here the moment they are raised.</p>
    </div>`;

  $$('#drawerList .dnote').forEach((b) => b.onclick = () => {
    const id = b.dataset.noteJump;
    const n = S.notifs.find((x) => x.id === id);
    closeDrawer();
    S.sysFilter = n?.state === 'resolved' ? 'resolved' : 'all';
    setView('system');
    requestAnimationFrame(() => {
      const card = $(`.note[data-note="${id}"]`);
      if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.style.borderColor = 'var(--amber)';
        setTimeout(() => { card.style.borderColor = ''; }, 1400);
        $('[data-reply-input]', card)?.focus({ preventScroll: true });
      }
    });
  });
}

function openDrawer() {
  renderDrawer();
  $('#drawer').classList.add('is-open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#scrim').hidden = false;
  $('#drawerClose').focus();
}
function closeDrawer() {
  $('#drawer').classList.remove('is-open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  if (!$('#sheet').classList.contains('is-open')) $('#scrim').hidden = true;
}

/* ================================================= PICKER (new session) */

function openPicker(preselect = null) {
  S.pickerPick = preselect;
  $('#pickerSearch').value = '';
  $('#pickerKickoff').value = '';
  renderPicker();
  openModal('#picker');
  if (preselect) $('#pickerKickoff').focus();
  else $('#pickerSearch').focus();
}

function renderPicker() {
  const q = $('#pickerSearch').value.trim().toLowerCase();
  const list = S.agents.filter((a) => !q ||
    [a.name, a.slug, a.tagline, a.squad, ...(a.skills || [])].join(' ').toLowerCase().includes(q));

  $('#pickerEmpty').hidden = !!list.length;
  $('#pickerGrid').hidden = !list.length;
  $('#pickerGrid').innerHTML = list.map((a) => `
    <button class="pcard ${S.pickerPick === a.slug ? 'is-picked' : ''}"
            style="--h:${hueOf(a)}" data-pick="${a.slug}">
      <span class="avatar" aria-hidden="true">${a.glyph}</span>
      <span class="pcard-id">
        <span class="pcard-name">${esc(a.name)}</span>
        <span class="pcard-tag">${esc(a.tagline)}</span>
      </span>
    </button>`).join('');

  $$('#pickerGrid .pcard').forEach((b) => b.onclick = () => {
    S.pickerPick = b.dataset.pick;
    renderPicker();
    $('#pickerKickoff').focus();
  });

  const foot = $('#pickerFoot');
  if (S.pickerPick) {
    const a = agentBy(S.pickerPick);
    foot.hidden = false;
    $('#picked').innerHTML = `
      <span class="avatar" style="--h:${hueOf(a)}" aria-hidden="true">${a.glyph}</span>
      <span>
        <div style="font-weight:650">${esc(a.name)}</div>
        <div style="font-size:11.5px;color:var(--ink-3)">${esc(modelLabel(a))} · ${esc(a.squad)} · ${esc(a.slug)}</div>
      </span>`;
  } else {
    foot.hidden = true;
  }
}

async function startSession() {
  const slug = S.pickerPick;
  if (!slug) return;
  const first = $('#pickerKickoff').value.trim();
  const a = agentBy(slug);
  let sess;
  try {
    const r = await api('sessions', {
      method: 'POST',
      body: JSON.stringify({ agent: slug, first_message: first || null }),
    });
    sess = r.session;
    S.stats = { ...r.stats, needs_you: sysCounts().needs };
  } catch {
    // Degrade rather than dead-end: keep the session client-side.
    sess = {
      id: `local-${Date.now()}`, agent: slug, group: 'Today', pinned: false,
      unread: 0, target: null, state: 'open', updated_at: Date.now() / 1000,
      // you just started it from the gallery, so it is yours by definition
      source: 'user', origin: null,
      title: first ? first.slice(0, 58) : `New session · ${a.name}`,
      messages: first ? [{ role: 'user', text: first, at: Date.now() / 1000 }] : [],
    };
    toast('Backend unreachable — session kept locally');
  }
  S.sessions = S.sessions.filter((s) => s.id !== sess.id);
  S.sessions.unshift(sess);
  closeModal('#picker');
  // land on the scope that actually contains what you just made
  S.scope = 'user';
  S.sessionQ = '';
  $('#sessionSearch').value = '';
  openSession(sess.id);
  toast(`Opened a tab with ${a.name}`, 'ok');
}

/* ======================================================= COMMAND PALETTE */

function cmdSources() {
  const items = [
    { kind: 'action', glyph: '+', main: 'New session', sub: 'Opens the agent gallery, lands as a tab', h: 40, run: () => openPicker() },
    { kind: 'action', glyph: '◆', main: 'Go to Fleet', sub: 'All agents', h: 40, run: () => setView('fleet') },
    { kind: 'action', glyph: '▤', main: 'Go to Sessions', sub: 'Browse conversations', h: 196, run: () => setView('sessions') },
    { kind: 'action', glyph: '!', main: 'Go to System', sub: 'Notices and agent questions', h: 32, run: () => setView('system') },
    { kind: 'action', glyph: '✉', main: 'Open notifications drawer', sub: 'Quick triage', h: 32, run: openDrawer },
    { kind: 'action', glyph: '▸', main: 'Show running agents only', sub: 'Filters the fleet', h: 152, run: () => { S.runningOnly = true; setView('fleet'); } },
    { kind: 'action', glyph: '⤭', main: 'Show agent-started sessions', sub: 'Everything a dispatch opened', h: 268, run: () => { S.scope = 'agent'; S.pane = 'list'; setView('sessions'); } },
    { kind: 'action', glyph: '✕', main: 'Close every open tab', sub: 'Clears the working set, keeps the sessions', h: 0, run: closeAllTabs },
  ];
  S.agents.forEach((a) => items.push({
    kind: 'agent', glyph: a.glyph, main: a.name, h: hueOf(a),
    sub: `${a.slug} · ${modelLabel(a)} · ${a.status}`,
    run: () => openSheet(a.slug),
  }));
  S.sessions.forEach((s) => items.push({
    kind: 'session', glyph: agentBy(s.agent)?.glyph ?? '▤', h: hueOf(agentBy(s.agent)),
    main: s.title,
    sub: `${s.agent} · ${agoPhrase(s.updated_at)} · ${originPhrase(s)}${S.openTabs.includes(s.id) ? ' · open' : ''}`,
    run: () => openSession(s.id),
  }));
  S.notifs.filter((n) => n.state === 'open').forEach((n) => items.push({
    kind: 'system', glyph: NOTE_ICON[n.kind], h: 32,
    main: n.title, sub: `${n.from_agent} · needs an answer`,
    run: () => { S.sysFilter = 'all'; setView('system'); },
  }));
  return items;
}

function closeAllTabs() {
  if (!S.openTabs.length) return toast('No tabs are open');
  const n = S.openTabs.length;
  S.openTabs = [];
  S.activeSession = null;
  S.pane = 'list';
  renderTabs(); renderCrumbs();
  if (S.view === 'sessions') { renderChannels(); renderThread(); }
  toast(`Closed ${n} tab${n > 1 ? 's' : ''} — every session is still in the list`);
}

function renderCmd() {
  const q = $('#cmdInput').value.trim().toLowerCase();
  const all = cmdSources();
  S.cmdItems = (q
    ? all.filter((i) => `${i.main} ${i.sub} ${i.kind}`.toLowerCase().includes(q))
    : all).slice(0, 40);
  if (S.cmdSel >= S.cmdItems.length) S.cmdSel = 0;

  if (!S.cmdItems.length) {
    $('#cmdList').innerHTML = `<div class="cmd-none">Nothing matches “${esc(q)}”.<br>
      Try an agent name, a session title, or “new”.</div>`;
    return;
  }
  const order = ['action', 'agent', 'session', 'system'];
  const labels = { action: 'Actions', agent: 'Agents', session: 'Sessions', system: 'System' };
  let html = '';
  order.forEach((k) => {
    const group = S.cmdItems.filter((i) => i.kind === k);
    if (!group.length) return;
    html += `<div class="cmd-group">${labels[k]}</div>`;
    group.forEach((i) => {
      const idx = S.cmdItems.indexOf(i);
      html += `
        <button class="cmd-item ${idx === S.cmdSel ? 'is-sel' : ''}" style="--h:${i.h}"
                data-cmd="${idx}" role="option" aria-selected="${idx === S.cmdSel}">
          <span class="cmd-glyph" aria-hidden="true">${i.glyph}</span>
          <span class="cmd-id">
            <span class="cmd-main">${esc(i.main)}</span>
            <span class="cmd-sub">${esc(i.sub)}</span>
          </span>
          <span class="cmd-kind">${i.kind}</span>
        </button>`;
    });
  });
  $('#cmdList').innerHTML = html;
  $$('#cmdList .cmd-item').forEach((b) => b.onclick = () => runCmd(Number(b.dataset.cmd)));
  $('#cmdList .cmd-item.is-sel')?.scrollIntoView({ block: 'nearest' });
}

function runCmd(idx) {
  const item = S.cmdItems[idx];
  if (!item) return;
  closeModal('#cmdk');
  item.run();
}

/* ================================================================= MODALS */

let lastFocus = null;
function openModal(sel) {
  lastFocus = document.activeElement;
  $(sel).classList.add('is-open');
  $(sel).setAttribute('aria-hidden', 'false');
}
function closeModal(sel) {
  $(sel).classList.remove('is-open');
  $(sel).setAttribute('aria-hidden', 'true');
  lastFocus?.focus?.();
}
function anyOpen() {
  return $$('.modal.is-open').length || $('#drawer').classList.contains('is-open')
    || $('#sheet').classList.contains('is-open');
}
function closeTop() {
  const m = $$('.modal.is-open').pop();
  if (m) return closeModal(`#${m.id}`);
  if ($('#sheet').classList.contains('is-open')) return closeSheet();
  if ($('#drawer').classList.contains('is-open')) return closeDrawer();
}

/* ============================================================== LIVE FEED */

function paintPulse(ev) {
  const a = ev.agent ? agentBy(ev.agent) : null;
  const label = a ? esc(a.name) : 'Fleet';
  $('#pulseText').innerHTML = `<b>${label}</b> ${esc(ev.text)}`;
  const p = $('#pulse');
  p.classList.remove('is-fresh');
  void p.offsetWidth;
  p.classList.add('is-fresh');
}

async function tick() {
  try {
    const r = await api('pulse');
    paintPulse(r.event);
    const prev = S.stats;
    // The real pulse poll only carries `running` (a cheap count) — merge
    // rather than replace, or the Agents/Online tiles would vanish on the
    // very first tick.
    S.stats = { ...S.stats, ...r.stats, needs_you: sysCounts().needs };
    if (S.view === 'fleet' && prev.running !== S.stats.running) renderHeroStats();
  } catch {
    $('#pulseText').innerHTML = '<b>Platform unreachable</b> — the view still works on cached data';
    $('.pulse-dot').style.background = 'var(--crit)';
  }
}

/* ==================================================================== WIRE */

function wire() {
  // rail / nav
  $$('.rail-btn[data-view]').forEach((b) => b.onclick = () => {
    // Tapping Sessions while already there means "show me the list" — on a
    // narrow screen the thread pane is covering it.
    if (b.dataset.view === 'sessions' && S.view === 'sessions') S.pane = 'list';
    setView(b.dataset.view);
  });
  $('#railLogo').onclick = () => setView('fleet');
  $('#railNew').onclick = () => openPicker();
  $('#railKeys').onclick = () => openModal('#keys');
  $('#keysClose').onclick = () => closeModal('#keys');
  $('#wsChip').onclick = () => toast(`${S.workspace.label} · ${S.workspace.env} · ${S.workspace.region}`);

  // tab strip
  $('#tabNew').onclick = () => openPicker();

  // fleet filters
  $('#agentSearch').oninput = (e) => { S.q = e.target.value; renderFleet(); };
  $('#agentSearchClear').onclick = () => { S.q = ''; $('#agentSearch').value = ''; renderFleet(); $('#agentSearch').focus(); };
  $('#runningToggle').onclick = () => { S.runningOnly = !S.runningOnly; renderFleet(); };
  $('#fleetReset').onclick = () => {
    S.q = ''; S.squad = null; S.modelFilter.clear(); S.runningOnly = false;
    $('#agentSearch').value = ''; renderFleet(); toast('Filters reset');
  };

  // sessions
  $('#sessionSearch').oninput = (e) => { S.sessionQ = e.target.value; renderChannels(); };
  $$('#view-sessions .seg-btn[data-scope]').forEach((b) => b.onclick = () => {
    S.scope = b.dataset.scope; renderChannels();
  });
  $('#newSessionBtn').onclick = () => openPicker();
  $('#threadEmptyNew').onclick = () => openPicker();
  $('#threadEmptyFleet').onclick = () => setView('fleet');
  $('#threadBack').onclick = () => { S.pane = 'list'; renderThread(); renderCrumbs(); };
  $('#threadAgent').onclick = () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (s) openSheet(s.agent);
  };
  $('#threadPin').onclick = async () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (!s) return;
    s.pinned = !s.pinned;
    try { await api(`sessions/${s.id}/pin`, { method: 'POST' }); } catch { /* local session */ }
    renderThread(); renderChannels();
    toast(s.pinned ? 'Pinned to the top of the list' : 'Unpinned');
  };
  $('#threadArchive').onclick = async () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (!s) return;
    s.state = s.state === 'archived' ? 'open' : 'archived';
    try { await api(`sessions/${s.id}/archive`, { method: 'POST' }); } catch { /* local session */ }
    const archived = s.state === 'archived';
    toast(archived
      ? 'Archived — tab closed, find it under the Archived tab'
      : 'Restored to Open', 'ok');
    // Archiving takes it out of the working set too, like closing an editor.
    if (archived) closeTab(s.id, { quiet: true });
    renderTabs(); renderChannels(); renderThread(); renderCrumbs();
  };

  // composer
  const ta = $('#composerInput');
  const syncComposer = () => {
    $('#sendBtn').disabled = !ta.value.trim();
    ta.style.height = 'auto';
    ta.style.height = `${Math.min(180, ta.scrollHeight)}px`;
  };
  ta.oninput = syncComposer;
  ta.onkeydown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#composer').requestSubmit(); }
  };
  $('#composer').onsubmit = (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    ta.value = ''; syncComposer();
    sendMessage(text);
  };

  // system
  $$('#sysFilters .seg-btn').forEach((b) => b.onclick = () => {
    S.sysFilter = b.dataset.filter; renderSystem();
  });
  $('#sysShowAll').onclick = () => { S.sysFilter = 'all'; renderSystem(); };

  // drawer + bell
  $('#bellBtn').onclick = () => ($('#drawer').classList.contains('is-open') ? closeDrawer() : openDrawer());
  $('#drawerClose').onclick = closeDrawer;
  $('#drawerOpenPanel').onclick = () => { closeDrawer(); S.sysFilter = 'all'; setView('system'); };
  $('#scrim').onclick = () => { closeDrawer(); closeSheet(); };

  // picker
  $('#pickerClose').onclick = () => closeModal('#picker');
  $('#pickerBack').onclick = () => { S.pickerPick = null; renderPicker(); $('#pickerSearch').focus(); };
  $('#pickerStart').onclick = startSession;
  $('#pickerSearch').oninput = renderPicker;
  $('#pickerClear').onclick = () => { $('#pickerSearch').value = ''; renderPicker(); $('#pickerSearch').focus(); };
  $('#pickerKickoff').onkeydown = (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); startSession(); }
  };

  // command palette
  const openCmd = () => {
    S.cmdSel = 0; $('#cmdInput').value = ''; renderCmd();
    openModal('#cmdk'); $('#cmdInput').focus();
  };
  $('#cmdkTrigger').onclick = openCmd;
  $('#cmdInput').oninput = () => { S.cmdSel = 0; renderCmd(); };
  $('#cmdInput').onkeydown = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); S.cmdSel = Math.min(S.cmdSel + 1, S.cmdItems.length - 1); renderCmd(); }
    if (e.key === 'ArrowUp') { e.preventDefault(); S.cmdSel = Math.max(S.cmdSel - 1, 0); renderCmd(); }
    if (e.key === 'Enter') { e.preventDefault(); runCmd(S.cmdSel); }
  };

  // click outside a modal card closes it
  $$('.modal').forEach((m) => m.onclick = (e) => { if (e.target === m) closeModal(`#${m.id}`); });

  // global keys
  document.onkeydown = (e) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName);
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); return openCmd(); }
    if (e.key === 'Escape') { if (anyOpen()) { e.preventDefault(); closeTop(); } return; }

    // Tab controls carry a modifier so they still work from the composer —
    // the bare [ ] / W below are swallowed by the typing guard, which is
    // right for a text field but would make them unreachable where you
    // actually sit most of the time.
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      if (e.key === 'ArrowLeft') { e.preventDefault(); return cycleTab(-1); }
      if (e.key === 'ArrowRight') { e.preventDefault(); return cycleTab(1); }
      if (e.key.toLowerCase() === 'w') {
        e.preventDefault();
        if (S.activeSession) closeTab(S.activeSession);
        return;
      }
    }

    if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '1') setView('fleet');
    if (e.key === '2') setView('sessions');
    if (e.key === '3') setView('system');
    if (e.key === ']') { e.preventDefault(); cycleTab(1); }
    if (e.key === '[') { e.preventDefault(); cycleTab(-1); }
    if (e.key.toLowerCase() === 'w' && S.activeSession) { e.preventDefault(); closeTab(S.activeSession); }
    if (e.key.toLowerCase() === 'n') { e.preventDefault(); openPicker(); }
    if (e.key.toLowerCase() === 'b') { e.preventDefault(); $('#bellBtn').click(); }
    if (e.key === '?') openModal('#keys');
    if (e.key === '/') {
      e.preventDefault();
      const focusTarget = { fleet: '#agentSearch', sessions: '#sessionSearch', system: '#cmdkTrigger' }[S.view];
      $(focusTarget)?.focus();
    }
  };

  window.onhashchange = () => {
    const v = location.hash.replace('#', '');
    if (['fleet', 'sessions', 'system'].includes(v) && v !== S.view) setView(v, { silent: true });
  };
}

/* ==================================================================== BOOT */

async function boot() {
  let data;
  try {
    data = await api('bootstrap');
  } catch (err) {
    $('#boot').innerHTML = `
      <div class="empty" style="max-width:460px">
        <div class="empty-mark" style="color:var(--crit)">!</div>
        <h3>The Agents Platform did not answer</h3>
        <p>This app's backend could not reach the Agents Platform
           (<code>${esc(err.message)}</code>). Check the app's settings for a
           valid platform URL and token.</p>
        <button class="btn btn--primary" onclick="location.reload()">Retry</button>
      </div>`;
    return;
  }

  S.models = data.models;
  S.squads = data.squads;
  S.agents = data.agents;
  S.sessions = data.sessions;
  S.notifs = data.notifications;
  S.stats = data.stats;
  S.ticker = data.ticker;
  S.workspace = data.workspace;

  // Seed the working set with the three most recent open sessions, so the
  // strip shows what a real session of work would have left open. They come
  // from both scopes on purpose — the tab strip is not filtered.
  S.openTabs = S.sessions
    .filter((s) => s.state === 'open')
    .sort((a, b) => b.updated_at - a.updated_at)
    .slice(0, 3)
    .map((s) => s.id);

  $('#wsName').textContent = data.workspace.name;
  $('#wsEnv').textContent = data.workspace.env;

  wire();
  updateBadges();
  renderDrawer();
  $('#railSessionCount').textContent = S.sessions.filter((s) => s.state === 'open').length;

  const hash = location.hash.replace('#', '');
  setView(['fleet', 'sessions', 'system'].includes(hash) ? hash : 'fleet', { silent: true });

  paintPulse(S.ticker[0]);
  tick();
  setInterval(tick, 5200);

  await sleep(260);
  $('#boot').classList.add('is-gone');
  $('#app').hidden = false;
  setTimeout(() => $('#boot').remove(), 400);
}

boot();
