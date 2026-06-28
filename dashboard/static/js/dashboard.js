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
  sort: 'newest', // 'newest' | 'oldest'
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
if (_urlParams.get('en_only') === '0') {
  state.en_only = false;
}

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
  fetchArticles();
});

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

// Fast, styled hover tooltip for view pills (shows the view's description)
const _pillTip = document.createElement('div');
_pillTip.className = 'pill-tip';
document.body.appendChild(_pillTip);
document.addEventListener('mouseover', (e) => {
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
    state[field] = e.target.value;
    state.page = 1;
    updateMoreFiltersDot();
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(fetchArticles, 300);
  });
});
document.getElementById('languageSelect').addEventListener('change', (e) => {
  state.language = e.target.value;
  state.page = 1;
  fetchArticles();
});
document.getElementById('sortSelect').addEventListener('change', (e) => {
  state.sort = e.target.value;
  state.page = 1;
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
        ${matchBadge ? `<div class="match-line">${matchBadge}</div>` : ''}
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
function _snapKey() {
  const hasFilters = state.q || state.person || state.org || state.location ||
    state.theme || state.domain || state.outlet || state.date_from || state.date_to || state.page > 1;
  if (hasFilters) return null;
  return `snap:${state.view}|${state.hours}|${state.en_only ? 1 : 0}`;
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
    document.getElementById('articleList').innerHTML = s.feed;
    if (s.briefingShown && s.briefing) {
      document.getElementById('briefingPanel').style.display = '';
      document.getElementById('briefingText').innerHTML = s.briefing;
      document.getElementById('briefingMeta').textContent = s.briefingMeta || '';
    }
    return true;
  } catch (e) { return false; }
}

async function fetchArticles() {
  renderActiveFilters();
  if (typeof fetchBriefing === "function") fetchBriefing();

  const params = new URLSearchParams();
  if (state.hours) params.set('hours', state.hours);
  if (state.view) params.set('view', state.view);
  if (state.match_types && state.match_types.length) {
    params.set('match_types', state.match_types.join(','));
  }
  if (state.q) params.set('q', state.q);
  if (state.title) params.set('title', state.title);
  if (state.description) params.set('description', state.description);
  if (state.person) params.set('person', state.person);
  if (state.org) params.set('org', state.org);
  if (state.location) params.set('location', state.location);
  if (state.theme) params.set('theme', state.theme);
  if (state.domain) params.set('domain', state.domain);
  if (state.outlet) params.set('outlet', state.outlet);
  if (state.language) params.set('language', state.language);
  params.set('en_only', state.en_only ? '1' : '0');
  if (state.date_from) params.set('date_from', state.date_from);
  if (state.date_to) params.set('date_to', state.date_to);
  if (state.sort && state.sort !== 'newest') params.set('sort', state.sort);
  params.set('page', state.page);
  params.set('per_page', 50);

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

  const list = document.getElementById('articleList');
  const fetchStart = performance.now();
  // Keep existing content visible while refreshing (restored snapshot, previous
  // render, or auto-refresh) — only show the full spinner on a truly empty list.
  const hasContent = !!list.querySelector('li.article');
  if (!hasContent) {
    list.innerHTML = '<li class="loading"><span class="spinner"></span>Loading articles…<span class="loading-elapsed" id="loadElapsed">0.0s elapsed</span></li>';
  }
  document.body.classList.add('fetching');
  document.getElementById('topProgress').classList.add('active');
  const elapsedEl = document.getElementById('loadElapsed');
  currentFetchTimer = setInterval(() => {
    if (myGen !== currentFetchGen) { clearInterval(currentFetchTimer); return; }
    if (!elapsedEl || !elapsedEl.isConnected) { clearInterval(currentFetchTimer); return; }
    const s = ((performance.now() - fetchStart) / 1000).toFixed(1);
    elapsedEl.textContent = `${s}s elapsed${s > 5 ? ' — still working' : ''}`;
  }, 100);
  const finishLoading = () => {
    if (myGen !== currentFetchGen) return; // superseded, leave UI alone
    if (currentFetchTimer) { clearInterval(currentFetchTimer); currentFetchTimer = null; }
    clearTimeout(hardTimeout);
    document.body.classList.remove('fetching');
    document.getElementById('topProgress').classList.remove('active');
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
    (data.views || []).forEach(v => {
      viewsById[v.id] = v;
      const btn = document.createElement('button');
      btn.className = 'view-pill' + (state.view === v.id ? ' active' : '');
      // no-op; segmented control is rendered after the loop
      btn.textContent = v.name;
      if (v.description) btn.dataset.tip = v.description;
      btn.dataset.viewId = v.id;

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
    });

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
      navAuth.innerHTML = '';  // login/register hidden temporarily
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
    if (info.keywords && info.keywords.length) {
      html += `<strong>Keywords (${info.keywords.length}):</strong><div class="kw-list">`;
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
      <label>Pill name</label>
      <input type="text" id="newPillName" placeholder="e.g. Semiconductor Supply" maxlength="100">
      <label>Keywords</label>
      <textarea id="newPillKeywords" placeholder="chip, semiconductor, TSMC, fab, wafer, silicon"></textarea>
      <div class="hint">Comma-separated. Min 2, max 200. Articles matching any keyword will be shown.</div>
      <label style="display:flex;align-items:center;gap:0.4rem;margin-top:0.6rem;text-transform:none;letter-spacing:0;">
        <input type="checkbox" id="newPillScanDesc" checked> Also scan article descriptions (recommended)
      </label>
      <div class="pill-modal-actions">
        <button class="btn-cancel" onclick="this.closest('.pill-modal').remove()">Cancel</button>
        <button class="btn-create" onclick="createPill()">Create</button>
      </div>
      <div id="newPillError" style="color:#a33;font-size:0.78rem;margin-top:0.5rem;"></div>
    </div>
  `;
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });
  document.body.appendChild(modal);
}

async function createPill() {
  const name = document.getElementById('newPillName').value.trim();
  const keywords = document.getElementById('newPillKeywords').value.trim();
  const scanDesc = document.getElementById('newPillScanDesc').checked;
  const errEl = document.getElementById('newPillError');
  if (!name) { errEl.textContent = 'Name required.'; return; }
  if (!keywords) { errEl.textContent = 'Keywords required.'; return; }

  try {
    const resp = await fetch('/api/pills', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, keywords, scan_description: scanDesc }),
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
restoreSnapshot();
fetchViews().catch(e => console.error("fetchViews:", e));
fetchStats();
fetchGalFacets();
fetchArticles().catch(e => console.error("fetchArticles:", e));

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
  if (!currentFetchController && state.page === 1 && !state.view) {
    fetchArticles();
  }
}, 60000);

// Auto-refresh briefing every 15 min (matches cache TTL)
setInterval(() => {
  if (document.hidden) return;
  _briefingView = null;  // force re-fetch
  fetchBriefing();
}, 900000);
function toggleDarkMode() {
  const dark = document.body.classList.toggle('dark');
  localStorage.setItem('gdelt-dark', dark ? '1' : '0');
  document.getElementById('themeBtn').textContent = dark ? 'dark' : 'light';
}
