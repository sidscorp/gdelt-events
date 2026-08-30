// ---------------------------------------------------------------------------
// Briefing markdown renderer
//
// Briefing text reaches this file three ways — streamed raw from the model,
// replayed from briefing_cache (already run through briefing._normalize_briefing),
// or re-hydrated from a localStorage snapshot. They must all render identically,
// so every quirk is smoothed out here rather than upstream.
// ---------------------------------------------------------------------------

// The model is inconsistent run to run: some briefings cite [3], others 【3】
// (fullwidth brackets fall back to a CJK font, so they show as unlinked glyphs
// with a wide gap before the sentence period). It also sprinkles U+202F narrow
// no-break spaces and U+2011 non-breaking hyphens, which do the same thing
// mid-word. Fold all of it back to plain ASCII before parsing.
function normalizeBriefingText(text) {
  return String(text == null ? '' : text)
    // \u3010N\u3011 \u3014N\u3015 \uff3bN\uff3d [[N]] [N,M]  ->  [N] / [N][M]
    .replace(/[\u3010\u3014\uff3b\[]{1,2}\s*(\d+(?:\s*[,\uff0c\u3001]\s*\d+)*)\s*[\u3011\u3015\uff3d\]]{1,2}/g,
      (_m, nums) => nums.split(/[,\uff0c\u3001]/).map((n) => '[' + n.trim() + ']').join(''))
    .replace(/[\u00a0\u2009\u202f]/g, ' ')   // nbsp / thin space / narrow no-break space
    .replace(/\u2011/g, '-')                   // non-breaking hyphen
    // Citation placement drifts too: one briefing writes "...this week[3].",
    // the next "...this week. [3]". Pull a line-final period back inside and
    // drop any space before a marker so citations always hug their clause.
    .replace(/([.!?])\s*((?:\[\d+\])+)\s*$/gm, '$2$1')
    .replace(/[ \t]+((?:\[\d+\])+)/g, '$1');
}

// While the briefing streams, its tail is always mid-token: "**at least nine"
// has no closing "**" yet. Rendering that verbatim paints raw asterisks that
// snap to bold a beat later — the flicker you see on every load. Drop the
// dangling opener instead so the words keep flowing and the syntax never shows.
function trimOpenMarkup(text) {
  let s = text;
  // Two passes: a closing "**" arrives one character at a time, so the frame
  // where only its first "*" has landed leaves a lone "*" behind once the
  // unmatched opener is removed.
  for (let pass = 0; pass < 2; pass++) {
    // Scan for runs of '*', ignoring any that are a list marker ("* item").
    const runs = [];
    for (let i = 0; i < s.length; i++) {
      if (s[i] !== '*') continue;
      let j = i;
      while (s[j] === '*') j++;
      const len = j - i;
      const atLineStart = i === 0 || s[i - 1] === '\n';
      if (!(len === 1 && atLineStart && s[j] === ' ')) runs.push({ i: i, len: len });
      i = j - 1;
    }
    let bold = 0, ital = 0, lastBold = -1, lastItal = -1;
    for (const r of runs) {
      if (r.len >= 2) { bold++; lastBold = r.i; } else { ital++; lastItal = r.i; }
    }
    if (bold % 2 === 1) s = s.slice(0, lastBold) + s.slice(lastBold + 2);
    else if (ital % 2 === 1) s = s.slice(0, lastItal) + s.slice(lastItal + 1);
    else break;
  }
  // A half-arrived citation marker or heading hash, likewise.
  return s.replace(/[\[\u3010\u3014\uff3b][\d,\s]*$/, '').replace(/(^|\n)#{1,4}\s*$/, '$1');
}

function _mdEscape(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _mdInline(s) {
  return _mdEscape(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
}

// Block-level render. Emits plain semantic tags with no inline styles — the
// briefing's look lives entirely in .briefing-body CSS. (The old renderer
// inline-styled its <ul> with list-style:disc, which beat the stylesheet's
// list-style:none and drew a second bullet next to the ▸ marker.)
function renderMd(text, opts) {
  let src = normalizeBriefingText(text);
  if (opts && opts.streaming) src = trimOpenMarkup(src);

  const out = [];
  let list = null;
  let para = [];

  const flushPara = () => {
    if (para.length) { out.push('<p>' + _mdInline(para.join(' ')) + '</p>'); para = []; }
  };
  const flushList = () => {
    if (list) { out.push('<ul>' + list.join('') + '</ul>'); list = null; }
  };

  for (const raw of src.split('\n')) {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); continue; }

    const li = line.match(/^(?:[-*\u2022\u2023\u25aa\u2013]|\d+[.)])\s+(.*)$/);
    if (li) {
      flushPara();
      (list || (list = [])).push('<li>' + _mdInline(li[1]) + '</li>');
      continue;
    }

    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      out.push('<h4>' + _mdInline(h[2]) + '</h4>');
      continue;
    }

    // Prose. Consecutive non-blank lines are one paragraph, so a hard-wrapped
    // paragraph doesn't shatter into a stack of one-line <p>s.
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return out.join('');
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

// Live freshness bar ticker
let _freshnessTimer = null;
let _freshnessData = null;

function stopFreshnessTicker() {
  if (_freshnessTimer) { clearInterval(_freshnessTimer); _freshnessTimer = null; }
  _freshnessData = null;
}

function updateFreshnessBar() {
  if (!_freshnessData) return;
  const el = document.getElementById('briefingFreshness');
  if (!el) return;
  const { generatedAt, ttlS, articleCount } = _freshnessData;
  const now = Date.now();
  const gen = new Date(generatedAt + 'Z');
  if (isNaN(gen.getTime())) { el.innerHTML = ''; return; }
  const elapsed = Math.max(0, Math.floor((now - gen.getTime()) / 1000));
  const remaining = Math.max(0, ttlS - elapsed);

  const elapsedLabel = elapsed < 60 ? `${elapsed}s` : elapsed < 3600 ? `${Math.floor(elapsed / 60)}m ago` : `${Math.floor(elapsed / 3600)}h ago`;
  const remainLabel = remaining <= 0 ? 'any moment' : remaining < 60 ? `~${remaining}s` : remaining < 3600 ? `~${Math.floor(remaining / 60)}m` : `~${Math.floor(remaining / 3600)}h`;

  const ratio = ttlS > 0 ? Math.min(1, elapsed / ttlS) : 0;
  let cls = '';
  if (ratio > 0.9) cls = 'bf-stale';
  else if (ratio > 0.5) cls = 'bf-aging';

  el.className = 'briefing-freshness ' + cls;
  el.innerHTML = '<span class="bf-elapsed">Generated ' + elapsedLabel + '</span>'
    + (ttlS > 0 ? ' <span class="bf-sep">·</span> <span class="bf-next">Next update in ' + remainLabel + '</span>' : '')
    + (articleCount ? ' <span class="bf-sep">·</span> <span class="bf-count">From ' + articleCount + ' articles</span>' : '');
}

function startFreshnessTicker(generatedAt, ttlS, articleCount) {
  if (!generatedAt) return;
  stopFreshnessTicker();
  _freshnessData = { generatedAt, ttlS: ttlS || 0, articleCount: articleCount || 0 };
  updateFreshnessBar();
  _freshnessTimer = setInterval(updateFreshnessBar, 15000);
}

// Dismiss collapses the panel into a small restore pill instead of hiding it
// outright, so there's always a way back without switching views or waiting
// on the 15-min auto-refresh.
function dismissBriefing() {
  const p = document.getElementById('briefingPanel');
  const r = document.getElementById('briefingRestore');
  const f = document.getElementById('briefingFreshness');
  if (p) p.style.display = 'none';
  if (r) r.style.display = '';
  if (f) f.innerHTML = '';
  stopFreshnessTicker();
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
  stopFreshnessTicker();
  const frEl = document.getElementById('briefingFreshness');
  if (frEl) frEl.innerHTML = '';

  // Abort any in-flight briefing
  if (_briefingAbort) { _briefingAbort.abort(); _briefingAbort = null; }

// --- "we are writing your briefing" state -----------------------------------
// Timings are measured, not guessed (perf_samples, 2026-08-29): a CACHED briefing
// lands at p50 0.2s / p90 0.8s, a LIVE generation shows its first text at p50 0.34s
// and finishes at p50 2.9s / p90 10.8s.
//
// Two consequences, both deliberate:
//   * The panel is delayed by SHOW_AFTER_MS. 90% of cached briefings arrive inside
//     0.8s, so showing it immediately would flash a "generating" message on almost
//     every warm page load, which reads as slowness rather than transparency.
//   * It stays up until `done`, not until the first token. First text lands at
//     ~0.34s; a note that disappears that fast is a note nobody reads, and the
//     wait people actually feel is the ~3s to a complete briefing.
const BRIEF_PROGRESS_SHOW_AFTER_MS = 700;
let _briefProgressTick = null;
let _briefProgressDelay = null;

function _briefProgressHost() {
  let el = document.getElementById('briefingProgress');
  if (!el) {
    el = document.createElement('div');
    el.id = 'briefingProgress';
    el.className = 'brief-progress';
    el.setAttribute('role', 'status');      // announced to screen readers…
    el.setAttribute('aria-live', 'polite');  // …without interrupting them
    const meta = document.getElementById('briefingMeta');
    meta.parentNode.insertBefore(el, meta.nextSibling);
  }
  return el;
}

function showBriefProgress(coldStart) {
  hideBriefProgress();
  const t0 = performance.now();
  _briefProgressDelay = setTimeout(() => {
    const host = _briefProgressHost();
    // The panel is the single status voice once it appears. Without this the
    // meta line's "Generating briefing…" sits directly above the panel's
    // "Writing this briefing now · 3s" and the user reads the same fact twice.
    const meta = document.getElementById('briefingMeta');
    if (meta) meta.textContent = '';
    const paint = () => {
      const secs = Math.round((performance.now() - t0) / 1000);
      host.innerHTML =
        '<div class="brief-progress-row">' +
          '<span class="brief-spinner" aria-hidden="true"></span>' +
          '<span class="brief-progress-label">' +
            (coldStart ? 'Writing this briefing now' : 'Updating this briefing') +
            (secs >= 1 ? ' · ' + secs + 's' : '') +
          '</span>' +
        '</div>' +
        '<div class="brief-progress-note">Usually ready in a few seconds. ' +
          'Only the most-read views are written ahead of time — this one is generated ' +
          'on demand, which keeps the running costs of the site down.' +
        '</div>';
    };
    paint();
    host.style.display = '';
    _briefProgressTick = setInterval(paint, 1000);
  }, BRIEF_PROGRESS_SHOW_AFTER_MS);
}

function hideBriefProgress() {
  if (_briefProgressDelay) { clearTimeout(_briefProgressDelay); _briefProgressDelay = null; }
  if (_briefProgressTick) { clearInterval(_briefProgressTick); _briefProgressTick = null; }
  const el = document.getElementById('briefingProgress');
  if (el) { el.style.display = 'none'; el.innerHTML = ''; }
}

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
  showBriefProgress(!keepText);
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
          hideBriefProgress();
          if (data.found !== undefined) {
            textEl.innerHTML = '<span style="color:var(--text-tertiary); font-style:italic;">Not enough recent articles for a briefing — try a wider time range.</span>';
            metaEl.textContent = '';
          } else if (!textEl.textContent.trim()) {
            panel.style.display = 'none';
          } else {
            metaEl.textContent = 'Using cached briefing';
          }
          return true;
        }
        // /api/briefing streams in two phases: the cached briefing in full
        // (done:false), then — if that cache was stale — a freshly generated
        // one on the same connection. The server re-sends `sources` to open
        // phase two, which is the only signal that what follows REPLACES what
        // we have rather than continuing it. Without this reset the reader
        // sees the cached briefing with the new one appended to it.
        if (data.sources) {
          if (fullText) fullText = '';
          sourcesMap = data.sources;
        }
        if (data.text) {
          if (!briefFirstMarked && window.perfMark) { briefFirstMarked = true; window.perfMark('briefing_first', performance.now() - briefStart); }
          fullText += data.text;
          // streaming:true while tokens are still arriving — the trailing
          // half-written **bold** is hidden rather than shown as raw asterisks.
          textEl.innerHTML = linkifyCitations(
            renderMd(fullText, { streaming: !data.done }), sourcesMap);
          textEl.dataset.key = wantKey; // this content now belongs to this view/window
        }
        if (data.article_count) articleCount = data.article_count;
        if (data.done) {
          hideBriefProgress();
          // Final repaint with markup complete, so nothing stays trimmed.
          if (fullText) textEl.innerHTML = linkifyCitations(renderMd(fullText), sourcesMap);
          // 'refreshed' means stale cached text was just replaced in place.
          let label;
          if (data.meta) label = data.meta;
          else if (data.refreshed) label = 'just refreshed';
          else if (data.cached) label = 'cached';
          else label = 'just generated';
          metaEl.textContent = articleCount ? `From ${articleCount} articles · ${label}` : '';
          if (window.perfMark) { window.perfMark(data.cached ? 'briefing_done_cached' : 'briefing_done', performance.now() - briefStart); window.perfFlush(); }
          if (typeof saveSnapshot === 'function') saveSnapshot();
          // Start the live freshness countdown: generated_at in UTC, TTL from server
          if (data.generated_at || data.cache_ttl_s) {
            startFreshnessTicker(data.generated_at, data.cache_ttl_s, data.article_count);
          }
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
    hideBriefProgress();
    if (!fullText.trim()) {
      textEl.innerHTML = '<span style="color:var(--text-tertiary); font-style:italic;">Briefing unavailable — try a wider time range.</span>';
      metaEl.textContent = '';
    }
  } catch (err) {
    // Includes the abort path: switching view mid-generation must not leave a
    // stale "writing…" ticker running under the new view's briefing.
    hideBriefProgress();
    if (err.name !== 'AbortError' && !textEl.textContent.trim()) panel.style.display = 'none';
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
  // display:'' alone does NOT show the panel - the stylesheet keeps the id at
  // display:none, so clearing the inline style falls back to hidden.
  panel.style.display = 'block';
  _fdaEventsLoaded = true;

  // Convert hours to days for the API. Floor at 30: FDA regenerates these feeds
  // weekly (510(k)s monthly), so a raw 24h feed window almost always reads
  // "No FDA regulatory actions" — a true-but-misleading empty state.
  const days = Math.max(30, Math.min(90, Math.ceil((hours || 168) / 24)));
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
      const fullDesc = (e.product_description || '').trim();
      const fullReason = (e.reason_for_recall || '').trim();
      const descFull = (fullDesc + (fullReason && fullReason !== fullDesc ? ' — Reason: ' + fullReason : '')).trim();
      const descShort = descFull.substring(0, 120);
      const classLabel = e.recall_class ? ` ${e.recall_class.replace(/^Class\s*/i, 'Class ')}` : '';
      // 510(k) clearances have a canonical FDA page keyed by the K-number;
      // enforcement/recall records have no stable public URL - those rows
      // expand for the full text and the firm name filters the news instead.
      const srcUrl = e.event_type === '510k'
        ? `https://www.accessdata.fda.gov/scripts/cdrh/cfdocs/cfpmn/pmn.cfm?ID=${encodeURIComponent(e.event_id)}` : null;
      return `<div class="fda-event-row" onclick="this.classList.toggle('open')" title="Click to expand">
        <span class="fda-event-badge ${_fdaBadgeClass(e.event_type)}">${_fdaBadgeLabel(e.event_type)}${classLabel}</span>
        <span class="fda-event-firm" title="Filter the news to this company" onclick="event.stopPropagation();applyFilter('org','${firm.replace(/'/g,"\\'")}')">${firm}</span>
        <span class="fda-event-desc"><span class="d-short">${descShort}</span><span class="d-full">${descFull}</span>${srcUrl ? ` <a class="fda-src" href="${srcUrl}" target="_blank" rel="noopener" onclick="event.stopPropagation()">FDA&nbsp;page&nbsp;↗</a>` : ''}</span>
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

