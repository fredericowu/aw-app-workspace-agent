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

   Fleet, Sessions and System are all wired to the live Agents Platform over
   RELATIVE fetch paths ("api/...") — this app's own backend serves the page
   at its mount root, so those resolve under /api/apps/workspace-agent/api/*.
   System's two feeds (agent questions, flow escalations) are read-only past
   the single inline-reply action: replying to a question is the only write,
   and it resumes the real asking run server-side — this app cannot observe
   that resume, only that the platform accepted the answer (see replyTo).

   WHAT A SESSION IS HERE — the thing most likely to mislead a reader of this
   file. A session is a real CLI session (the same one Telegram and every
   agent dispatch create), and A MESSAGE IS A RUN: your text is the run's
   input, the agent's reply is its output, and that run's own token counts and
   cost_usd are what the turn cost. So:

     * Real runs take 3-15 MINUTES, not milliseconds. The in-flight state is
       load-bearing, not decoration — see sendMessage/attachStream.
     * A new session has NO id until the CLI mints one. Until then the tab is
       keyed on `run:<run_id>` and a second send is QUEUED, not dispatched,
       because dispatching it without the session id would fork a second
       session instead of continuing this one. See PENDING/resolvePending.
     * Every send spends real money. Cost rides on the turn and on the
       session, because an interface that hides it is a product defect.
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

/** Real money, and small amounts of it — so never round a non-zero cost to
    "$0.00" and imply the turn was free. */
function money(usd) {
  const n = Number(usd || 0);
  if (!n) return '$0';
  if (n < 0.01) return `$${n.toFixed(4)}`;
  if (n < 1) return `$${n.toFixed(3)}`;
  return `$${n.toFixed(2)}`;
}

function tokens(n) {
  const v = Number(n || 0);
  if (v < 1000) return String(v);
  if (v < 1e6) return `${(v / 1000).toFixed(v < 10000 ? 1 : 0)}k`;
  return `${(v / 1e6).toFixed(1)}M`;
}

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

  // A draft never ran, so there is no session behind it and nothing in the
  // rail to go back to — closing the tab IS discarding it. Anything that
  // has run stays in the list, as the toast below promises.
  if (isDraft(id)) S.sessions = S.sessions.filter((s) => s.id !== id);

  // closing the active tab hands focus to its neighbour, like an editor
  if (S.activeSession === id) {
    const next = S.openTabs[i] ?? S.openTabs[i - 1] ?? null;
    S.activeSession = next;
    S.pane = next ? 'thread' : 'list';
  }
  renderTabs();
  renderCrumbs();
  if (S.view === 'sessions') { renderChannels(); renderThread(); }
  if (!quiet) {
    toast(isDraft(id)
      ? 'Draft discarded — it never dispatched a run'
      : 'Tab closed — the session is still in the list');
  }
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
  // A rail entry whose thread has never been opened has no messages, but the
  // backend already derived a preview from its newest run — use that rather
  // than claiming an active session is empty.
  if (!m) return s.preview || 'No messages yet — say something to get going.';
  if (m.pending) return 'Thinking…';
  return `${m.role === 'user' ? 'You: ' : ''}${String(m.text || '').replace(/\s+/g, ' ').slice(0, 90)}`;
}

/* --- in-flight runs -------------------------------------------------------

   Three things have to survive across turns, and none of them belongs on a
   session object that gets replaced wholesale by every rail refresh:

   PENDING  — session id => { runId, queue: [text] }. A session whose id has
              not been minted yet, or whose current turn is still running.
              `queue` is why a second message during a 10-minute run is held
              rather than dispatched: a send with no session_id forks a new
              session, and a send with one while the CLI is mid-turn queues
              behind the executor's per-session lock anyway.
   STREAMS  — run id => EventSource. Closed on tab close and on run end.
   POLLERS  — run id => interval handle. The belt-and-braces fallback, so a
              wedged stream cannot strand the UI in "thinking" forever.
*/

const PENDING = new Map();
const STREAMS = new Map();
const POLLERS = new Map();

const POLL_MS = 15000;   // run-row fallback poll while a run is in flight
const RAIL_MS = 10000;   // rail refresh — a Telegram session must show up fast

function pendingOf(id) { return PENDING.get(id) || null; }

function isDraft(id) { return String(id || '').startsWith('draft:'); }
function isUnresolved(id) { return String(id || '').startsWith('run:'); }

/** Does this agent's model run through a coding CLI?

    Only those mint a CLI session, which is the only kind of session this app
    shows — so an agent on a plain API model answers once and leaves nothing
    to resume. 84 of 92 agents here are CLI-backed; the other 8 are not, and
    the Fleet gallery offers all of them. */
function resumable(agent) {
  const m = S.models[agent?.model];
  return m ? m.resumable !== false : true;   // unknown model: assume it works
}

/** Can this session take a message right now? */
function sendState(s) {
  if (!s) return { ok: false, why: '' };
  if (s.retired) {
    return {
      ok: false,
      why: `This session was retired by the platform${s.retired_reason ? ` (${s.retired_reason})` : ''}`
        + ' — it cannot be resumed. Start a new one with the same agent.',
    };
  }
  // Confirmed un-resumable: the first run finished and the platform never
  // minted a session id. Sending still works — it just starts a fresh run
  // each time rather than continuing one.
  if (s.no_session) {
    return {
      ok: true,
      why: `${agentBy(s.agent)?.name ?? s.agent} does not run through a coding CLI, so there is no `
        + 'session to resume — each message is a standalone run, and this tab is the only place '
        + 'the thread is kept.',
    };
  }
  const p = pendingOf(s.id);
  if (p && isUnresolved(s.id)) {
    return { ok: 'queue', why: 'Waiting for the CLI to mint this session id — your next message is queued until it does.' };
  }
  // A running turn on a resolved session is NOT held here: the server queues
  // the message durably and sends it once the turn ends, tab open or not.
  if (p) return { ok: true, why: 'A turn is still running — your message is queued on the server and sent when it ends.' };
  return { ok: true, why: '' };
}

function stopWatching(runId) {
  const es = STREAMS.get(runId);
  if (es) { es.close(); STREAMS.delete(runId); }
  const t = POLLERS.get(runId);
  if (t) { clearInterval(t); POLLERS.delete(runId); }
}

function scopedSessions() {
  const q = S.sessionQ.trim().toLowerCase();
  return S.sessions.filter((s) => {
    if (!inScope(s, S.scope)) return false;
    if (!q) return true;
    const ag = agentBy(s.agent);
    // s.preview is in the haystack because most rail entries have never had
    // their thread loaded, so s.messages is empty for them — searching only
    // messages would silently match nothing outside the tabs you have open.
    const hay = [s.title, s.target, s.origin, s.preview, ag?.name, s.agent,
                 ...s.messages.map((m) => m.text)].join(' ').toLowerCase();
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
  loadThread(id);
}

/** Fetch a session's real transcript — every run, unsummarized.

    Lazy on purpose: the rail carries 100 sessions and a transcript is the
    one payload that is genuinely large. `loaded` makes a re-open free, and
    `loading` keeps a double-click from firing two fetches. */
async function loadThread(id) {
  const s = S.sessions.find((x) => x.id === id);
  if (!s || s.loaded || s.loading || isDraft(id) || isUnresolved(id)) return;
  s.loading = true;
  try {
    const r = await api(`sessions/${encodeURIComponent(id)}/thread`);
    Object.assign(s, r.session, { loading: false, unread: 0 });
    // Messages the server is holding for this session (sent while a turn was
    // running) — rehydrated here so a reload shows them instead of losing them.
    s.messages = s.messages.concat((r.session.queued || []).map(queuedMessage));
    // A session can be mid-turn when you open it — somebody else's dispatch,
    // or your own from before a reload. Pick the stream back up.
    if (r.session.live_run_id) {
      PENDING.set(id, { runId: r.session.live_run_id, queue: pendingOf(id)?.queue || [] });
      watchRun(id, r.session.live_run_id);
    } else {
      followQueue(s);
    }
  } catch (err) {
    s.loading = false;
    s.loadError = err.message;
  }
  if (S.activeSession === id) { renderThread(); }
  renderChannels();
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
  const cost = Number(s.cost_usd || 0);
  $('#threadMeta').innerHTML = `
    <span class="status status--${a?.status}"><i></i>${a?.status}</span>
    <span>${esc(a?.name ?? s.agent)}</span>
    <span class="mono">${esc(modelLabel(a))}</span>
    <span class="mono" title="${s.source === 'agent' ? 'Opened by a dispatch, not typed by you' : 'You opened this one yourself'}">${esc(originPhrase(s))}</span>
    ${s.run_count ? `<span class="mono" title="A message here is one platform run">${s.run_count} turn${s.run_count > 1 ? 's' : ''}</span>` : ''}
    ${cost ? `<span class="mono" title="Real spend on this session: ${tokens(s.tokens_in)} in / ${tokens(s.tokens_out)} out">${money(cost)}</span>` : ''}
    ${s.retired ? '<span class="tag tag--mono" title="The platform retired this session — it cannot be resumed">retired</span>' : ''}
    ${s.state === 'archived' ? '<span class="tag tag--mono">archived</span>' : ''}`;

  $('#threadPin').classList.toggle('is-on', !!s.pinned);
  $('#threadPin').setAttribute('aria-label', s.pinned ? 'Unpin session' : 'Pin session');
  $('#threadArchive').setAttribute('aria-label', s.state === 'archived' ? 'Unarchive session' : 'Archive session');
  $('#threadRename').disabled = isDraft(s.id) || isUnresolved(s.id);

  const send = sendState(s);
  $('#composerInput').disabled = send.ok === false;
  $('#composerHint').textContent = send.why
    || (s.state === 'archived'
      ? 'This session is archived — sending a message reopens it.'
      : isDraft(s.id)
        ? `Nothing has been dispatched yet. Your first message starts a real run with ${a?.name ?? s.agent}.`
        : 'Each message is a real run — typically 3-15 minutes, and it costs money.');

  const box = $('#msgs');
  let lastDay = null;
  box.innerHTML = s.messages.map((m) => {
    const d = dayLabel(m.at);
    const sep = d !== lastDay ? `<div class="day-sep">${esc(d)}</div>` : '';
    lastDay = d;
    return sep + msgHtml(m, a);
  }).join('') || (s.loading ? `
    <div class="empty empty--pane">
      <div class="empty-mark">◆</div>
      <h3>Loading the transcript…</h3>
      <p>Reading every run on this session from the platform.</p>
    </div>` : s.loadError ? `
    <div class="empty empty--pane">
      <div class="empty-mark" style="color:var(--crit)">!</div>
      <h3>Could not load this thread</h3>
      <p><code>${esc(s.loadError)}</code></p>
    </div>` : `
    <div class="empty empty--pane">
      <div class="empty-mark">◆</div>
      <h3>Nothing said yet</h3>
      <p>This session is open with ${esc(a?.name ?? s.agent)} but empty. Send the first message below.</p>
    </div>`);
  box.scrollTop = box.scrollHeight;
}

/** The cost line under an agent turn.

    Shown for every finished agent turn, including a zero-cost one: a visible
    "$0" is information (a cached or free-tier turn), while hiding the line
    makes an expensive turn and a free one look identical. */
function turnMeta(m) {
  if (m.role !== 'agent' || m.pending) return '';
  const bits = [
    `<span title="What this turn cost">${money(m.cost_usd)}</span>`,
    `<span title="Tokens in / out">${tokens(m.tokens_in)} ↓ ${tokens(m.tokens_out)} ↑</span>`,
  ];
  if (m.model) bits.push(`<span>${esc(m.model)}</span>`);
  if (m.status && m.status !== 'success') bits.push(`<span class="turn-bad">${esc(m.status)}</span>`);
  if (m.run_id) {
    bits.push(`<button class="turn-trace" data-trace="${esc(m.run_id)}"
                       title="Show this run's tool calls">tool trace</button>`);
  }
  return `<div class="turn-meta mono">${bits.join('<i>·</i>')}</div>`;
}

function msgHtml(m, a) {
  const mine = m.role === 'user';
  // An in-flight turn is the normal case here, not an edge case: the run it
  // is waiting on takes minutes. It gets the same bubble as a finished one so
  // the thread does not reflow when the answer lands.
  if (m.pending) {
    return `
      <div class="msg msg--agent msg--pending" style="--h:${hueOf(a)}" data-run="${esc(m.run_id || '')}">
        <span class="msg-av" aria-hidden="true">${a?.glyph ?? '◆'}</span>
        <div>
          <div class="msg-who">${esc(a?.name ?? 'Agent')}
            <span class="mono">${esc(m.phase || 'thinking')}… ${agoPhrase(m.at)}</span></div>
          <div class="msg-text">${m.text ? md(m.text)
            : '<span class="typing"><i></i><i></i><i></i></span>'}</div>
          ${m.trace ? `<div class="turn-meta mono"><span>${esc(m.trace)}</span></div>` : ''}
        </div>
      </div>`;
  }
  const hold = m.queued ? `
        <div class="turn-meta mono"><span class="${m.qstatus === 'failed' ? 'turn-bad' : ''}">${
          m.qstatus === 'failed'
            ? `Not sent: ${esc(m.error || 'dispatch failed')}`
            : 'Queued — will be sent when the current turn ends'}</span>
          <button class="turn-trace" data-cancel-queue="${esc(m.queue_id || '')}">${
            m.qstatus === 'failed' ? 'dismiss' : 'cancel'}</button></div>` : '';
  return `
    <div class="msg msg--${mine ? 'user' : 'agent'}${m.queued ? ' msg--queued' : ''}" style="--h:${hueOf(a)}"
         data-run="${esc(m.run_id || '')}">
      <span class="msg-av" aria-hidden="true">${mine ? 'F' : (a?.glyph ?? '◆')}</span>
      <div>
        <div class="msg-who">${mine ? 'You' : esc(a?.name ?? 'Agent')}
          <span class="mono">${clock(m.at)}</span></div>
        <div class="msg-text">${md(m.text) || '<span class="mono">(no reply text)</span>'}</div>
        ${hold}${turnMeta(m)}
      </div>
    </div>`;
}

/** A server-held message, shaped like a thread message. */
function queuedMessage(q) {
  return {
    role: 'user', text: q.text, at: q.at, queued: true,
    queue_id: q.queue_id, qstatus: q.status, error: q.error,
  };
}

/** After a turn ends, the server sends the next queued message on its own
    (within a few seconds). Reload the thread until it has, so the new run's
    stream is picked up — the queue itself never depends on this tab. */
async function followQueue(s) {
  const waiting = () => s.messages.some((m) => m.queued && m.qstatus !== 'failed');
  if (s.following || !waiting()) return;
  s.following = true;
  try {
    for (let i = 0; i < 40 && waiting(); i++) {
      await sleep(3000);
      s.loaded = false;
      await loadThread(s.id);
      if (PENDING.has(s.id)) break;   // loadThread re-attached to the live run
    }
  } finally { s.following = false; }
}

async function cancelQueued(queueId) {
  const s = S.sessions.find((x) => x.id === S.activeSession);
  if (!s) return;
  try {
    await api(`sessions/${encodeURIComponent(s.id)}/queue/${encodeURIComponent(queueId)}`, { method: 'DELETE' });
    s.messages = s.messages.filter((m) => m.queue_id !== queueId);
    toast('Queued message cancelled');
  } catch (err) {
    toast('It was already sent — cancel the run instead', 'warn');
    s.loaded = false; loadThread(s.id);
  }
  renderThread();
}

/** Send a message — i.e. dispatch a real agent run.

    The 2-second acceptance criterion is met by ORDER, not by speed: the user
    turn and the in-flight bubble are painted from local state before the
    fetch is awaited, and the dispatch endpoint returns its run_id without
    waiting for the run. Nothing here blocks on the 3-15 minutes that
    follow. */
async function sendMessage(text) {
  const s = S.sessions.find((x) => x.id === S.activeSession);
  if (!s || !text.trim()) return;

  const send = sendState(s);
  if (send.ok === false) { toast(send.why, 'warn'); return; }

  // A turn is already in flight (or the session id has not been minted yet):
  // hold this message instead of dispatching it. Dispatching now would
  // either fork a second session or just queue behind the executor's own
  // per-session lock with no feedback to the user.
  if (send.ok === 'queue') {
    const p = pendingOf(s.id);
    p.queue.push(text);
    s.messages.push({ role: 'user', text, at: Date.now() / 1000, queued: true });
    renderThread();
    toast(`Queued — ${p.queue.length} message${p.queue.length > 1 ? 's' : ''} waiting for this turn to finish`);
    return;
  }

  // Paint first. Everything below this point is already visible to the user.
  s.messages.push({ role: 'user', text, at: Date.now() / 1000 });
  s.messages.push({ role: 'agent', text: '', at: Date.now() / 1000, pending: true, phase: 'dispatching' });
  s.updated_at = Date.now() / 1000;
  if (s.state === 'archived') { s.state = 'open'; unarchive(s); }
  renderThread();
  renderChannels();

  // Dispatch WITHOUT a session_id when there is no session to continue: a
  // draft that has never run, or an agent that turned out not to mint one.
  const fresh = isDraft(s.id) || s.no_session;
  try {
    const r = fresh
      ? await api('sessions', {
        method: 'POST',
        body: JSON.stringify({ agent: s.agent, first_message: text }),
      })
      : await api(`sessions/${encodeURIComponent(s.id)}/messages`, {
        method: 'POST', body: JSON.stringify({ text }),
      });

    const live = s.messages[s.messages.length - 1];
    if (r.queued) {
      // The turn is still running: the server is holding this message and
      // will send it when the session is idle. No run exists yet, so drop the
      // in-flight bubble and show it as queued (cancellable).
      s.messages.pop();
      Object.assign(s.messages[s.messages.length - 1], {
        queued: true, queue_id: r.queue_id, qstatus: 'queued',
      });
      renderThread();
      toast('Queued — sent when the current turn ends');
      return;
    }
    live.run_id = r.run_id;
    live.phase = 'running';

    if (fresh) {
      // The run exists but its session does not have an id yet. Re-key the
      // tab onto the run so a reload (or the rail refresh) can find it, and
      // let the poller swap in the real session id when the CLI mints it.
      rekeySession(s, `run:${r.run_id}`);
    }
    PENDING.set(s.id, { runId: r.run_id, queue: pendingOf(s.id)?.queue || [] });
    watchRun(s.id, r.run_id);
    renderThread();
    renderChannels();
    renderTabs();
  } catch (err) {
    const live = s.messages[s.messages.length - 1];
    live.pending = false;
    live.text = `The dispatch was refused: \`${err.message}\`. Nothing is running — `
      + 'your message is still here, so you can retry it.';
    live.status = 'error';
    renderThread();
    toast('Dispatch refused', 'warn');
  }
}

/** Swap a session's id in place, keeping its tab and selection.

    Needed exactly once per new session: a draft becomes `run:<id>`, and then
    `run:<id>` becomes the real session id. Rewriting openTabs/activeSession
    rather than closing and reopening keeps the user's place. */
function rekeySession(s, nextId) {
  const prev = s.id;
  if (prev === nextId) return;
  const p = PENDING.get(prev);
  PENDING.delete(prev);
  s.id = nextId;
  s.session_id = isUnresolved(nextId) || isDraft(nextId) ? null : nextId;
  if (p) PENDING.set(nextId, p);
  S.openTabs = S.openTabs.map((id) => (id === prev ? nextId : id));
  if (S.activeSession === prev) S.activeSession = nextId;
}

/** Watch an in-flight run two independent ways.

    The stream is the good path — it carries the tool trace and the reply as
    it is written. The 15s poll exists because a stream that wedges (a proxy
    that buffers, a socket a sleeping laptop never reopens) is indistinguishable
    from an agent that is still thinking, and the failure mode is a UI stuck
    on "thinking" forever. Whichever notices the run ended first wins; both
    funnel into finishRun, which is idempotent. */
function watchRun(sessionId, runId) {
  attachStream(sessionId, runId);
  if (!POLLERS.has(runId)) {
    POLLERS.set(runId, setInterval(() => pollRun(sessionId, runId), POLL_MS));
  }
  // Poll once straight away too: a brand-new session's id can resolve within
  // seconds, and the tab should stop saying "waiting for an id" that fast.
  pollRun(sessionId, runId);
}

function attachStream(sessionId, runId) {
  if (STREAMS.has(runId)) return;
  let es;
  try {
    es = new EventSource(`api/runs/${encodeURIComponent(runId)}/stream`);
  } catch {
    return; // no EventSource — the poll alone still finishes the turn
  }
  STREAMS.set(runId, es);

  const live = () => {
    const s = S.sessions.find((x) => x.id === sessionId)
      || S.sessions.find((x) => x.session_id === sessionId);
    const m = s && s.messages[s.messages.length - 1];
    return m && m.pending && m.run_id === runId ? { s, m } : null;
  };

  const onEvent = (kind) => (ev) => {
    const ctx = live();
    if (!ctx) return;
    let payload = {};
    try { payload = JSON.parse(ev.data || '{}').payload || {}; } catch { /* keep going */ }
    if (kind === 'llm_token' && payload.delta) {
      ctx.m.text += payload.delta;
      ctx.m.phase = 'writing';
    } else if (kind === 'tool_call') {
      ctx.m.phase = 'working';
      ctx.m.trace = `${payload.name || 'tool'}`;
    } else if (kind === 'finalizing') {
      ctx.m.phase = 'finishing';
    } else if (kind === 'node_end' || kind === 'done') {
      // The reply text and the real cost both come off the run row, not off
      // the stream — one authoritative read instead of two half-truths.
      pollRun(sessionId, runId);
      return;
    }
    if (S.activeSession === ctx.s.id) renderThread();
  };

  ['llm_token', 'tool_call', 'tool_result', 'finalizing', 'node_end', 'done']
    .forEach((k) => es.addEventListener(k, onEvent(k)));

  // Do NOT reconnect by hand on error. EventSource reconnects natively, and
  // the upstream stream route replays every persisted event from the DB on
  // each connect — so the naive thing is also the lossless thing.
  es.addEventListener('error', () => { if (!live()) stopWatching(runId); });
}

async function pollRun(sessionId, runId) {
  let r;
  try {
    r = await api(`runs/${encodeURIComponent(runId)}`);
  } catch {
    return; // transient — the interval tries again
  }
  let s = S.sessions.find((x) => x.id === sessionId);

  // The CLI has minted the session id: re-key the tab onto it and pull the
  // real thread, which is now fetchable.
  if (r.session_id && s && isUnresolved(s.id)) {
    rekeySession(s, r.session_id);
    sessionId = r.session_id;
    renderTabs(); renderChannels(); renderCrumbs();
  }
  s = S.sessions.find((x) => x.id === sessionId);
  if (!s) return;

  if (r.done) { finishRun(s, runId, r); return; }
  const m = s.messages[s.messages.length - 1];
  if (m && m.pending && m.run_id === runId && !m.text && r.status === 'running') {
    m.phase = 'running';
    if (S.activeSession === s.id) renderThread();
  }
}

/** A run ended. Replace the in-flight bubble with the authoritative reply
    and its real cost, then release the queue. Safe to call twice — the
    stream and the poll race each other by design. */
function finishRun(s, runId, r) {
  stopWatching(runId);

  // The run is over and the platform never minted a session id, so one never
  // will: this agent does not run through a coding CLI. Record that on the
  // session so the composer can say so, and — critically — so the queue is
  // released instead of waiting forever for an id that is not coming.
  if (!r.session_id && isUnresolved(s.id)) s.no_session = true;

  // Found by run id, not by position: queued messages sit after it.
  const m = s.messages.find((x) => x.pending && x.run_id === runId);
  if (m) {
    Object.assign(m, {
      pending: false,
      text: r.text || (r.error ? `The run ended as \`${r.status}\`: ${r.error}` : ''),
      at: r.ended_at || Date.now() / 1000,
      status: r.status,
      cost_usd: r.cost_usd,
      tokens_in: r.tokens_in,
      tokens_out: r.tokens_out,
      model: r.model,
      error: r.error,
    });
    s.cost_usd = Number(s.cost_usd || 0) + Number(r.cost_usd || 0);
    s.tokens_in = Number(s.tokens_in || 0) + Number(r.tokens_in || 0);
    s.tokens_out = Number(s.tokens_out || 0) + Number(r.tokens_out || 0);
    s.run_count = Number(s.run_count || 0) + 1;
    s.updated_at = r.ended_at || Date.now() / 1000;
    s.last_status = r.status;
  }

  const p = PENDING.get(s.id);
  PENDING.delete(s.id);
  if (S.activeSession === s.id) renderThread();
  renderChannels(); renderTabs();

  followQueue(s);
  if (r.status === 'success') toast(`${agentBy(s.agent)?.name ?? s.agent} replied · ${money(r.cost_usd)}`, 'ok');
  else toast(`Run ended as ${r.status}`, 'warn');

  // Release one queued message. One, not all: each is a turn of its own and
  // the next can only go out once this session is free again.
  if (p && p.queue.length) {
    const next = p.queue.shift();
    const idx = s.messages.findIndex((x) => x.queued && x.text === next);
    if (idx >= 0) s.messages.splice(idx, 1);
    PENDING.delete(s.id);
    const was = S.activeSession;
    S.activeSession = s.id;
    sendMessage(next).finally(() => {
      const held = PENDING.get(s.id);
      if (held) held.queue.push(...p.queue);
      S.activeSession = was;
    });
  }
}

/** Show one run's tool calls, in-DOM under the turn — no native dialogs are
    available inside the window's iframe. */
async function showTrace(runId, btn) {
  const holder = btn.closest('.msg').querySelector('.trace-out');
  if (holder) { holder.remove(); return; }
  btn.disabled = true;
  let events = [];
  try { events = await api(`runs/${encodeURIComponent(runId)}/events`); } catch (err) {
    toast(`Could not read the trace: ${err.message}`, 'warn');
    btn.disabled = false;
    return;
  }
  btn.disabled = false;
  const calls = events.filter((e) => e.kind === 'tool_call');
  const out = document.createElement('div');
  out.className = 'trace-out mono';
  out.innerHTML = calls.length
    ? calls.map((e) => `<div>→ ${esc((e.payload || {}).name || 'tool')}</div>`).join('')
    : '<div>No tool calls on this run.</div>';
  btn.closest('.msg').querySelector('div').append(out);
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
  // Only an agent QUESTION gets a reply box — a flow escalation has no
  // reply action in v1 (it is resolved by working the run/card, not a
  // text answer), so it never had a real endpoint to pretend to call.
  const canReply = n.kind === 'question';
  const jump = ctx.session
    ? `<button class="btn btn--ghost btn--sm" data-goto-session="${esc(ctx.session)}">${canReply ? 'Open session →' : 'Open the run →'}</button>`
    : '';
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

        ${canReply && n.state === 'open' ? `
          <div class="note-reply">
            ${n.suggestions?.length ? `<div class="sugg">${n.suggestions.map((s) => `
              <button class="sugg-btn" data-sugg="${esc(s)}">${esc(s)}</button>`).join('')}</div>` : ''}
            <div class="reply-box">
              <textarea rows="1" data-reply-input placeholder="Reply to ${esc(a?.name ?? 'the agent')} — this resumes the run…" aria-label="Reply"></textarea>
              <button class="btn btn--primary btn--sm" data-send-reply disabled>Send</button>
            </div>
            <div class="note-foot">
              <span class="spacer"></span>
              ${jump}
            </div>
          </div>` : `
          <div class="note-foot" style="margin-top:12px">
            <span class="spacer"></span>
            ${jump}
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
    $$('[data-goto-agent]', card).forEach((b) => b.onclick = () => {
      closeDrawer(); openSheet(b.dataset.gotoAgent);
    });
    $$('[data-goto-session]', card).forEach((b) => b.onclick = () => {
      closeDrawer(); openSession(b.dataset.gotoSession);
    });
  });
}

/** Answer an agent question inline. Posts to the real
    POST /api/human-questions/{token}/answer alias (proxied), which resumes
    the asking run server-side with the answer as its next prompt — this app
    only learns that the platform accepted it, not that the resume actually
    ran; opening the session (see the "Open session →" jump button once
    context.session is known) is how you watch that happen. */
async function replyTo(id, text) {
  const n = S.notifs.find((x) => x.id === id);
  const value = text.trim();
  if (!n || !value) return;
  try {
    await api(`notifications/${id}/reply`, { method: 'POST', body: JSON.stringify({ text: value }) });
  } catch (err) {
    toast(`Could not send the answer: ${err.message}`, 'err');
    return;
  }
  n.replies.push({ role: 'user', text: value, at: Date.now() / 1000 });
  n.state = 'resolved';
  toast(`Answer sent — ${agentBy(n.from_agent)?.name ?? n.from_agent} is resuming`, 'ok');
  renderSystem();
  renderDrawer();
  renderHeroStats();
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
        ${resumable(a) ? '' : `<div style="font-size:11.5px;color:var(--warn,var(--ink-3));margin-top:3px">
          This one answers over a plain API call, not a coding CLI — so it leaves no
          resumable session. Each message will be a standalone run.</div>`}
      </span>`;
  } else {
    foot.hidden = true;
  }
}

let draftSeq = 0;

/** Open a tab for a new session.

    A CLI session does not exist until a run has created it, so there is
    nothing to dispatch and nothing to name until the first message. With a
    kickoff message typed, this dispatches immediately. Without one, it opens
    a DRAFT tab — real agent, no run, no session id — and the first send is
    what makes it real. Drafts are never persisted anywhere; closing the tab
    discards one, which is the right outcome for something that never ran. */
async function startSession() {
  const slug = S.pickerPick;
  if (!slug) return;
  const first = $('#pickerKickoff').value.trim();
  const a = agentBy(slug);

  const sess = {
    id: `draft:${++draftSeq}`, session_id: null, agent: slug,
    title: first ? first.slice(0, 58) : `New session · ${a.name}`,
    source: 'user', origin: null, state: 'open', pinned: false, unread: 0,
    target: null, updated_at: Date.now() / 1000, created_at: Date.now() / 1000,
    run_count: 0, cost_usd: 0, tokens_in: 0, tokens_out: 0,
    retired: false, preview: '', messages: [], loaded: true,
  };
  S.sessions.unshift(sess);
  closeModal('#picker');
  // land on the scope that actually contains what you just made
  S.scope = 'user';
  S.sessionQ = '';
  $('#sessionSearch').value = '';
  openSession(sess.id);

  if (first) {
    toast(`Dispatching to ${a.name}…`);
    await sendMessage(first);
  } else {
    toast(`Opened a tab with ${a.name} — send a message to start the run`, 'ok');
  }
}

/** Persist the app-local archive flag. Pin and archive are this app's own
    columns: there is no upstream archive route (CliSessionUpdate takes only
    name/description, and retired_at is the executor's death marker, not a
    user action). */
async function setFlag(s, flag, value) {
  if (isDraft(s.id) || isUnresolved(s.id)) return;   // nothing upstream to key on yet
  try {
    await api(`sessions/${encodeURIComponent(s.id)}/${flag}`, {
      method: 'POST', body: JSON.stringify({ value }),
    });
  } catch (err) {
    toast(`Could not save ${flag}: ${err.message}`, 'warn');
  }
}

function unarchive(s) { setFlag(s, 'archive', false); }

async function renameSession(name) {
  const s = S.sessions.find((x) => x.id === S.activeSession);
  if (!s || isDraft(s.id) || isUnresolved(s.id)) return;
  const prev = s.title;
  s.title = name || prev;
  renderThread(); renderChannels(); renderTabs(); renderCrumbs();
  try {
    const r = await api(`sessions/${encodeURIComponent(s.id)}`, {
      method: 'PATCH', body: JSON.stringify({ name }),
    });
    s.title = r.title || name;
    toast('Renamed — every consumer of this session sees it', 'ok');
  } catch (err) {
    s.title = prev;
    toast(`Rename failed: ${err.message}`, 'warn');
  }
  renderThread(); renderChannels(); renderTabs(); renderCrumbs();
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
    main: n.title, sub: `${n.from_agent} · ${n.kind === 'question' ? 'needs an answer' : 'needs a human'}`,
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

/** Refresh the rail from the live platform.

    This is how a session started somewhere else — Telegram, a dispatch, the
    CLI — shows up here, so it runs whichever view you are on rather than
    only on the Sessions tab.

    Two tiers. The default refresh costs the backend ONE upstream call; the
    authoritative one (`?full=1`) costs a second call that measured ~4.8s on
    its own, so it runs on first paint and then once a minute rather than
    every 10s. See sessions.build_rail's docstring for the numbers.

    MERGE, never replace. A wholesale swap would drop the client-only state
    the rail does not know about: drafts that have never run, a tab mid-turn
    whose id has not been minted, a loaded transcript, and the in-flight
    bubble itself. */
// Starts at 1, not 0: boot() already paid for a full build through
// /api/bootstrap, so the first poll 10s later should be the cheap one.
let railTicks = 1;
const FULL_RAIL_EVERY = 6;   // 6 x RAIL_MS = ~60s

async function refreshRail() {
  const full = (railTicks++ % FULL_RAIL_EVERY) === 0;
  let r;
  try { r = await api(`sessions${full ? '?full=1' : ''}`); } catch { return; }

  const mine = new Map(S.sessions.map((s) => [s.id, s]));
  const merged = r.sessions.map((fresh) => {
    const cur = mine.get(fresh.id);
    if (!cur) return fresh;
    mine.delete(fresh.id);
    // Keep the loaded transcript and anything in flight; take the rest.
    const keep = cur.loaded
      ? { messages: cur.messages, loaded: true, live_run_id: cur.live_run_id }
      : {};
    return { ...cur, ...fresh, ...keep };
  });

  // Whatever the platform did not list is either client-only (a draft, an
  // unresolved run) or has fallen off the end of the 100-row window. Keep
  // the first kind; a tab the user still has open counts as the first kind
  // too, because closing someone's tab from under them is worse than
  // carrying a stale row.
  for (const [id, s] of mine) {
    if (isDraft(id) || isUnresolved(id) || S.openTabs.includes(id)) merged.push(s);
  }

  merged.sort((a, b) => b.updated_at - a.updated_at);
  if (r.escalations) mergeNotes(r.escalations, 'esc:');
  S.sessions = merged;
  S.stats = { ...S.stats, sessions_open: merged.filter((s) => s.state === 'open').length };
  if (S.view === 'sessions') { renderChannels(); renderTabs(); }
  if (S.view === 'fleet') renderHeroStats();
}

const NOTIFY_MS = 15000;   // same cadence class as the rail poll

/** Fold one feed's fresh notes into S.notifs, leaving the other feed alone.
    Two feeds, two pollers: questions come from `notifications`, escalations
    ride on the rail response (same runs sweep — no second /api/runs call).
    `prefix` names the feed ('hq:' | 'esc:'). */
function mergeNotes(fresh, prefix) {
  const mine = new Map(S.notifs.filter((n) => n.id.startsWith(prefix)).map((n) => [n.id, n]));
  const merged = fresh.map((f) => {
    const cur = mine.get(f.id);
    mine.delete(f.id);
    return cur ? { ...cur, ...f } : f;
  });
  // An escalation that dropped out of the sweep resolved itself (its
  // `flow_needs_human` flag cleared) — keep it, marked resolved, rather than
  // vanishing a card the user may be mid-read on.
  if (prefix === 'esc:') for (const n of mine.values()) merged.push({ ...n, state: 'resolved' });
  S.notifs = S.notifs.filter((n) => !n.id.startsWith(prefix)).concat(merged)
    .sort((a, b) => b.at - a.at);
  updateBadges();
  if (S.view === 'system') renderSystem();
  renderDrawer();
}

/** Agent questions, from the live platform. `status=all` upstream, so an
    answered question stays reachable under the Resolved filter. */
async function refreshNotifications() {
  let r;
  try { r = await api('notifications'); } catch { return; }
  mergeNotes(r.notifications, 'hq:');
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
  $('#threadRename').onclick = () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (!s) return;
    $('#renamerInput').value = s.title || '';
    openModal('#renamer');
    $('#renamerInput').focus();
    $('#renamerInput').select();
  };
  $('#renamerClose').onclick = () => closeModal('#renamer');
  $('#renamerCancel').onclick = () => closeModal('#renamer');
  $('#renamerSave').onclick = () => {
    const name = $('#renamerInput').value.trim();
    closeModal('#renamer');
    renameSession(name);
  };
  $('#renamerInput').onkeydown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); $('#renamerSave').click(); }
  };

  // Tool traces are rendered inside message bubbles that renderThread()
  // rebuilds wholesale, so delegate rather than binding per button.
  $('#msgs').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-trace]');
    if (btn) showTrace(btn.dataset.trace, btn);
    const cq = e.target.closest('[data-cancel-queue]');
    if (cq) cancelQueued(cq.dataset.cancelQueue);
  });

  $('#threadPin').onclick = async () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (!s) return;
    s.pinned = !s.pinned;
    setFlag(s, 'pin', s.pinned);
    renderThread(); renderChannels();
    toast(s.pinned ? 'Pinned to the top of the list' : 'Unpinned');
  };
  $('#threadArchive').onclick = async () => {
    const s = S.sessions.find((x) => x.id === S.activeSession);
    if (!s) return;
    s.state = s.state === 'archived' ? 'open' : 'archived';
    setFlag(s, 'archive', s.state === 'archived');
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
  // The rail poll is the acceptance criterion for "a session started from
  // Telegram appears here within one refresh", so it runs on every view.
  setInterval(refreshRail, RAIL_MS);
  setInterval(refreshNotifications, NOTIFY_MS);

  await sleep(260);
  $('#boot').classList.add('is-gone');
  $('#app').hidden = false;
  setTimeout(() => $('#boot').remove(), 400);
}

boot();
