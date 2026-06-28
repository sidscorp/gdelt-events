let currentPage = 1;
let lastQuery = null;

function setQuery(el) {
  document.getElementById('q').value = el.textContent;
  doSearch();
}

function scoreBadgeClass(s) {
  if (s >= 0.65) return 'high';
  if (s >= 0.55) return 'mid';
  return 'low';
}

function escHtml(s) {
  if (s === null || s === undefined) return '';
  return String(s).replace(/[&<>"]/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'
  }[c]));
}

async function doSearch(page) {
  const q = document.getElementById('q').value.trim();
  if (!q) return;

  if (!page) page = 1;
  currentPage = page;
  lastQuery = q;

  const hours = document.getElementById('hours').value;
  const domain = document.getElementById('domain').value.trim();
  const perPage = document.getElementById('per_page').value;

  const params = new URLSearchParams({ q, page, per_page: perPage });
  if (hours) params.set('hours', hours);
  if (domain) params.set('domain', domain);
  params.set('language', 'en');

  const btn = document.getElementById('searchBtn');
  btn.disabled = true; btn.textContent = 'Searching...';
  document.getElementById('errorBox').style.display = 'none';
  document.getElementById('results').innerHTML = '<div class="loading">Searching ' + escHtml(q) + '...</div>';
  document.getElementById('pagination').style.display = 'none';
  document.getElementById('metaBar').style.display = 'none';

  try {
    const resp = await fetch('/api/semantic_search?' + params);
    const data = await resp.json();
    btn.disabled = false; btn.textContent = 'Search';

    if (!resp.ok) {
      document.getElementById('errorBox').textContent = data.error || 'Search failed';
      document.getElementById('errorBox').style.display = 'block';
      document.getElementById('results').innerHTML = '';
      return;
    }

    renderResults(data);
  } catch (err) {
    btn.disabled = false; btn.textContent = 'Search';
    document.getElementById('errorBox').textContent = 'Network error: ' + err.message;
    document.getElementById('errorBox').style.display = 'block';
    document.getElementById('results').innerHTML = '';
  }
}

function renderResults(data) {
  const meta = document.getElementById('metaBar');
  const t = data.timing_ms || {};
  meta.innerHTML =
    `${data.total.toLocaleString()} results · page ${data.page} of ${data.pages}` +
    `<span>FAISS ${t.faiss}ms · DB ${t.db}ms</span>`;
  meta.style.display = 'flex';

  const results = document.getElementById('results');
  if (data.articles.length === 0) {
    results.innerHTML = '<div class="empty">No matches found in this time range. Try expanding the time range or rephrasing your query.</div>';
    document.getElementById('pagination').style.display = 'none';
    return;
  }

  results.innerHTML = data.articles.map(a => {
    const s = a.score;
    const cls = scoreBadgeClass(s);
    return `
      <div class="article">
        <div class="article-header">
          <div class="article-title"><a href="${escHtml(a.url)}" target="_blank" rel="noopener">${escHtml(a.title || '(untitled)')}</a></div>
          <span class="score-badge ${cls}">${s.toFixed(3)}</span>
        </div>
        <div class="article-meta">
          <span class="domain">${escHtml(a.outlet_name || a.domain || '?')}</span>
          ${a.time_ago ? ' · ' + escHtml(a.time_ago) : ''}
        </div>
        ${a.description ? `<div class="article-desc">${escHtml(a.description.substring(0, 300))}${a.description.length > 300 ? '...' : ''}</div>` : ''}
      </div>
    `;
  }).join('');

  renderPagination(data);
}

function renderPagination(data) {
  const pg = document.getElementById('pagination');
  if (data.pages <= 1) { pg.style.display = 'none'; return; }
  pg.style.display = 'flex';

  const buttons = [];
  buttons.push(`<button onclick="doSearch(1)" ${data.page === 1 ? 'disabled' : ''}>« First</button>`);
  buttons.push(`<button onclick="doSearch(${data.page - 1})" ${data.page === 1 ? 'disabled' : ''}>‹ Prev</button>`);
  buttons.push(`<span class="page-info">Page ${data.page} of ${data.pages}</span>`);
  buttons.push(`<button onclick="doSearch(${data.page + 1})" ${data.page === data.pages ? 'disabled' : ''}>Next ›</button>`);
  buttons.push(`<button onclick="doSearch(${data.pages})" ${data.page === data.pages ? 'disabled' : ''}>Last »</button>`);
  pg.innerHTML = buttons.join('');
}

// Submit on Enter (Cmd/Ctrl+Enter or just Enter without shift)
document.getElementById('q').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    doSearch();
  }
});
