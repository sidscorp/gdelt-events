// Minimal markdown renderer
function renderMd(text) {
  // First pass: convert markdown to HTML tokens
  let html = text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^### (.+)$/gm, '<h4 style="font-size:0.85rem;margin:0.5rem 0 0.15rem;font-weight:600;">$1</h4>')
    .replace(/^## (.+)$/gm, '<h4 style="font-size:0.88rem;margin:0.6rem 0 0.15rem;font-weight:600;">$1</h4>')
    .replace(/^[•\-\*] (.+)$/gm, '<<LI>>$1<</LI>>');
  // Wrap consecutive LI tokens in a tight UL
  html = html.replace(/(<<LI>>.*?<<\/LI>>\n?)+/g, (match) => {
    const items = match.replace(/<<LI>>/g, '<li>').replace(/<<\/LI>>/g, '</li>');
    return '<ul style="list-style:disc;padding-left:1.2rem;margin:0.2rem 0;">' + items + '</ul>';
  });
  // Clean up newlines — but not inside lists
  html = html.replace(/\n\n/g, '<br>').replace(/\n/g, ' ');
  return html;
}

// Turn [N] citation markers into clickable superscripts linking to the source's
// event page (or article). The model only emits indices, so links can't be
// hallucinated; out-of-range markers are left as plain text.
function linkifyCitations(html, sources) {
  if (!sources || !sources.length) return html;
  return html.replace(/\[(\d+)\]/g, (m, num) => {
    const s = sources[parseInt(num, 10) - 1];
    if (!s || !s.link) return m;
    const cnt = (s.n_sources && s.n_sources > 1) ? ` · ${s.n_sources} sources` : '';
    const tip = ((s.outlet || 'source') + cnt + (s.title ? ' — ' + s.title : ''))
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
    return `<sup class="cite"><a href="${s.link}" target="_blank" rel="noopener" title="${tip}">${num}</a></sup>`;
  });
}

// AI Briefing — streams a summary for the current view/time via SSE
let _briefingView = null;
let _briefingHours = null;
let _briefingAbort = null;

// Dismiss collapses the panel into a small restore pill instead of hiding it
// outright, so there's always a way back without switching views or waiting
// on the 15-min auto-refresh.
function dismissBriefing() {
  const p = document.getElementById('briefingPanel');
  const r = document.getElementById('briefingRestore');
  if (p) p.style.display = 'none';
  if (r) r.style.display = '';
}
function restoreBriefing() {
  const p = document.getElementById('briefingPanel');
  const r = document.getElementById('briefingRestore');
  if (p) p.style.display = '';
  if (r) r.style.display = 'none';
}

async function fetchBriefing() {
  const view = (typeof state !== 'undefined' && state.view) || '';
  const hours = (typeof state !== 'undefined' && state.hours) || 24;
  const q = (typeof state !== 'undefined' && state.q) ? String(state.q).trim() : '';

  // The briefing is a feed-level summary; it is not meaningful while a search
  // query is active. Hide the panel and force regeneration once search clears.
  if (q) {
    const p = document.getElementById('briefingPanel');
    const r = document.getElementById('briefingRestore');
    if (p) p.style.display = 'none';
    if (r) r.style.display = 'none';
    _briefingView = null;
    return;
  }

  if (_briefingView !== null && view === _briefingView && hours === _briefingHours) return;
  _briefingView = view;
  _briefingHours = hours;

  // Abort any in-flight briefing
  if (_briefingAbort) { _briefingAbort.abort(); _briefingAbort = null; }

  const panel = document.getElementById('briefingPanel');
  const textEl = document.getElementById('briefingText');
  const metaEl = document.getElementById('briefingMeta');

  // Keep existing text visible ONLY if it belongs to this view/window (boot
  // restore and snapshot paints stamp textEl.dataset.key). Otherwise show a
  // shimmer skeleton — never another view's briefing posing as current.
  const wantKey = `${view}|${hours}`;
  const keepText = textEl.dataset.key === wantKey && textEl.textContent.trim().length > 0;
  if (!keepText) {
    textEl.innerHTML =
      '<div class="brief-skel" aria-hidden="true">' +
        '<div class="skel-block skel-line w45"></div>' +
        '<div class="skel-block skel-line thin w95"></div>' +
        '<div class="skel-block skel-line thin w88"></div>' +
        '<div class="skel-block skel-line thin w60"></div>' +
      '</div>';
  }
  metaEl.textContent = keepText ? 'Updating…' : 'Generating briefing…';
  panel.style.display = '';
  const restoreEl = document.getElementById('briefingRestore');
  if (restoreEl) restoreEl.style.display = 'none';
  const briefStart = performance.now();
  let briefFirstMarked = false;

  const params = new URLSearchParams();
  if (view) params.set('view', view);
  params.set('hours', hours);
  params.set('stream', '1');

  const controller = new AbortController();
  _briefingAbort = controller;

  try {
    const resp = await fetch('/api/briefing?' + params, { signal: controller.signal });
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullText = '';
    let articleCount = 0;
    let buffer = '';
    let sourcesMap = [];

    // Process one complete SSE "data:" line. Returns true if the stream
    // signalled that we should stop (error/return).
    const handleLine = (line) => {
      if (!line.startsWith('data: ')) return false;
      try {
        const data = JSON.parse(line.slice(6));
        if (data.error) {
          if (data.found !== undefined) {
            textEl.innerHTML = '<span style="color:var(--text-tertiary); font-style:italic;">Not enough recent articles for a briefing — try a wider time range.</span>';
            metaEl.textContent = '';
          } else {
            panel.style.display = 'none';
          }
          return true;
        }
        if (data.sources) sourcesMap = data.sources;
        if (data.text) {
          if (!briefFirstMarked && window.perfMark) { briefFirstMarked = true; window.perfMark('briefing_first', performance.now() - briefStart); }
          fullText += data.text;
          textEl.innerHTML = linkifyCitations(renderMd(fullText), sourcesMap);
          textEl.dataset.key = wantKey; // this content now belongs to this view/window
        }
        if (data.article_count) articleCount = data.article_count;
        if (data.done) {
          const label = data.cached ? 'cached' : 'just generated';
          metaEl.textContent = articleCount ? `From ${articleCount} articles · ${label}` : '';
          if (window.perfMark) { window.perfMark(data.cached ? 'briefing_done_cached' : 'briefing_done', performance.now() - briefStart); window.perfFlush(); }
          if (typeof saveSnapshot === 'function') saveSnapshot();
        }
      } catch (_) {}
      return false;
    };

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      // Buffer across reads: an SSE event can be split across network chunks,
      // so only parse complete lines and keep the trailing partial for later.
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl);
        buffer = buffer.slice(nl + 1);
        if (handleLine(line)) return;
      }
    }
    // Flush any final line left without a trailing newline.
    if (buffer && handleLine(buffer)) return;
    if (!fullText.trim()) {
      textEl.innerHTML = '<span style="color:var(--text-tertiary); font-style:italic;">Briefing unavailable — try a wider time range.</span>';
      metaEl.textContent = '';
    }
  } catch (err) {
    if (err.name !== 'AbortError') panel.style.display = 'none';
  }
}

// Trigger briefing on initial page load
fetchBriefing();

// ---------------------------------------------------------------------------
// FDA Regulatory Events panel
// ---------------------------------------------------------------------------
let _fdaPanelOpen = true;
let _fdaEventsLoaded = false;

function toggleFdaPanel() {
  _fdaPanelOpen = !_fdaPanelOpen;
  const list = document.getElementById('fdaEventsList');
  const label = document.getElementById('fdaPanelToggleLabel');
  list.style.display = _fdaPanelOpen ? '' : 'none';
  label.innerHTML = _fdaPanelOpen ? '&#9660; collapse' : '&#9654; expand';
}

function _fdaDateFmt(d) {
  if (!d || d.length < 8) return d || '';
  return d.slice(0, 4) + '-' + d.slice(4, 6) + '-' + d.slice(6, 8);
}

function _fdaBadgeClass(type) {
  if (type === 'recall') return 'fda-badge-recall';
  if (type === '510k') return 'fda-badge-510k';
  return 'fda-badge-enforcement';
}

function _fdaBadgeLabel(type) {
  if (type === 'recall') return 'RECALL';
  if (type === '510k') return '510(k)';
  return 'ENFORCE';
}

async function loadFdaEvents(hours) {
  const panel = document.getElementById('fdaEventsPanel');
  const list = document.getElementById('fdaEventsList');
  const countEl = document.getElementById('fdaEventCount');
  if (!panel) return;
  panel.style.display = '';
  _fdaEventsLoaded = true;

  // Convert hours to days for the API
  const days = Math.max(1, Math.min(90, Math.ceil((hours || 168) / 24)));
  try {
    const resp = await fetch(`/api/fda_events?days=${days}`);
    const data = await resp.json();
    if (!data.events || data.events.length === 0) {
      list.innerHTML = '<div style="padding:0.5rem 0.8rem; color:var(--text-tertiary); font-size:0.78rem;">No FDA regulatory actions in this period.</div>';
      countEl.textContent = '';
      return;
    }
    countEl.textContent = `(${data.events.length})`;
    list.innerHTML = data.events.slice(0, 50).map(e => {
      const firm = (e.firm_name || '').substring(0, 40);
      const desc = (e.product_description || e.reason_for_recall || '').substring(0, 120);
      const classLabel = e.recall_class ? ` ${e.recall_class.replace(/^Class\s*/i, 'Class ')}` : '';
      return `<div class="fda-event-row">
        <span class="fda-event-badge ${_fdaBadgeClass(e.event_type)}">${_fdaBadgeLabel(e.event_type)}${classLabel}</span>
        <span class="fda-event-firm" title="${(e.firm_name||'').replace(/"/g,'&quot;')}" onclick="applyFilter('org','${firm.replace(/'/g,"\'")}') ">${firm}</span>
        <span class="fda-event-desc" title="${(desc||'').replace(/"/g,'&quot;')}">${desc}</span>
        <span class="fda-event-date">${_fdaDateFmt(e.event_date)}</span>
      </div>`;
    }).join('');
  } catch (err) {
    list.innerHTML = '<div style="padding:0.5rem 0.8rem; color:var(--text-tertiary); font-size:0.78rem;">Unable to load FDA events.</div>';
  }
}

function hideFdaPanel() {
  const panel = document.getElementById('fdaEventsPanel');
  if (panel) panel.style.display = 'none';
  _fdaEventsLoaded = false;
}

