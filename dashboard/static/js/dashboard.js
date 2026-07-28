const state = {
  page: 1,
  hours: 3,
  q: '',
  person: '', org: '', location: '', theme: '',
  domain: '', outlet: '', language: '',
  date_from: '', date_to: '',
  view: '',
  source: 'gal', // 'gal' | 'gkg' | 'all'
  match_types: ['legal'], // ['legal'] = Strict, ['legal','stripped'] = Broad
  order: 'importance', // 'importance' (default, event-ranked) | 'date'
  sort: 'newest', // date-direction when order==='date': 'newest' | 'oldest'
  en_only: true, // English-only by default; ?en_only=0 to include all languages
};

// Unified feed — no source selection needed
state.source = 'all';

// Resolve initial match_types from URL, then localStorage, then default.
(function initMatchTypes() {
  const p = new URLSearchParams(location.search);
  const urlMT = (p.get('match_types') || '').toLowerCase();
  const lsMT = (localStorage.getItem('dashMatchTypes') || '').toLowerCase();
  const raw = urlMT || lsMT || 'legal';
  const parts = raw.split(',').map(s => s.trim()).filter(Boolean);
  const valid = parts.filter(t => ['legal','stripped','contextual'].includes(t));
  state.match_types = valid.length ? valid : ['legal'];
})();

// Restore more-filters drawer state from localStorage
(function initMoreFilters() {
  if (localStorage.getItem('moreFiltersOpen') === '1') {
    const drawer = document.getElementById('moreFiltersDrawer');
    const btn = document.getElementById('moreFiltersBtn');
    if (drawer) drawer.style.display = '';
    if (btn) btn.classList.add('active');
  }
})();

// The actual data window (populated from /api/stats)
const dataWindow = { earliest: null, latest: null };

// Loaded preset views, keyed by id
const viewsById = {};

let debounceTimer = null;

// Fetch coordinator: ensures only one /api/articles request is "visible" at a
// time, aborts superseded ones, and caps each request at 45s so a slow backend
// shows a real error instead of a phantom-restart spinner.
let currentFetchController = null;
let currentFetchGen = 0;
let currentFetchTimer = null; // elapsed counter setInterval id

// Initialize state.view from URL param if present
const _urlParams = new URLSearchParams(window.location.search);
if (_urlParams.get('view')) {
  state.view = _urlParams.get('view');
}
// ?hours= drives the initial window (and its chip) — required for the
// server-rendered first paint to describe the same combo the client fetches.
if (_urlParams.get('hours')) {
  const _h = _urlParams.get('hours');
  if (['3', '6', '24', '72', '168', '720'].includes(_h)) {
    state.hours = _h;
    document.querySelectorAll('.time-pill').forEach(p =>
      p.classList.toggle('active', p.dataset.hours === _h));
  }
}
if (_urlParams.get('en_only') === '0') {
  state.en_only = false;
}
if (_urlParams.get('order') === 'date') {
  state.order = 'date';
  const _s = _urlParams.get('sort');
  if (_s === 'oldest' || _s === 'newest') state.sort = _s;
}
// Reflect the resolved order/direction in the Sort dropdown.
(function syncSortSelect() {
  const sel = document.getElementById('sortSelect');
  if (sel) sel.value = state.order === 'date' ? state.sort : 'importance';
})();

// Time pills
document.getElementById('timePills').addEventListener('click', (e) => {
  if (!e.target.classList.contains('time-pill')) return;
  document.querySelectorAll('.time-pill').forEach(p => p.classList.remove('active'));
  e.target.classList.add('active');
  state.hours = e.target.dataset.hours;
  state.page = 1;
  state.date_from = '';
  state.date_to = '';
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  window.scrollTo({ top: 0 }); // make the transition visible
  fetchArticles();
});

// Hover-prefetch: warm the snapshot for a time pill before the click lands
// (current view, hovered hours). ~200-400ms head start on a real click.
document.getElementById('timePills').addEventListener('pointerenter', (e) => {
  const el = e.target.closest && e.target.closest('.time-pill');
  if (el && el.dataset.hours) prefetchCombo(state.view, el.dataset.hours);
}, true);

// English-only toggle (default on)
const _enBtn = document.getElementById('enOnlyToggle');
function syncEnBtn() {
  _enBtn.classList.toggle('active', state.en_only);
  _enBtn.setAttribute('aria-pressed', String(state.en_only));
  _enBtn.textContent = state.en_only ? '🌐 English only' : '🌐 All languages';
}
syncEnBtn();
_enBtn.addEventListener('click', () => {
  state.en_only = !state.en_only;
  state.page = 1;
  syncEnBtn();
  if (typeof updateUrl === 'function') updateUrl();
  fetchArticles();
});

// Fast, styled hover tooltip for view pills (shows the view's description).
// Hover-capable pointers only: on touch devices a tap fires emulated
// mouseover with no mouseout, leaving the tip stuck until the next tap.
const _pillTip = document.createElement('div');
_pillTip.className = 'pill-tip';
document.body.appendChild(_pillTip);
const _hoverCapable = window.matchMedia('(hover: hover) and (pointer: fine)').matches;
document.addEventListener('mouseover', (e) => {
  if (!_hoverCapable) return;
  const el = e.target.closest && e.target.closest('.view-pill[data-tip]');
  if (!el) return;
  _pillTip.textContent = el.dataset.tip;
  const r = el.getBoundingClientRect();
  _pillTip.style.left = Math.min(Math.max(8, r.left), window.innerWidth - 332) + 'px';
  _pillTip.style.top = (r.bottom + 6) + 'px';
  _pillTip.classList.add('show');
});
document.addEventListener('mouseout', (e) => {
  if (e.target.closest && e.target.closest('.view-pill[data-tip]')) _pillTip.classList.remove('show');
});
// Any click/tap dismisses the tip (covers hybrid touch+mouse devices too).
document.addEventListener('click', () => _pillTip.classList.remove('show'));

// Filter inputs. Map DOM id -> state field name.
const FILTER_INPUT_MAP = {
  searchInput: 'q',
  personInput: 'person',
  orgInput: 'org',
  locationInput: 'location',
  themeInput: 'theme',
  domainInput: 'domain',
  outletInput: 'outlet',
};
Object.entries(FILTER_INPUT_MAP).forEach(([id, field]) => {
  const el = document.getElementById(id);
  if (!el) return;
  el.addEventListener('input', (e) => {
    const val = e.target.value;
    // Free-text search hits an ILIKE scan server-side — a 1-2 char prefix is
    // never useful and just burns a ~2s query on every keystroke. Wait for a
    // real prefix (or a full clear) and give it a bit longer to settle.
    if (field === 'q' && val.trim().length > 0 && val.trim().length < 3) {
      clearTimeout(debounceTimer);
      state[field] = val;
      updateMoreFiltersDot();
      return; // don't fetch yet — wait for >=3 chars or a clear
    }
    state[field] = val;
    state.page = 1;
    updateMoreFiltersDot();
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchArticles, field === 'q' ? 450 : 300);
  });
});
document.getElementById('languageSelect').addEventListener('change', (e) => {
  state.language = e.target.value;
  state.page = 1;
  fetchArticles();
});
document.getElementById('sortSelect').addEventListener('change', (e) => {
  const v = e.target.value;
  if (v === 'importance') {
    state.order = 'importance';
  } else {
    state.order = 'date';
    state.sort = v; // 'newest' | 'oldest'
  }
  state.page = 1;
  if (typeof updateUrl === 'function') updateUrl();
  fetchArticles();
});

// Source tabs removed — unified feed

// switchSource removed — unified feed

function clearFields(stateKeys, domIds) {
  stateKeys.forEach(k => { state[k] = ''; });
  domIds.forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
}

function updateUrl() {
  const p = new URLSearchParams(location.search);
  p.delete('source');  // unified feed
  if (state.view) p.set('view', state.view); else p.delete('view');
  if (state.match_types && state.match_types.length && state.view) {
    p.set('match_types', state.match_types.join(','));
  } else {
    p.delete('match_types');
  }
  if (state.en_only) p.delete('en_only'); else p.set('en_only', '0');
  // Importance is the default → keep the URL clean; persist only explicit date order.
  if (state.order === 'date') {
    p.set('order', 'date');
    if (state.sort && state.sort !== 'newest') p.set('sort', state.sort); else p.delete('sort');
  } else {
    p.delete('order'); p.delete('sort');
  }
  history.replaceState(null, '', location.pathname + '?' + p.toString());
}

// Render the match-profile segmented control for the currently active
// view. Called from the view pill click handler and from init (via
// fetchViews). Data-driven: reads `available_match_types` off the view.
function renderMatchProfile() {
  // Show or hide the FDA Events panel based on whether an FDA view is active
  const curView = state.view ? viewsById[state.view] : null;
  if (curView && curView.kind === 'fda_match') {
    if (!_fdaEventsLoaded) loadFdaEvents(state.hours || 168);
  } else {
    hideFdaPanel();
  }
  const el = document.getElementById('matchProfile');
  const v = state.view && viewsById[state.view];
  if (!v || !Array.isArray(v.available_match_types) || v.available_match_types.length === 0) {
    el.classList.remove('visible');
    el.querySelectorAll('.profile-seg').forEach(b => b.remove());
    return;
  }
  el.querySelectorAll('.profile-seg').forEach(b => b.remove());
  const currentSet = new Set(state.match_types);
  const isActive = (profMt) => {
    if (profMt.length !== currentSet.size) return false;
    return profMt.every(t => currentSet.has(t));
  };
  v.available_match_types.forEach(prof => {
    const btn = document.createElement('button');
    btn.className = 'profile-seg' + (isActive(prof.match_types) ? ' active' : '');
    btn.textContent = prof.label;
    btn.title = prof.description || '';
    btn.dataset.profileId = prof.id;
    btn.addEventListener('click', () => {
      state.match_types = [...prof.match_types];
      localStorage.setItem('dashMatchTypes', state.match_types.join(','));
      state.page = 1;
      el.querySelectorAll('.profile-seg').forEach(b => {
        b.classList.toggle('active', b.dataset.profileId === prof.id);
      });
      updateUrl();
      fetchArticles();
    });
    el.appendChild(btn);
  });
  el.classList.add('visible');
}

['dateFrom', 'dateTo'].forEach(id => {
  const field = id === 'dateFrom' ? 'date_from' : 'date_to';
  document.getElementById(id).addEventListener('change', (e) => {
    state[field] = e.target.value;
    state.page = 1;
    // When picking a specific date range, clear the time pill so they don't conflict
    if (state.date_from || state.date_to) {
      state.hours = '';
      document.querySelectorAll('.time-pill').forEach(p => p.classList.remove('active'));
    }
    updateMoreFiltersDot();
    fetchArticles();
  });
});

function clearFilters() {
  state.q = '';
  state.person = ''; state.org = ''; state.location = ''; state.theme = '';
  state.domain = ''; state.outlet = ''; state.language = '';
  state.date_from = ''; state.date_to = '';
  state.view = '';
  state.page = 1;
  ['searchInput','personInput','orgInput',
   'locationInput','themeInput','domainInput','outletInput','dateFrom','dateTo'
  ].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  const ls = document.getElementById('languageSelect');
  if (ls) ls.value = '';
  document.querySelectorAll('.view-pill').forEach(p => p.classList.remove('active'));
  updateMoreFiltersDot();
  updateUrl();
  fetchArticles();
}

function toggleMoreFilters() {
  const drawer = document.getElementById('moreFiltersDrawer');
  const btn = document.getElementById('moreFiltersBtn');
  if (drawer.style.display === 'none') {
    drawer.style.display = '';
    btn.classList.add('active');
    localStorage.setItem('moreFiltersOpen', '1');
  } else {
    drawer.style.display = 'none';
    btn.classList.remove('active');
    localStorage.setItem('moreFiltersOpen', '0');
  }
}

function updateMoreFiltersDot() {
  const adv = ['person','org','location','theme','domain','outlet','date_from','date_to'];
  const active = adv.some(f => state[f]);
  const dot = document.getElementById('moreFiltersDot');
  const btn = document.getElementById('moreFiltersBtn');
  if (dot) dot.style.display = active ? '' : 'none';
  if (active) {
    const drawer = document.getElementById('moreFiltersDrawer');
    if (drawer && drawer.style.display === 'none') {
      drawer.style.display = '';
      if (btn) btn.classList.add('active');
    }
  }
}

function applyFilter(type, value) {
  const inputMap = {
    person: 'personInput', org: 'orgInput', organization: 'orgInput',
    location: 'locationInput', source: 'sourceInput', theme: 'themeInput'
  };
  const stateKey = type === 'organization' ? 'org' : type;
  const inputId = inputMap[type];
  if (inputId) {
    state[stateKey] = value;
    document.getElementById(inputId).value = value;
    state.page = 1;
    fetchArticles();
  }
}

function toneClass(tone) {
  if (!tone) return 'tone-neutral';
  if (tone.tone < -3) return 'tone-negative';
  if (tone.tone > 3) return 'tone-positive';
  return 'tone-neutral';
}

function toneLabel(tone) {
  if (!tone) return '';
  const v = tone.tone.toFixed(1);
  return v > 0 ? `+${v}` : v;
}

function renderActiveFilters() {
  const el = document.getElementById('activeFilters');
  const filters = [];
  const push = (label, field, inputId) => {
    filters.push({
      label,
      clear: () => {
        state[field] = '';
        const inp = inputId && document.getElementById(inputId);
        if (inp) inp.value = '';
      },
    });
  };
  if (state.q)           push(`search: ${state.q}`,           'q',           'searchInput');
  if (state.title)       push(`title: ${state.title}`,        'title',       'titleInput');
  if (state.description) push(`desc: ${state.description}`,   'description', 'descriptionInput');
  if (state.person)      push(`person: ${state.person}`,      'person',      'personInput');
  if (state.org)         push(`org: ${state.org}`,            'org',         'orgInput');
  if (state.location)    push(`place: ${state.location}`,     'location',    'locationInput');
  if (state.theme)       push(`theme: ${state.theme}`,        'theme',       'themeInput');
  if (state.domain)      push(`domain: ${state.domain}`,      'domain',      'domainInput');
  if (state.outlet)      push(`outlet: ${state.outlet}`,      'outlet',      'outletInput');
  if (state.language)    filters.push({
    label: `lang: ${state.language}`,
    clear: () => {
      state.language = '';
      const sel = document.getElementById('languageSelect');
      if (sel) sel.value = '';
    },
  });

  el.innerHTML = filters.map((f, i) =>
    `<span class="active-filter" data-idx="${i}">${f.label} &times;</span>`
  ).join('');

  el.querySelectorAll('.active-filter').forEach((tag, i) => {
    tag.addEventListener('click', () => {
      filters[i].clear();
      state.page = 1;
      fetchArticles();
    });
  });
}

function esc(s) {
  return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Time-window ladder for auto-widening an empty feed (3h is the floor).
const TIME_LADDER = [3, 6, 24, 72, 168, 720];
function hoursLabel(h) {
  return { 3:'3h', 6:'6h', 24:'24h', 72:'3 days', 168:'7 days', 720:'30 days' }[h] || (h + 'h');
}

function renderArticle(a) {
  const title = a.title || a.url.replace(/https?:\/\//, '').substring(0, 80) + '...';

  const rawDesc = (a.description || '').trim();
  const descHtml = rawDesc ? `<div class="article-desc">${esc(rawDesc)}</div>` : '';

  const img = (a.image || '').trim();
  const thumbHtml = img
    ? `<div class="article-thumb"><img src="${esc(img)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.closest('.article-thumb').remove()"></div>`
    : '';

  let matchBadge = '';
  if (a.matched_name) {
    const firstSpec = (a.matched_specialty || '').split('|')[0].trim();
    const specSuffix = firstSpec ? ` <span class="match-spec">(${firstSpec})</span>` : '';
    const verifiedBadge = a.matched_type === 'contextual'
      ? ' <span class="match-verified" title="Device context confirmed in article description">&#10003; verified</span>'
      : '';
    matchBadge = `<span class="match-badge" title="Matched FDA-registered company: ${esc(a.matched_name)}${a.matched_specialty ? ' — medical specialty: ' + esc(a.matched_specialty) : ''}"><span class="match-label">FDA co.</span> ${esc(a.matched_name)}${specSuffix}${verifiedBadge}</span>`;
  }

  // Transparency: why this article is in the current pill (from article_tags).
  let whyBadge = '';
  if (a.inclusion && a.inclusion.via) {
    const via = a.inclusion.via;
    const detail = a.inclusion.detail || '';
    if (via === 'judge') {
      const [verdict, sim] = detail.split('|');
      whyBadge = `<span class="why-badge" title="An LLM judge (gpt-oss-120b) read this article and judged it ${esc(verdict || 'relevant')} to this topic${sim ? ' (semantic similarity ' + esc(sim) + ')' : ''}. See /methodology.">&#10003; AI-judged: ${esc(verdict || 'relevant')}</span>`;
    } else if (via === 'semantic') {
      whyBadge = `<span class="why-badge" title="Matched this pill's description by meaning (cosine similarity ${esc(detail)}). See /methodology.">semantic match ${esc(detail)}</span>`;
    } else {
      whyBadge = `<span class="why-badge" title="Matched via ${esc(via)}: '${esc(detail)}'. Newly-ingested articles are AI-judged within ~15 minutes; non-English articles keep keyword matching. See /methodology.">${esc(via)}: ${esc(detail)}</span>`;
    }
  }

  let rollupHtml = '';
  if (a.variant_count && a.variant_count > 1) {
    const vid = 'var_' + (a.cluster_id || Math.random().toString(36).slice(2));
    const shown = (a.variants || []);
    const items = shown.map(v =>
      `<li style="padding:0.3rem 0; border-top:1px solid var(--border);">
         <span style="color:var(--accent-brand,#d8a657); font-size:0.78rem;">${esc(v.outlet_name || 'source')}</span>
         <span style="color:var(--text-tertiary); font-size:0.72rem; margin-left:0.4rem;">${v.time_ago || ''}</span>
         <a href="${v.url}" target="_blank" rel="noopener" style="display:block; font-size:0.82rem;">${esc(v.title || v.url)}</a>
       </li>`
    ).join('');
    const moreCount = a.variant_count - shown.length;
    const moreItem = moreCount > 0
      ? `<li style="padding:0.4rem 0; border-top:1px solid var(--border);"><a href="${a.event_url}">+ ${moreCount} more on the event page &rarr;</a></li>`
      : '';
    rollupHtml = `
      <div class="rollup" style="margin-top:0.4rem; font-size:0.8rem;">
        <button onclick="toggleVariants('${vid}')" style="background:none; border:1px solid var(--border); color:var(--text-tertiary); border-radius:3px; padding:0.1rem 0.5rem; cursor:pointer; font-family:inherit; font-size:0.74rem;">
          &#9636; covered by ${a.variant_count} sources
        </button>
        <a href="${a.event_url}" style="margin-left:0.5rem; font-size:0.74rem;">view event &rarr;</a>
        <ul id="${vid}" style="display:none; list-style:none; margin:0.5rem 0 0; padding:0;">${items}${moreItem}</ul>
      </div>`;
  }

  return `<li class="article">
    <div class="article-row">
      ${thumbHtml}
      <div class="article-body">
        <div class="article-header">
          <span class="article-source">${esc(a.source)}</span>
          <span class="article-time">${a.time_ago}</span>
        </div>
        <div class="article-title"><a href="${a.url}" target="_blank" rel="noopener">${esc(title)}</a></div>
        ${descHtml}
        ${matchBadge || whyBadge ? `<div class="match-line">${matchBadge}${whyBadge}</div>` : ''}
        ${rollupHtml}
      </div>
    </div>
  </li>`;
}

function toggleVariants(id) {
  const el = document.getElementById(id);
  if (el) el.style.display = (el.style.display === 'none') ? 'block' : 'none';
}

function renderPagination(data) {
  const el = document.getElementById('pagination');
  if (data.pages <= 1) { el.innerHTML = ''; return; }

  el.innerHTML = `
    <button ${data.page <= 1 ? 'disabled' : ''} onclick="goPage(${data.page - 1})">&larr; prev</button>
    <span class="page-info">${data.page} of ${data.pages}</span>
    <button ${data.page >= data.pages ? 'disabled' : ''} onclick="goPage(${data.page + 1})">next &rarr;</button>
  `;
}

function goPage(p) {
  state.page = p;
  fetchArticles();
  window.scrollTo(0, 0);
}

// Instant-paint: persist the last clean (unfiltered, page-1) feed + briefing to
// localStorage so the app shows content the instant it opens, then refreshes.
function _snapKey(overrides) {
  const view = (overrides && overrides.view !== undefined) ? overrides.view : state.view;
  const hours = (overrides && overrides.hours !== undefined) ? overrides.hours : state.hours;
  const hasFilters = state.q || state.person || state.org || state.location ||
    state.theme || state.domain || state.outlet || state.date_from || state.date_to || state.page > 1;
  if (hasFilters) return null;
  const ord = state.order === 'date' ? 'date-' + state.sort : 'importance';
  return `snap:${view}|${hours}|${state.en_only ? 1 : 0}|${ord}`;
}
function saveSnapshot() {
  const k = _snapKey(); if (!k) return;
  try {
    const bp = document.getElementById('briefingPanel');
    localStorage.setItem(k, JSON.stringify({
      ts: Date.now(),
      feed: document.getElementById('articleList').innerHTML,
      briefing: (document.getElementById('briefingText') || {}).innerHTML || '',
      briefingMeta: (document.getElementById('briefingMeta') || {}).textContent || '',
      briefingShown: bp ? bp.style.display !== 'none' : false,
    }));
  } catch (e) {}
}
function restoreSnapshot() {
  const k = _snapKey(); if (!k) return false;
  try {
    const raw = localStorage.getItem(k); if (!raw) return false;
    const s = JSON.parse(raw);
    if (!s.feed || Date.now() - s.ts > 6 * 3600 * 1000) return false; // skip if stale (>6h)
    const list = document.getElementById('articleList');
    list.innerHTML = s.feed;
    list.classList.remove('is-stale');
    list.dataset.snapKey = k;
    if (s.briefingShown && s.briefing) {
      document.getElementById('briefingPanel').style.display = '';
      const bt = document.getElementById('briefingText');
      bt.innerHTML = s.briefing;
      bt.dataset.key = `${state.view}|${state.hours}`; // lets fetchBriefing keep it visible
      document.getElementById('briefingMeta').textContent = s.briefingMeta || '';
    }
    return true;
  } catch (e) { return false; }
}

// Prefetch (view,hours) combos into the snapshot store WITHOUT touching the
// DOM, so hovering a pill (or idling on page load) can make the eventual
// click instant-paint. Never overwrites an already-fresh (<6h) snapshot —
// a real visit's snapshot (with its briefing) always wins over a prefetch.
const _prefetchInFlight = new Set();
async function prefetchCombo(view, hours) {
  const key = _snapKey({ view, hours });
  if (!key || _prefetchInFlight.has(key)) return;
  try {
    const raw = localStorage.getItem(key);
    if (raw) {
      const s = JSON.parse(raw);
      if (s.feed && Date.now() - s.ts < 6 * 3600 * 1000) return; // already fresh
    }
  } catch (e) {}
  _prefetchInFlight.add(key);
  try {
    const params = buildArticleParams(Object.assign({}, state, { view, hours, page: 1 }));
    const resp = await fetch(`/api/articles?${params}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (!data.articles || !data.articles.length) return;
    localStorage.setItem(key, JSON.stringify({
      ts: Date.now(),
      feed: data.articles.map(renderArticle).join(''),
      briefing: '', briefingMeta: '', briefingShown: false,
    }));
  } catch (e) {
    // best-effort background warm — a failure here is invisible to the user
  } finally {
    _prefetchInFlight.delete(key);
  }
}

// --- Transparency: "how this was made" for the AI briefing -----------------
async function showBriefingInfo() {
  const params = new URLSearchParams();
  if (state.view) params.set('view', state.view);
  params.set('hours', state.hours || 24);
  let data;
  try {
    const resp = await fetch(`/api/briefing_meta?${params}`);
    data = await resp.json();
    if (!resp.ok) throw new Error(data.error || 'no meta');
  } catch (err) {
    alert('No generation details available yet — open the briefing first. (' + err.message + ')');
    return;
  }
  const m = data.meta || {};
  const age = data.age_s != null ? (data.age_s < 90 ? `${data.age_s}s ago` : `${Math.round(data.age_s / 60)}m ago`) : '?';
  const srcRows = (data.sources || []).map(s =>
    `<tr><td style="text-align:right;color:var(--text-tertiary);padding-right:.5rem;">${s.n}</td>` +
    `<td style="padding-right:.5rem;white-space:nowrap;color:var(--accent-brand);">${s.n_sources || 1}&times;</td>` +
    `<td><a href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.title || s.link)}</a></td></tr>`
  ).join('');
  const cont = (m.continuity_titles || []).map(t => `<li>${esc(t)}</li>`).join('');
  const threads = (m.threads_used || []).map(t =>
    `<li><strong>${esc(t.title || '')}</strong> (since ${esc(t.first_seen || '?')}): ${esc(t.summary || '')}</li>`).join('');

  const modal = document.createElement('div');
  modal.className = 'pill-modal';
  modal.innerHTML = `
    <div class="pill-modal-content" style="max-width:760px;max-height:85vh;overflow-y:auto;">
      <h3>How this briefing was made</h3>
      <div style="font-size:0.82rem;line-height:1.5;">
        <p><strong>Model:</strong> ${esc(m.model || 'unknown (generated before metadata capture)')} ·
           <strong>Generated:</strong> ${esc(data.generated_at || '?')} UTC (${age}) ·
           <strong>Cache:</strong> ${m.cache_ttl_s ? Math.round(m.cache_ttl_s / 60) + ' min' : '45 min'}</p>
        <p>The briefing summarizes the window's <strong>top ${data.article_count || '?'} events ranked by the same
           Importance score as the feed</strong> (coverage 0.5 &middot; recency 0.3 &middot; velocity 0.2 —
           <a href="/methodology" target="_blank">full methodology</a>).</p>
        ${cont ? `<details><summary style="cursor:pointer;">Continuity: stories the previous briefing covered (${(m.continuity_titles || []).length})</summary><ul style="margin:.4rem 0 .4rem 1.1rem;">${cont}</ul></details>` : ''}
        ${threads ? `<details><summary style="cursor:pointer;">Ongoing story threads it was tracking (${(m.threads_used || []).length})</summary><ul style="margin:.4rem 0 .4rem 1.1rem;">${threads}</ul></details>` : ''}
        <details><summary style="cursor:pointer;">The ${(data.sources || []).length} ranked source events it was given</summary>
          <div style="max-height:220px;overflow-y:auto;margin:.4rem 0;"><table style="font-size:0.78rem;border-collapse:collapse;">${srcRows}</table></div>
        </details>
        ${m.prompt ? `<details><summary style="cursor:pointer;"><strong>The verbatim prompt</strong> (exactly what the model received)</summary>
          <pre style="white-space:pre-wrap;font-size:0.72rem;max-height:300px;overflow-y:auto;background:var(--bg,rgba(127,127,127,.08));padding:.6rem;border-radius:6px;">${esc(m.prompt)}</pre>
        </details>` : '<p style="color:var(--text-tertiary);">Prompt not stored for this briefing (generated before metadata capture) — refresh the briefing to capture it.</p>'}
      </div>
      <div class="pill-modal-actions">
        <button class="btn-cancel" onclick="this.closest('.pill-modal').remove()">Close</button>
      </div>
    </div>`;
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

// Transparency: plain-words explanation of the Importance ordering.
function showSortInfo(ev) {
  ev.stopPropagation();
  const tip = document.createElement('div');
  tip.className = 'pill-modal';
  tip.innerHTML = `
    <div class="pill-modal-content" style="max-width:460px;">
      <h3>How "Importance" ordering works</h3>
      <div style="font-size:0.84rem;line-height:1.55;">
        <p>Every event in your time window gets a score:</p>
        <ul style="margin:0 0 .6rem 1.1rem;">
          <li><strong>Coverage (50%)</strong> — how many outlets reported it</li>
          <li><strong>Recency (30%)</strong> — newer events score higher (12h decay)</li>
          <li><strong>Velocity (20%)</strong> — how fast coverage is growing</li>
        </ul>
        <p>No editors, no personalization — everyone sees the same ranking.
           "Newest/Oldest" simply sort by crawl time.
           <a href="/methodology" target="_blank">Full methodology &rarr;</a></p>
      </div>
      <div class="pill-modal-actions">
        <button class="btn-cancel" onclick="this.closest('.pill-modal').remove()">Close</button>
      </div>
    </div>`;
  tip.addEventListener('click', (e) => { if (e.target === tip) tip.remove(); });
  document.body.appendChild(tip);
}

// Skeleton placeholder cards shown while a different view/window loads —
// honest "new content coming" instead of another view's stale articles.
function skeletonCards(n = 8) {
  const card =
    '<li class="skeleton-card" aria-hidden="true">' +
      '<div class="skel-block skel-thumb"></div>' +
      '<div class="skel-lines">' +
        '<div class="skel-block skel-line w85"></div>' +
        '<div class="skel-block skel-line w55"></div>' +
        '<div class="skel-block skel-line thin w95"></div>' +
        '<div class="skel-block skel-line thin w70"></div>' +
        '<div class="skel-block skel-line thin w35"></div>' +
      '</div>' +
    '</li>';
  return card.repeat(n);
}

// Build /api/articles query params from a state-like object. Shared by
// fetchArticles (full state) and prefetchCombo (a minimal {view,hours}
// override layered on the current state) so the two never drift apart.
function buildArticleParams(s) {
  const params = new URLSearchParams();
  if (s.hours) params.set('hours', s.hours);
  if (s.view) params.set('view', s.view);
  if (s.match_types && s.match_types.length) {
    params.set('match_types', s.match_types.join(','));
  }
  if (s.q) params.set('q', s.q);
  if (s.title) params.set('title', s.title);
  if (s.description) params.set('description', s.description);
  if (s.person) params.set('person', s.person);
  if (s.org) params.set('org', s.org);
  if (s.location) params.set('location', s.location);
  if (s.theme) params.set('theme', s.theme);
  if (s.domain) params.set('domain', s.domain);
  if (s.outlet) params.set('outlet', s.outlet);
  if (s.language) params.set('language', s.language);
  params.set('en_only', s.en_only ? '1' : '0');
  if (s.date_from) params.set('date_from', s.date_from);
  if (s.date_to) params.set('date_to', s.date_to);
  // Importance is the backend default → send nothing so the key matches the
  // pre-warmed feed. Only the explicit date order (and its direction) is sent.
  if (s.order === 'date') {
    params.set('order', 'date');
    if (s.sort && s.sort !== 'newest') params.set('sort', s.sort);
  }
  params.set('page', s.page || 1);
  params.set('per_page', 50);
  return params;
}

async function fetchArticles() {
  renderActiveFilters();

  // --- Switch-aware loading state. Runs BEFORE the briefing kicks off so a
  // snapshot paint can restore the matching briefing too. Three cases:
  //   1. snapshot exists for the target combo  -> instant-paint, quiet refresh
  //   2. different combo, no snapshot          -> skeleton cards
  //   3. same combo (filters/auto-refresh)     -> keep content, dim as stale
  const list = document.getElementById('articleList');
  const snapKey = _snapKey(); // non-null only for clean page-1 views
  const effKey = snapKey || `filtered:${state.view}|${state.hours}`;
  const prevKey = list.dataset.snapKey || '';
  let painted = false;
  if (snapKey && snapKey !== prevKey) painted = restoreSnapshot();
  if (!painted) {
    if (effKey !== prevKey || !list.querySelector('li.article')) {
      list.classList.remove('is-stale');
      list.innerHTML = skeletonCards();
      list.classList.add('anim-next'); // animate the next real render in
    } else {
      list.classList.add('is-stale');
    }
  }
  list.dataset.snapKey = effKey;

  if (typeof fetchBriefing === "function") fetchBriefing();

  const params = buildArticleParams(state);

  if (currentFetchController) {
    try { currentFetchController.abort(); } catch (_) {}
  }
  if (currentFetchTimer) {
    clearInterval(currentFetchTimer);
    currentFetchTimer = null;
  }

  const myGen = ++currentFetchGen;
  const controller = new AbortController();
  currentFetchController = controller;
  const hardTimeout = setTimeout(() => {
    try { controller.abort('client-timeout'); } catch (_) {}
  }, 45000);

  const fetchStart = performance.now();
  document.body.classList.add('fetching');
  document.getElementById('topProgress').classList.add('active');
  // Elapsed feedback lives in the results-meta line (works alongside skeletons
  // and kept-visible content alike).
  const resultsMetaEl = document.getElementById('resultsMeta');
  currentFetchTimer = setInterval(() => {
    if (myGen !== currentFetchGen) { clearInterval(currentFetchTimer); return; }
    const s = ((performance.now() - fetchStart) / 1000).toFixed(1);
    if (resultsMetaEl) resultsMetaEl.textContent = `Loading… ${s}s${s > 5 ? ' — still working' : ''}`;
  }, 100);
  const finishLoading = () => {
    if (myGen !== currentFetchGen) return; // superseded, leave UI alone
    if (currentFetchTimer) { clearInterval(currentFetchTimer); currentFetchTimer = null; }
    clearTimeout(hardTimeout);
    document.body.classList.remove('fetching');
    document.getElementById('topProgress').classList.remove('active');
    list.classList.remove('is-stale');
    if (resultsMetaEl && /^Loading…/.test(resultsMetaEl.textContent)) resultsMetaEl.textContent = '';
    if (currentFetchController === controller) currentFetchController = null;
  };

  try {
    const resp = await fetch(`/api/articles?${params}`, { signal: controller.signal });
    if (myGen !== currentFetchGen) return; // a newer fetch has started; drop
    if (!resp.ok) {
      const body = await resp.text();
      throw new Error(`server returned ${resp.status} (${body.slice(0, 80)})`);
    }
    const data = await resp.json();
    if (myGen !== currentFetchGen) return;

    const meta = document.getElementById('resultsMeta');

    if (data.error) {
      meta.textContent = '';
      list.innerHTML = `<li class="loading">${data.error}<br><small>The dashboard will populate once the backfill finishes. Refresh in a minute.</small></li>`;
      document.getElementById('pagination').innerHTML = '';
      finishLoading();
      return;
    }

    meta.textContent = `${data.total.toLocaleString()} articles`;

    if (data.articles.length === 0) {
      // Auto-widen: an empty time window (with no custom date range) steps out to
      // the next wider window until articles appear — covers the default + sparse pills.
      const curH = parseInt(state.hours, 10);
      const li = TIME_LADDER.indexOf(curH);
      if (data.total === 0 && !state.date_from && !state.date_to && li > -1 && li < TIME_LADDER.length - 1) {
        if (window.__widenFrom == null) window.__widenFrom = curH;
        state.hours = String(TIME_LADDER[li + 1]);
        document.querySelectorAll('.time-pill').forEach(p => p.classList.toggle('active', p.dataset.hours === state.hours));
        if (typeof updateUrl === 'function') updateUrl();
        finishLoading();
        fetchArticles();
        return;
      }
      window.__widenFrom = null;
      // Check if the date range is (partly) outside the data window
      let hint = '';
      if (dataWindow.earliest && dataWindow.latest) {
        const outsideBefore = state.date_from && state.date_from < dataWindow.earliest;
        const outsideAfter = state.date_to && state.date_to > dataWindow.latest;
        if (outsideBefore || outsideAfter) {
          hint = `<br><small>Your date range is outside the available data window (${dataWindow.earliest} to ${dataWindow.latest}). The pipeline keeps a rolling 60 days.</small>`;
        }
      }
      list.innerHTML = `<li class="loading">No articles match these filters.${hint}</li>`;
    } else {
      let noticeHtml = '';
      if (window.__widenFrom != null && String(window.__widenFrom) !== String(state.hours)) {
        noticeHtml = `<li class="feed-notice">No articles in the last ${hoursLabel(window.__widenFrom)} — showing the last ${hoursLabel(parseInt(state.hours, 10))}.</li>`;
      }
      window.__widenFrom = null;
      list.innerHTML = noticeHtml + data.articles.map(renderArticle).join('');
      if (list.classList.contains('anim-next')) {
        list.classList.remove('anim-next');
        list.classList.add('anim-in'); // staggered card entrance (CSS)
        setTimeout(() => list.classList.remove('anim-in'), 700);
      }
      if (window.perfMark) {
        window.perfMark('feed_ms', performance.now() - fetchStart);
        if (!window.__ttaSent) { window.__ttaSent = true; window.perfMark('time_to_articles', performance.now()); }
      }
      saveSnapshot();
    }

    renderPagination(data);
  } catch (err) {
    if (myGen !== currentFetchGen) return; // a newer fetch superseded us
    if (err.name === 'AbortError') {
      // Either client-timeout or user action. If it was us hitting 45s, show
      // an actionable message; if user triggered another fetch, they'll see
      // the new spinner so we don't need to render anything here.
      if (currentFetchController === controller) {
        list.innerHTML = `<li class="loading">Request timed out after 45s — the backend is overloaded or the query is hung.<br><small>Try a narrower time window (1h or 6h), or wait a minute and retry.</small></li>`;
      }
    } else {
      list.innerHTML = `<li class="loading">Error loading articles: ${err.message}<br><small>The backend may be restarting. Try again in a few seconds.</small></li>`;
    }
  } finally {
    finishLoading();
  }
}

function compact(n) {
  if (n == null) return '…';
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return n.toString();
}

async function fetchStats() {
  try {
    const resp = await fetch('/api/stats');
    const data = await resp.json();
    if (data.error) {
      document.getElementById('stats').innerHTML = `<span>backfill in progress — loading initial 60 days of data</span>`;
      return;
    }
    document.getElementById('stats').innerHTML =
      `<span>${data.total_articles.toLocaleString()} articles</span>` +
      `<span>${data.sources.toLocaleString()} sources</span>` +
      `<span>window: ${data.earliest_display} → ${data.latest_display}</span>` +
      `<span>latest: ${data.latest_ago}</span>`;

    // Source tab counts removed — unified feed

    // Constrain date pickers to the actual data window
    if (data.earliest_date && data.latest_date) {
      const df = document.getElementById('dateFrom');
      const dt = document.getElementById('dateTo');
      df.min = data.earliest_date;
      df.max = data.latest_date;
      dt.min = data.earliest_date;
      dt.max = data.latest_date;
      dataWindow.earliest = data.earliest_date;
      dataWindow.latest = data.latest_date;
    }
  } catch (err) {
    document.getElementById('stats').textContent = 'stats unavailable';
  }
}

async function fetchGalFacets() {
  try {
    const resp = await fetch('/api/gal_facets');
    const data = await resp.json();
    if (data.error) return;
    const sel = document.getElementById('languageSelect');
    if (!sel) return;
    const current = state.language;
    sel.innerHTML = '<option value="">any</option>' +
      (data.languages || []).map(l =>
        `<option value="${l.code}">${l.name} (${compact(l.count)})</option>`
      ).join('');
    if (current) sel.value = current;
  } catch (_) { /* non-fatal */ }
}

async function fetchViews() {
  try {
    const resp = await fetch('/api/views');
    const data = await resp.json();
    const bar = document.getElementById('viewsBar');
    bar.innerHTML = '';
    const GROUP_ORDER = ["Technology & AI", "Security", "Health & Science", "World & Economy"];
    const groups = {};
    (data.views || []).forEach(v => {
      const g = v.group || 'Other';
      if (!groups[g]) groups[g] = [];
      groups[g].push(v);
      viewsById[v.id] = v;
    });
    const orderedGroups = [
      ...GROUP_ORDER.filter(g => groups[g]),
      ...Object.keys(groups).filter(g => !GROUP_ORDER.includes(g)),
    ];
    for (const group of orderedGroups) {
      const pills = groups[group];
      if (!pills || !pills.length) continue;
      const label = document.createElement('div');
      label.className = 'pill-group-label';
      label.textContent = group;
      bar.appendChild(label);
      for (const v of pills) {
      const btn = document.createElement('button');
      btn.className = 'view-pill' + (state.view === v.id ? ' active' : '');
      // no-op; segmented control is rendered after the loop
      btn.textContent = v.name;
      if (v.description) btn.dataset.tip = v.description;
      btn.dataset.viewId = v.id;

      // Hover-prefetch: warm the snapshot for what a click would actually
      // select (mirrors the click handler's default_hours snap below).
      btn.addEventListener('pointerenter', () => {
        if (state.view === v.id) return; // hovering the active pill == deselect, nothing to warm
        prefetchCombo(v.id, v.default_hours ? String(v.default_hours) : state.hours);
      });

      const infoBtn = document.createElement('button');
      infoBtn.className = 'pill-info-btn';
      infoBtn.textContent = '?';
      infoBtn.title = 'Show filter criteria';
      infoBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        showPillInfo(v.id);
      });
      btn.addEventListener('click', () => {
        if (state.view === v.id) {
          state.view = '';
          // When deselecting a view, reset match_types to default.
          state.match_types = ['legal'];
          hideFdaPanel();
        } else {
          state.view = v.id;
          // Snap to the view's default time window (e.g. 24h for sparse
          // medical device news) so the first click isn't worst-case.
          if (v.default_hours) {
            state.hours = String(v.default_hours);
            state.date_from = '';
            state.date_to = '';
            document.getElementById('dateFrom').value = '';
            document.getElementById('dateTo').value = '';
            document.querySelectorAll('.time-pill').forEach(p => {
              p.classList.toggle('active', p.dataset.hours === state.hours);
            });
          }
          // Apply the view's default match_types if the current state
          // doesn't match any of its available profiles.
          if (Array.isArray(v.available_match_types) && v.available_match_types.length) {
            const currentSet = new Set(state.match_types);
            const matchesAny = v.available_match_types.some(p =>
              p.match_types.length === currentSet.size &&
              p.match_types.every(t => currentSet.has(t))
            );
            if (!matchesAny && Array.isArray(v.default_match_types)) {
              state.match_types = [...v.default_match_types];
            }
          }
        }
        state.page = 1;
        document.querySelectorAll('.view-pill').forEach(p => {
          p.classList.toggle('active', p.dataset.viewId === state.view);
        });
        renderMatchProfile();
        updateUrl();
        window.scrollTo({ top: 0 }); // make the transition visible
        fetchArticles();
      });
      if (v.custom) {
        const badge = document.createElement('span');
        badge.className = 'pill-badge-custom';
        badge.textContent = v.job_status === 'completed' ? 'custom' : v.job_status || 'building...';
        btn.appendChild(badge);

        const delBtn = document.createElement('button');
        delBtn.className = 'pill-delete-btn';
        delBtn.textContent = '\u00d7';
        delBtn.title = 'Delete this pill';
        delBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm(`Delete "${v.name}"? This removes all matched articles.`)) return;
          await fetch(`/api/pills/${v.pill_id}`, {method: 'DELETE'});
          fetchViews();
          if (state.view === v.id) { state.view = ''; fetchArticles(); }
        });
        btn.appendChild(delBtn);
      }
      bar.appendChild(btn);
      bar.appendChild(infoBtn);
      }
    }

    // Add [+ New] button if authenticated
    if (data.authenticated) {
      const newBtn = document.createElement('button');
      newBtn.className = 'new-pill-btn';
      newBtn.textContent = '+ New';
      newBtn.title = 'Create a custom monitoring pill';
      newBtn.addEventListener('click', showNewPillModal);
      bar.appendChild(newBtn);
    }

    // After the pills are in the DOM, render the profile toggle for
    // whichever view is currently active (from URL/localStorage restore).
    renderMatchProfile();

    // Render auth nav links based on login state from /api/views response
    const navAuth = document.getElementById('navAuth');
    if (data.authenticated && data.user) {
      navAuth.innerHTML =
        `<a href="/portal" style="color:var(--text-secondary);text-decoration:none;">My Pills</a>` +
        ` <a href="/account" style="color:var(--text-secondary);text-decoration:none;">Account</a>` +
        (data.user.is_admin ? ` <a href="/admin/users" style="color:var(--text-secondary);text-decoration:none;">Admin</a>` : '') +
        ` <a href="/logout" style="color:var(--text-tertiary);text-decoration:none;">Logout (${data.user.display_name})</a>`;
    } else {
      // Discreet sign-in link — unlocks custom pills ("+ New" in the pill row).
      navAuth.innerHTML =
        `<a href="/login" style="color:var(--text-secondary);text-decoration:none;">Sign in</a>`;
    }
  } catch (err) {
    document.getElementById('viewsBar').innerHTML = '';
  }
}

// --- Pill info modal ---
async function showPillInfo(viewId) {
  try {
    const resp = await fetch(`/api/pill_info/${viewId}`);
    const info = await resp.json();
    let html = `<button class="pill-info-close" onclick="this.closest('.pill-info-modal').remove()">&times;</button>`;
    html += `<h3>${info.name || viewId}</h3>`;
    html += `<p style="color:var(--text-secondary);font-size:0.78rem;">${info.description || ''}</p>`;
    if (info.judge_criteria) {
      html += `<strong>How articles get in:</strong>` +
        `<p style="font-size:0.76rem;color:var(--text-secondary);margin:0.3rem 0;">` +
        `Candidate articles (matched by the keywords below${info.candidate_source ? ', or by ' + esc(info.candidate_source) : ', or by semantic similarity'}) ` +
        `are each read by an AI judge (${esc(info.judge_model || 'LLM')}) and only included if they match this definition` +
        `${info.judge_strict ? ' <em>(strict — borderline matches are rejected)</em>' : ''}:</p>` +
        `<blockquote style="font-size:0.76rem;color:var(--text-secondary);border-left:2px solid var(--accent-brand);margin:0.3rem 0;padding-left:0.6rem;">${esc(info.judge_criteria)}</blockquote>`;
      if (info.neg_criteria) {
        html += `<p style="font-size:0.72rem;color:var(--text-tertiary);margin:0.3rem 0;">Explicitly excluded: ${esc(info.neg_criteria)}</p>`;
      }
    }
    if (info.keywords && info.keywords.length) {
      html += `<strong>Nomination keywords (${info.keywords.length}):</strong><div class="kw-list">`;
      html += info.keywords.map(k => `<span class="kw-tag">${k}</span>`).join('');
      html += `</div>`;
    }
    if (info.gkg_theme_prefixes && info.gkg_theme_prefixes.length) {
      html += `<strong>GKG Theme Prefixes:</strong><div class="kw-list">`;
      html += info.gkg_theme_prefixes.map(t => `<span class="kw-tag">${t}</span>`).join('');
      html += `</div>`;
    }
    if (info.sample_companies) {
      html += `<strong>Top Companies (${info.total_companies} total):</strong><div class="kw-list">`;
      html += info.sample_companies.map(c => `<span class="kw-tag">${c}</span>`).join('');
      html += `</div>`;
    }
    if (info.scan_description) {
      html += `<p style="font-size:0.72rem;color:var(--text-tertiary);margin-top:0.5rem;">Scans article title + description</p>`;
    }
    const modal = document.createElement('div');
    modal.className = 'pill-info-modal';
    modal.innerHTML = `<div class="pill-info-content">${html}</div>`;
    modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
    document.body.appendChild(modal);
  } catch (err) {
    console.error('pill info error:', err);
  }
}

// --- New pill modal ---
function showNewPillModal() {
  const modal = document.createElement('div');
  modal.className = 'pill-modal';
  modal.innerHTML = `
    <div class="pill-modal-content">
      <h3>Create Custom Pill</h3>
      <div class="pill-mode-toggle" style="display:flex;gap:0.4rem;margin-bottom:0.7rem;">
        <button type="button" id="pillModeSem" class="profile-seg active" onclick="setPillMode('semantic')">Describe it</button>
        <button type="button" id="pillModeKw" class="profile-seg" onclick="setPillMode('keyword')">Keywords</button>
      </div>
      <label>Pill name</label>
      <input type="text" id="newPillName" placeholder="e.g. GLP-1 & Obesity Medicine" maxlength="100">

      <div id="pillSemFields">
        <label>Describe the topic</label>
        <textarea id="newPillDesc" maxlength="2000" placeholder="News about GLP-1 drugs, weight-loss medications, and obesity medicine — approvals, trials, insurance coverage, makers like Novo Nordisk and Eli Lilly."></textarea>
        <div class="hint">Articles are matched by meaning, not exact words. 1-3 sentences works best.</div>
        <label>Match strictness</label>
        <select id="newPillStrict">
          <option value="0.48">Broad — more articles, more noise</option>
          <option value="0.55" selected>Balanced</option>
          <option value="0.65">Strict — fewer, highly on-topic</option>
        </select>
        <button type="button" class="btn-cancel" style="margin-top:0.5rem;" onclick="previewPill()">Preview matches</button>
        <div id="pillPreview" style="max-height:180px;overflow-y:auto;font-size:0.78rem;margin-top:0.4rem;"></div>
      </div>

      <div id="pillKwFields" style="display:none;">
        <label>Keywords</label>
        <textarea id="newPillKeywords" placeholder="chip, semiconductor, TSMC, fab, wafer, silicon"></textarea>
        <div class="hint">Comma-separated. Min 2, max 200. Articles matching any keyword will be shown.</div>
        <label style="display:flex;align-items:center;gap:0.4rem;margin-top:0.6rem;text-transform:none;letter-spacing:0;">
          <input type="checkbox" id="newPillScanDesc" checked> Also scan article descriptions (recommended)
        </label>
      </div>

      <div class="pill-modal-actions">
        <button class="btn-cancel" onclick="this.closest('.pill-modal').remove()">Cancel</button>
        <button class="btn-create" onclick="createPill()">Create</button>
      </div>
      <div id="newPillError" style="color:var(--tone-neg);font-size:0.78rem;margin-top:0.5rem;"></div>
    </div>
  `;
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

function setPillMode(mode) {
  window.__pillMode = mode;
  document.getElementById('pillSemFields').style.display = mode === 'semantic' ? '' : 'none';
  document.getElementById('pillKwFields').style.display = mode === 'keyword' ? '' : 'none';
  document.getElementById('pillModeSem').classList.toggle('active', mode === 'semantic');
  document.getElementById('pillModeKw').classList.toggle('active', mode === 'keyword');
}

// Live preview via the existing semantic-search endpoint — shows what the
// description would match BEFORE creating the pill.
async function previewPill() {
  const desc = document.getElementById('newPillDesc').value.trim();
  const box = document.getElementById('pillPreview');
  if (desc.length < 10) { box.textContent = 'Describe the topic first (10+ chars).'; return; }
  box.textContent = 'Searching…';
  try {
    const resp = await fetch(`/api/semantic_search?q=${encodeURIComponent(desc)}&per_page=10&hours=168`);
    const data = await resp.json();
    const arts = data.articles || [];
    if (!arts.length) { box.textContent = 'No matches in the last week — try a broader description.'; return; }
    box.innerHTML = arts.map(a =>
      `<div style="padding:0.25rem 0;border-top:1px solid var(--border);">${esc(a.title || a.url)}</div>`
    ).join('');
  } catch (err) {
    box.textContent = 'Preview failed: ' + err.message;
  }
}

async function createPill() {
  const name = document.getElementById('newPillName').value.trim();
  const errEl = document.getElementById('newPillError');
  if (!name) { errEl.textContent = 'Name required.'; return; }

  const mode = window.__pillMode || 'semantic';
  let body;
  if (mode === 'semantic') {
    const description = document.getElementById('newPillDesc').value.trim();
    if (description.length < 10) { errEl.textContent = 'Description required (10+ chars).'; return; }
    body = {
      name, pill_type: 'semantic', description,
      similarity_threshold: parseFloat(document.getElementById('newPillStrict').value),
    };
  } else {
    const keywords = document.getElementById('newPillKeywords').value.trim();
    if (!keywords) { errEl.textContent = 'Keywords required.'; return; }
    body = { name, keywords, scan_description: document.getElementById('newPillScanDesc').checked };
  }

  try {
    const resp = await fetch('/api/pills', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) { errEl.textContent = data.error || 'Failed'; return; }
    document.querySelector('.pill-modal').remove();
    fetchViews();
  } catch (err) {
    errEl.textContent = 'Network error: ' + err.message;
  }
}

// --- Clock widget ---
(function initClock() {
  const tzSel = document.getElementById('clockTz');
  const clockEl = document.getElementById('clockTime');
  const saved = localStorage.getItem('dashClockTz');
  if (saved) tzSel.value = saved;
  function tick() {
    const tz = tzSel.value;
    const now = new Date();
    clockEl.textContent = now.toLocaleString('en-US', {
      timeZone: tz,
      weekday: 'short',
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hour12: false,
    });
  }
  tick();
  setInterval(tick, 1000);
  tzSel.addEventListener('change', () => {
    localStorage.setItem('dashClockTz', tzSel.value);
    tick();
  });
})();

// Source tabs removed — unified feed

// Init — paint the last snapshot instantly (if any), then fetch fresh data.
// When the server already rendered the feed (data-ssr, issue #6), keep that —
// it comes from the 15-min feed cache, always fresher than a <=6h-old local
// snapshot. fetchArticles sees the matching data-snap-key and treats it as
// current content: no skeletons, replaced in place when live data lands.
if (document.getElementById('articleList').dataset.ssr !== '1') {
  restoreSnapshot();
}
fetchViews().catch(e => console.error("fetchViews:", e));
fetchStats();
fetchGalFacets();
fetchArticles().catch(e => console.error("fetchArticles:", e));

// Idle prefetch: once the page has settled, quietly warm all curated pills'
// default combos so a first-of-session pill click instant-paints from a
// snapshot instead of hitting skeletons. Polls for fetchViews() to have
// populated viewsById (it's async and may not have resolved yet).
(function idlePrefetchPills() {
  const startedAt = performance.now();
  function tryStart() {
    const ids = Object.keys(viewsById);
    if (!ids.length) {
      if (performance.now() - startedAt > 10000) return; // give up after 10s
      setTimeout(tryStart, 500);
      return;
    }
    let i = 0;
    (function next() {
      if (i >= ids.length) return;
      const v = viewsById[ids[i++]];
      if (v && v.id !== state.view) {
        prefetchCombo(v.id, v.default_hours ? String(v.default_hours) : state.hours);
      }
      setTimeout(next, 800);
    })();
  }
  setTimeout(tryStart, 3000);
})();

// When the app/tab is reopened, refresh in the background (content stays visible).
let _lastRefresh = Date.now();
addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  if (Date.now() - _lastRefresh < 20000) return; // debounce rapid focus changes
  _lastRefresh = Date.now();
  fetchStats();
  if (!currentFetchController && state.page === 1) fetchArticles();
});

// Auto-refresh. Only refresh stats unconditionally; only auto-refresh
// articles when (a) tab is visible, (b) no fetch is in flight, (c) we're on
// page 1, and (d) no view pill is active — active views are a deliberate
// user choice and yanking them mid-read produces the "loading forever" feel.
setInterval(() => {
  if (document.hidden) return;
  fetchStats();
  if (!currentFetchController && state.page === 1 && !state.view && !state.q) {
    fetchArticles();
  }
}, 60000);

// Auto-refresh briefing every 15 min (matches cache TTL)
setInterval(() => {
  if (document.hidden) return;
  if (state.q) return;  // briefing is hidden during search; don't refetch
  _briefingView = null;  // force re-fetch
  fetchBriefing();
}, 900000);
function toggleDarkMode() {
  const dark = document.body.classList.toggle('dark');
  localStorage.setItem('gdelt-dark', dark ? '1' : '0');
  document.getElementById('themeBtn').textContent = dark ? 'dark' : 'light';
}
