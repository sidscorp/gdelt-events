let pillType = 'keyword';

function setType(type) {
  pillType = type;
  document.querySelectorAll('.type-toggle button').forEach(b => b.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('keywordFields').style.display = type === 'keyword' ? '' : 'none';
  document.getElementById('semanticFields').style.display = type === 'semantic' ? '' : 'none';
}

function updateThreshold() {
  const val = document.getElementById('pillThreshold').value / 100;
  document.getElementById('thresholdDisplay').textContent = val.toFixed(2);
}

async function createPillFromPortal() {
  const name = document.getElementById('pillName').value.trim();
  const errEl = document.getElementById('createError');
  const successEl = document.getElementById('createSuccess');
  errEl.textContent = ''; successEl.textContent = '';
  if (!name) { errEl.textContent = 'Name required.'; return; }

  let body;
  if (pillType === 'semantic') {
    const description = document.getElementById('pillDescription').value.trim();
    if (!description || description.length < 10) {
      errEl.textContent = 'Description must be at least 10 characters.';
      return;
    }
    const threshold = document.getElementById('pillThreshold').value / 100;
    const scanDays = parseInt(document.getElementById('pillScanDays').value);
    body = { name, pill_type: 'semantic', description, similarity_threshold: threshold, scan_days: scanDays };
  } else {
    const keywords = document.getElementById('pillKeywords').value.trim();
    const scanDesc = document.getElementById('pillScanDesc').checked;
    if (!keywords) { errEl.textContent = 'Keywords required.'; return; }
    body = { name, pill_type: 'keyword', keywords, scan_description: scanDesc };
  }

  try {
    const btn = document.querySelector('.create-form button');
    btn.disabled = true;
    btn.textContent = pillType === 'semantic' ? 'Embedding description...' : 'Creating...';

    const resp = await fetch('/api/pills', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    btn.disabled = false;
    btn.textContent = 'Create Pill';

    if (!resp.ok) { errEl.textContent = data.error || 'Failed'; return; }

    const typeLabel = pillType === 'semantic' ? 'Semantic pill' : 'Pill';
    let timeEstimate = '~10 min';
    if (pillType === 'semantic') {
      const days = parseInt(document.getElementById('pillScanDays').value);
      const estimates = {3: '~15 min', 7: '~2 hours', 14: '~4 hours', 30: '~8 hours', 60: '~20 hours'};
      timeEstimate = estimates[days] || '~2 hours';
    }
    successEl.textContent = `${typeLabel} "${name}" created! Building in background (${timeEstimate}). Refresh to see progress.`;
    document.getElementById('pillName').value = '';
    document.getElementById('pillKeywords').value = '';
    document.getElementById('pillDescription').value = '';
    setTimeout(() => location.reload(), 3000);
  } catch (err) {
    document.querySelector('.create-form button').disabled = false;
    document.querySelector('.create-form button').textContent = 'Create Pill';
    errEl.textContent = 'Network error: ' + err.message;
  }
}

async function deletePill(id, name) {
  if (!confirm('Delete "' + name + '"? This removes all matched articles.')) return;
  await fetch('/api/pills/' + id, { method: 'DELETE' });
  document.getElementById('pill-' + id)?.remove();
}
