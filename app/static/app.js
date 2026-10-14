'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let cropper = null;
let currentCropSource = 'closed';
let areas = [];            // comparison areas being edited in Setup
let selectedArea = -1;
let areasLoaded = false;
let areasDirty = false;
let cropperReady = false;
let cropperGen = 0;         // bumps on every rebuild so stale callbacks bail out
let historyCache = [];
let ws = null;
let feedInterval = null;
let lastHistoryKey = '';
let llmMode = 'off';
let lastSuggestionTs = null;
let chart = null;
let chartThreshold = 0.4;
const scorePoints = [];
let MAX_CHART_POINTS = 60;

// ── Chart ─────────────────────────────────────────────────────────────────────
function initChart() {
  const ctx = document.getElementById('score-chart').getContext('2d');
  Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";

  chart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      spanGaps: true,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          grid: { color: '#2d3148', drawTicks: false },
          border: { color: '#2d3148' },
          ticks: {
            color: '#5c6280',
            maxTicksLimit: 7,
            maxRotation: 0,
            font: { size: 11 },
          },
        },
        y: {
          min: 0, max: 1,
          grid: { color: '#2d3148', drawTicks: false },
          border: { color: '#2d3148' },
          ticks: {
            color: '#5c6280',
            stepSize: 0.2,
            font: { size: 11 },
            callback: v => v.toFixed(1),
          },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#1a1d27',
          borderColor: '#3b4060',
          borderWidth: 1,
          titleColor: '#e8eaf0',
          bodyColor: '#9097b8',
          padding: 10,
          filter: item => !item.dataset.isThreshold && item.parsed.y != null,
          callbacks: {
            label: ctx => ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(3)}`,
          },
        },
      },
    },
  });
}

// Normalise a reading to {ts, areas: [{name, closed, open, threshold}]}.
// Readings from before multi-area support only carry the top-level scores.
function toScorePoint(ts, areas, sc, so) {
  return {
    ts,
    areas: areas && areas.length
      ? areas.map(a => Array.isArray(a)
          ? { name: a[0], closed: a[1], open: a[2], threshold: a[3] }
          : { name: a.name, closed: a.score_closed, open: a.score_open, threshold: a.threshold })
      : [{ name: null, closed: sc, open: so, threshold: null }],
  };
}

function lineStyle(color, dashed, fill) {
  return {
    borderColor: color,
    backgroundColor: fill ? color + '14' : 'transparent',
    fill,
    borderDash: dashed ? [6, 4] : [],
    tension: 0.35,
    pointRadius: dashed ? 0 : 2,
    pointHoverRadius: 5,
    borderWidth: 2,
  };
}

// One closed + one open line per area. A single area keeps the classic
// green/amber look; several areas use their area colour (solid = closed,
// dashed = open). Areas that didn't exist at a time are gaps (null).
function redrawScoreChart() {
  if (!chart) return;
  const latest = scorePoints.length ? scorePoints[scorePoints.length - 1].areas : [];
  const names = latest.map(a => a.name);
  // Legacy points (no per-area data) belong to the first current area.
  const nameOf = n => n ?? names[0] ?? null;
  const single = names.length <= 1;
  const valueAt = (p, name, key) => {
    const a = p.areas.find(x => nameOf(x.name) === name);
    return a ? a[key] : null;
  };

  const datasets = [];
  names.forEach((name, i) => {
    const prefix = single ? '' : `${name} · `;
    datasets.push({ label: prefix + 'Closed',
      ...lineStyle(single ? '#22c55e' : areaColor(i), false, single),
      data: scorePoints.map(p => valueAt(p, name, 'closed')) });
    datasets.push({ label: prefix + 'Open',
      ...lineStyle(single ? '#f59e0b' : areaColor(i), !single, single),
      data: scorePoints.map(p => valueAt(p, name, 'open')) });
  });
  const thresholds = [...new Set(latest.map(a => a.threshold ?? chartThreshold))];
  thresholds.forEach(t => datasets.push({
    label: 'Threshold', isThreshold: true, data: scorePoints.map(() => t),
    borderColor: 'rgba(239,68,68,.55)', borderDash: [5, 4], borderWidth: 1.5,
    pointRadius: 0, fill: false, tension: 0,
  }));

  chart.data.labels = scorePoints.map(p => formatTs(p.ts));
  chart.data.datasets = datasets;
  chart.update('none');
  renderChartLegend(names, single);
  if (scorePoints.length) document.getElementById('chart-empty').classList.add('hidden');
}

function renderChartLegend(names, single) {
  const el = document.getElementById('chart-legend');
  const key = names.join('\u0000');
  if (el.dataset.key === key) return;
  el.dataset.key = key;
  const threshold = '<span class="legend-item"><span class="legend-dot" style="background:rgba(239,68,68,.6);border-radius:2px;width:14px;height:2px"></span>Threshold</span>';
  el.innerHTML = single
    ? '<span class="legend-item"><span class="legend-dot" style="background:var(--closed)"></span>Closed</span>' +
      '<span class="legend-item"><span class="legend-dot" style="background:var(--open)"></span>Open</span>' + threshold
    : names.map((n, i) => `<span class="legend-item"><span class="legend-dot" style="background:${areaColor(i)}"></span>${escapeHtml(n)}</span>`).join('') +
      '<span class="legend-item legend-note">solid = closed · dashed = open</span>' + threshold;
}

// Seed the chart from the server's persisted history so it shows the last X
// readings regardless of when the page was opened.
async function loadScoreHistory() {
  try {
    const res = await fetch('/api/scores');
    if (!res.ok) return;
    const data = await res.json();
    scorePoints.length = 0;
    (data.scores || []).forEach(p =>
      scorePoints.push(toScorePoint(p.ts, p.areas, p.score_closed, p.score_open)));
    redrawScoreChart();
  } catch (_) {}
}

function updateScoreChart(s) {
  if (!chart || !s.last_update) return;
  if (s.score_closed === 0 && s.score_open === 0) return;

  const last = scorePoints[scorePoints.length - 1];
  if (last && last.ts === s.last_update) return;

  scorePoints.push(toScorePoint(s.last_update, s.areas, s.score_closed, s.score_open));
  if (MAX_CHART_POINTS > 0)
    while (scorePoints.length > MAX_CHART_POINTS) scorePoints.shift();

  redrawScoreChart();
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.tab === tab));
  document.querySelectorAll('.tab-section').forEach(s =>
    s.classList.toggle('active', s.id === `tab-${tab}`));

  if (tab === 'settings') loadSettings();
  if (tab === 'setup')    refreshCropTool();
  if (tab !== 'setup' && areasDirty) toast('Area changes are not saved yet', 'fail');
}

// Cropper.js sizes itself from its container, which is 0×0 while the Setup tab
// is hidden — so a cropper built at page load renders tiny. Rebuild it once the
// tab is visible (and has real dimensions). The saved crop is restored via the
// cropper's ready() callback, so this doesn't lose the user's selection.
function refreshCropTool() {
  const refImg = document.getElementById(`ref-preview-${currentCropSource}`);
  if (refImg && !refImg.classList.contains('hidden')) {
    loadCropImage(currentCropSource);
  }
}

function escapeHtml(str) {
  return String(str ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const SOURCE_LABELS = { ssim: 'via image match', llm: 'via AI', fallback: 'safety fallback' };
const AREA_COLORS = ['#4f7cff', '#c084fc', '#22d3ee', '#f472b6', '#facc15', '#34d399', '#fb923c', '#a3e635'];
const areaColor = i => AREA_COLORS[i % AREA_COLORS.length];

// ── Toast ─────────────────────────────────────────────────────────────────────
function toast(msg, type = '') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast show ${type ? 'toast-' + type : ''}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), 3000);
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = e => applyStatus(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connectWs, 3000);
}

function applyStatus(s) {
  // Header badge
  const badge = document.getElementById('header-badge');
  badge.className = `status-badge badge-${s.state}`;
  badge.textContent = '● ' + (s.state === 'unknown' ? 'Unknown' :
                               s.state === 'closed'  ? 'Closed'  : 'Open');

  // Toggle button
  const btn = document.getElementById('monitor-toggle');
  const lbl = document.getElementById('monitor-toggle-label');
  lbl.textContent = s.running ? 'Stop' : 'Start';
  btn.className = `btn btn-sm ${s.running ? 'btn-outline btn-danger' : 'btn-outline'}`;

  // Status card
  const card = document.getElementById('status-card');
  card.className = `card status-card ${s.state !== 'unknown' ? 'is-' + s.state : ''}`;

  document.getElementById('status-label').textContent =
    s.state === 'closed' ? 'Closed' : s.state === 'open' ? 'Open' : 'Unknown';

  const timeEl = document.getElementById('status-time');
  if (s.last_update) timeEl.dataset.iso = s.last_update;
  timeEl.textContent = s.last_update ? 'Updated ' + relativeTime(s.last_update) : '—';

  // Icons
  document.getElementById('icon-closed').classList.toggle('hidden',  s.state !== 'closed');
  document.getElementById('icon-open').classList.toggle('hidden',    s.state !== 'open');
  document.getElementById('icon-unknown').classList.toggle('hidden', s.state !== 'unknown');

  renderScores(s);

  // Error
  const err = document.getElementById('error-banner');
  if (s.error) { err.textContent = s.error; err.classList.remove('hidden'); }
  else         { err.classList.add('hidden'); }

  // Decision source
  const src = document.getElementById('status-source');
  if (s.source && s.state !== 'unknown') {
    src.textContent = SOURCE_LABELS[s.source] || s.source;
    src.className = `status-source src-${s.source}`;
  } else {
    src.className = 'status-source hidden';
  }

  // History — keyed on the newest entry, since the server caps the list length
  const hist = s.history || [];
  const key = hist.length + ':' + (hist.length ? hist[hist.length - 1].ts : '');
  if (key !== lastHistoryKey) {
    lastHistoryKey = key;
    renderHistory(hist);
  }

  renderAi(s.llm);
  renderSuggestion(s.suggestion);

  // Auto-refresh feed if monitoring
  if (s.running) scheduleFeedRefresh();
  else           stopFeedRefresh();

  updateScoreChart(s);
}

function relativeTime(isoStr) {
  const diff = (Date.now() - new Date(isoStr)) / 1000;
  if (diff < 5)   return 'just now';
  if (diff < 60)  return Math.round(diff) + 's ago';
  if (diff < 3600) return Math.round(diff / 60) + 'm ago';
  return Math.round(diff / 3600) + 'h ago';
}

// One closed/open bar pair per area. Rebuilt only when the set of areas
// changes so the bar widths can animate between readings.
function renderScores(s) {
  const el = document.getElementById('score-areas');
  const list = (s.areas && s.areas.length) ? s.areas
    : [{ name: '', score_closed: s.score_closed, score_open: s.score_open }];
  const multi = list.length > 1;
  const key = list.map(a => a.name).join('\u0000');
  if (el.dataset.key !== key) {
    el.dataset.key = key;
    el.innerHTML = list.map((a, i) => `
      <div class="score-area">
        ${multi ? `<div class="score-area-head">
          <span class="area-swatch" style="background:${areaColor(i)}"></span>
          <span class="score-area-name">${escapeHtml(a.name)}</span>
          <span class="score-flag hidden">below threshold</span>
        </div>` : ''}
        ${['closed', 'open'].map(st => `
        <div class="score-row">
          <span class="score-name">${st === 'closed' ? 'Closed' : 'Open'}</span>
          <div class="score-bar-wrap"><div class="score-bar score-bar-${st}" style="width:0%"></div></div>
          <span class="score-val">—</span>
        </div>`).join('')}
      </div>`).join('');
  }
  el.querySelectorAll('.score-area').forEach((block, i) => {
    const a = list[i];
    block.querySelectorAll('.score-row').forEach((row, j) => {
      const v = j === 0 ? a.score_closed : a.score_open;
      row.querySelector('.score-bar').style.width = Math.round(Math.max(0, v || 0) * 100) + '%';
      row.querySelector('.score-val').textContent = v ? v.toFixed(3) : '—';
    });
    const flag = block.querySelector('.score-flag');
    if (flag) flag.classList.toggle('hidden', a.confident !== false);
  });
  const thresholds = [...new Set(list.map(a => a.threshold).filter(t => t != null))];
  document.getElementById('threshold-note').textContent = thresholds.length
    ? `Threshold ${thresholds.map(t => t.toFixed(2)).join(' / ')}` +
      (multi ? ' · any area below it counts as uncertain' : '')
    : '';
}

function renderHistory(history) {
  const el = document.getElementById('history-list');
  historyCache = history;
  if (!history.length) {
    el.innerHTML = '<p class="empty-state">No events yet.</p>';
    return;
  }
  el.innerHTML = history.map((h, i) => ({ h, i })).reverse().map(({ h, i }) => `
    <button type="button" class="history-item" onclick="openEvent(${i})">
      <span class="history-dot dot-${escapeHtml(h.state)}"></span>
      <span class="history-state" style="color:var(--${h.state === 'closed' ? 'closed' : h.state === 'open' ? 'open' : 'unknown'})">${escapeHtml(h.state)}</span>
      ${h.source && h.source !== 'ssim' ? `<span class="history-source">${escapeHtml(SOURCE_LABELS[h.source] || h.source)}</span>` : ''}
      <span class="history-scores">c=${h.score_closed.toFixed(3)} o=${h.score_open.toFixed(3)}${h.areas && h.areas.length > 1 ? ` · ${h.areas.length} areas` : ''}</span>
      ${h.snapshot ? '<svg class="history-snap" viewBox="0 0 20 20" fill="currentColor" aria-label="Snapshot available"><path fill-rule="evenodd" d="M1 8a2 2 0 0 1 2-2h.93a2 2 0 0 0 1.664-.89l.812-1.22A2 2 0 0 1 8.07 3h3.86a2 2 0 0 1 1.664.89l.812 1.22A2 2 0 0 0 16.07 6H17a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8Zm13.5 3a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM10 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" clip-rule="evenodd"/></svg>' : ''}
      <span class="history-time">${formatTs(h.ts)}</span>
    </button>
  `).join('');
}

function formatTs(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

// ── Event detail (snapshot preview) ───────────────────────────────────────────
function openEvent(i) {
  const h = historyCache[i];
  if (!h) return;
  const stateLabel = h.state.charAt(0).toUpperCase() + h.state.slice(1);
  document.getElementById('event-title').textContent =
    `${stateLabel} · ${new Date(h.ts).toLocaleString()}`;

  const wrap = document.getElementById('event-snapshot-wrap');
  const img = document.getElementById('event-img');
  const missing = document.getElementById('event-missing');
  const boxes = document.getElementById('event-boxes');
  boxes.innerHTML = '';
  if (h.snapshot) {
    const url = `/api/snapshots/${encodeURIComponent(h.snapshot)}`;
    wrap.classList.remove('hidden');
    missing.classList.add('hidden');
    img.onerror = () => {
      wrap.classList.add('hidden');
      missing.textContent = `Snapshot ${h.snapshot} is no longer available (deleted or pruned).`;
      missing.classList.remove('hidden');
    };
    img.src = url;
    // Outline the compared areas on top of the frame.
    const [fw, fh] = h.frame || [];
    if (fw && fh) {
      boxes.innerHTML = (h.areas || []).map((a, j) => {
        if (!a.box) return '';
        const [x, y, w, bh] = a.box;
        const c = areaColor(j);
        return `<div class="snap-box" style="left:${x / fw * 100}%;top:${y / fh * 100}%;width:${w / fw * 100}%;height:${bh / fh * 100}%;border-color:${c}">
          <span style="background:${c}">${escapeHtml(a.name)}</span></div>`;
      }).join('');
    }
  } else {
    wrap.classList.add('hidden');
    img.removeAttribute('src');
    missing.textContent = 'No snapshot was saved for this event (snapshots off, or logged by an older version).';
    missing.classList.remove('hidden');
  }

  const rows = (h.areas || []).map((a, j) => `
    <tr class="${a.confident === false ? 'is-low' : ''}">
      <td><span class="area-swatch" style="background:${areaColor(j)}"></span>${escapeHtml(a.name)}</td>
      <td>${a.score_closed.toFixed(3)}</td>
      <td>${a.score_open.toFixed(3)}</td>
      <td>${a.threshold != null ? a.threshold.toFixed(2) : '—'}</td>
      <td>${a.confident === false ? 'below' : escapeHtml(a.state)}</td>
    </tr>`).join('');
  const ai = h.llm ? `<p class="event-ai"><strong>AI${h.source === 'llm' ? ' (decided)' : ' (consulted)'}:</strong>
      ${escapeHtml(h.llm.state)}, ${Math.round(h.llm.confidence * 100)}% — ${escapeHtml(h.llm.reason)}
      <span class="ai-meta">${escapeHtml(h.llm.model || '')}</span></p>` : '';
  document.getElementById('event-details').innerHTML = `
    <p class="event-meta">Decided ${escapeHtml(SOURCE_LABELS[h.source] || h.source || '')}
      ${h.note ? ` · ${escapeHtml(h.note)}` : ''}
      ${h.snapshot ? ` · <a href="/api/snapshots/${encodeURIComponent(h.snapshot)}" download="${escapeHtml(h.snapshot)}">${escapeHtml(h.snapshot)}</a>` : ''}</p>
    ${ai}
    ${rows ? `<table class="event-table">
      <thead><tr><th>Area</th><th>Closed</th><th>Open</th><th>Threshold</th><th>Says</th></tr></thead>
      <tbody>${rows}</tbody></table>` : ''}`;
  document.getElementById('event-modal').classList.remove('hidden');
}

function closeEvent() {
  document.getElementById('event-modal').classList.add('hidden');
}

// ── Live feed ─────────────────────────────────────────────────────────────────
function refreshFeed() {
  const img = document.getElementById('live-feed');
  const ph  = document.getElementById('feed-placeholder');
  const ts  = Date.now();
  const tmp = new Image();
  tmp.onload = () => {
    img.src = tmp.src;
    img.classList.remove('hidden');
    ph.classList.add('hidden');
  };
  tmp.src = `/api/current.jpg?t=${ts}`;
}

function scheduleFeedRefresh() {
  if (feedInterval) return;
  refreshFeed();
  feedInterval = setInterval(refreshFeed, 5000);
}

function stopFeedRefresh() {
  if (feedInterval) { clearInterval(feedInterval); feedInterval = null; }
}

// ── Monitor toggle ────────────────────────────────────────────────────────────
async function toggleMonitor() {
  const lbl = document.getElementById('monitor-toggle-label').textContent;
  const action = lbl === 'Start' ? 'start' : 'stop';
  const res = await fetch(`/api/monitor/${action}`, { method: 'POST' });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    toast(data.detail || 'Error', 'fail');
  }
}

// ── Setup: capture & upload ───────────────────────────────────────────────────
async function captureRef(state) {
  const btn = document.querySelector(`#ref-card-${state} .btn-primary`);
  btn.disabled = true;
  btn.textContent = 'Capturing…';
  try {
    const res = await fetch(`/api/capture/${state}`, { method: 'POST' });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || 'Capture failed', 'fail'); return; }
    setRefPreview(state, data.image);
    enableCropTool(state);
    toast(`${state} reference captured`, 'ok');
  } catch (e) {
    toast('Network error', 'fail');
  } finally {
    btn.disabled = false;
    btn.innerHTML = `<svg viewBox="0 0 20 20" fill="currentColor"><path fill-rule="evenodd" d="M1 8a2 2 0 0 1 2-2h.93a2 2 0 0 0 1.664-.89l.812-1.22A2 2 0 0 1 8.07 3h3.86a2 2 0 0 1 1.664.89l.812 1.22A2 2 0 0 0 16.07 6H17a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8Zm13.5 3a4.5 4.5 0 1 1-9 0 4.5 4.5 0 0 1 9 0ZM10 14a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z" clip-rule="evenodd"/></svg> Capture from Camera`;
  }
}

async function uploadRef(state, input) {
  const file = input.files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  try {
    const res  = await fetch(`/api/upload/${state}`, { method: 'POST', body: fd });
    const data = await res.json();
    if (!res.ok) { toast(data.detail || 'Upload failed', 'fail'); return; }
    setRefPreview(state, data.image);
    enableCropTool(state);
    toast(`${state} reference uploaded`, 'ok');
  } catch (e) {
    toast('Network error', 'fail');
  }
  input.value = '';
}

function setRefPreview(state, src) {
  const img = document.getElementById(`ref-preview-${state}`);
  const ph  = document.getElementById(`ref-placeholder-${state}`);
  img.src = src;
  img.classList.remove('hidden');
  ph.classList.add('hidden');
}

// ── Comparison areas ──────────────────────────────────────────────────────────
function enableCropTool(preferState) {
  document.getElementById('crop-placeholder').classList.add('hidden');
  currentCropSource = preferState || currentCropSource;
  loadCropImage(currentCropSource);
}

function loadCropImage(state) {
  const refImg = document.getElementById(`ref-preview-${state}`);
  if (!refImg || refImg.classList.contains('hidden')) {
    toast(`No ${state} reference image loaded yet`, 'fail');
    return;
  }
  currentCropSource = state;
  document.getElementById('crop-src-closed').classList.toggle('active', state === 'closed');
  document.getElementById('crop-src-open').classList.toggle('active', state === 'open');

  const cropImg = document.getElementById('crop-image');
  cropImg.classList.remove('hidden');

  if (cropper) { cropper.destroy(); cropper = null; }
  cropImg.src = refImg.src;
  cropImg.onload = () => initCropper(cropImg);
}

function initCropper(img) {
  const gen = ++cropperGen;
  cropperReady = false;
  cropper = new Cropper(img, {
    viewMode: 1,
    autoCrop: false,
    movable: false,
    rotatable: false,
    scalable: false,
    zoomable: false,
    async ready() {
      if (!areasLoaded) await loadAreas();
      if (gen !== cropperGen) return; // replaced while the areas were loading
      cropperReady = true;
      document.getElementById('add-area-btn').disabled = false;
      showSelectedArea();
    },
    crop(event) {
      const d = event.detail;
      document.getElementById('coord-x').textContent = Math.round(d.x);
      document.getElementById('coord-y').textContent = Math.round(d.y);
      document.getElementById('coord-w').textContent = Math.round(d.width);
      document.getElementById('coord-h').textContent = Math.round(d.height);
    },
    // Commit only when the user finished dragging: Cropper also emits `crop`
    // while it is rebuilt or re-laid out (e.g. a window resize with the Setup
    // tab hidden), and those boxes must never overwrite an area.
    cropend() {
      if (gen !== cropperGen || !cropperReady || !cropper?.cropped) return;
      const a = areas[selectedArea];
      const d = cropper.getData(true);
      if (!a || d.width < 1 || d.height < 1) return;
      const box = { x: d.x, y: d.y, w: d.width, h: d.height };
      if (box.x === a.x && box.y === a.y && box.w === a.w && box.h === a.h) return;
      Object.assign(a, box);
      const dims = document.querySelector(`#area-list .area-row[data-i="${selectedArea}"] .area-dims`);
      if (dims) dims.textContent = `${a.w}×${a.h}`;
      markAreasDirty();
    },
  });
}

async function loadAreas() {
  try {
    const res = await fetch('/api/settings');
    const cfg = await res.json();
    areas = (cfg.areas || []).map(a => ({ ...a }));
    chartThreshold = parseFloat(cfg.ssim_threshold) || chartThreshold;
    selectedArea = areas.length ? 0 : -1;
    areasLoaded = true;
    setAreasDirty(false);
    renderAreaList();
    if (areas.length) refreshCropPreviews();
  } catch (_) {}
}

function renderAreaList() {
  const el = document.getElementById('area-list');
  if (!areas.length) {
    el.innerHTML = '<p class="empty-state">No areas — the whole frame is compared. Add an area to focus on the door.</p>';
    return;
  }
  el.innerHTML = areas.map((a, i) => `
    <div class="area-row ${i === selectedArea ? 'selected' : ''}" data-i="${i}" onclick="selectArea(${i})">
      <span class="area-swatch" style="background:${areaColor(i)}"></span>
      <input type="text" class="area-name" value="${escapeHtml(a.name)}" maxlength="40"
             aria-label="Area name" oninput="areas[${i}].name = this.value; markAreasDirty()" />
      <label class="area-thr" title="Threshold for this area. Leave blank to use the global SSIM threshold from Settings.">
        <span>Threshold</span>
        <input type="number" min="0" max="1" step="0.01" placeholder="${chartThreshold.toFixed(2)}"
               value="${a.threshold ?? ''}" aria-label="Area threshold"
               oninput="areas[${i}].threshold = this.value === '' ? null : parseFloat(this.value); markAreasDirty()" />
      </label>
      <span class="area-dims">${a.w}×${a.h}</span>
      <button type="button" class="btn btn-xs btn-ghost" title="Remove area"
              onclick="event.stopPropagation(); removeArea(${i})">✕</button>
    </div>`).join('');
}

function selectArea(i) {
  if (i === selectedArea) return;
  selectedArea = i;
  document.querySelectorAll('#area-list .area-row').forEach(r =>
    r.classList.toggle('selected', +r.dataset.i === i));
  showSelectedArea();
}

// Put the crop box on the selected area and outline all the others.
function showSelectedArea() {
  if (!cropper) return;
  const a = areas[selectedArea];
  if (a) {
    cropper.crop();
    cropper.setData({ x: a.x, y: a.y, width: a.w, height: a.h });
    document.querySelector('.cropper-crop-box')?.style.setProperty('--area-color', areaColor(selectedArea));
  } else {
    cropper.clear();
    ['x', 'y', 'w', 'h'].forEach(k => document.getElementById('coord-' + k).textContent = '—');
  }
  drawAreaOverlays();
}

function drawAreaOverlays() {
  if (!cropper || !cropper.cropper) return;
  const host = cropper.cropper;
  host.querySelectorAll('.area-overlay').forEach(n => n.remove());
  const c = cropper.getCanvasData();
  if (!c.naturalWidth) return;
  const k = c.width / c.naturalWidth;
  areas.forEach((a, i) => {
    if (i === selectedArea) return;
    const div = document.createElement('div');
    div.className = 'area-overlay';
    div.style.cssText = `left:${c.left + a.x * k}px;top:${c.top + a.y * k}px;` +
      `width:${a.w * k}px;height:${a.h * k}px;border-color:${areaColor(i)}`;
    const tag = document.createElement('button');
    tag.type = 'button';
    tag.textContent = a.name || `Area ${i + 1}`;
    tag.style.background = areaColor(i);
    tag.onclick = () => selectArea(i);
    div.appendChild(tag);
    host.appendChild(div);
  });
}

function addArea() {
  if (!cropper) return;
  const img = cropper.getImageData();
  const w = Math.round(img.naturalWidth * 0.25), h = Math.round(img.naturalHeight * 0.25);
  // Offset each new box a little so it doesn't hide exactly behind the last one.
  const off = Math.round(Math.min(img.naturalWidth, img.naturalHeight) * 0.05) * (areas.length % 4);
  areas.push({
    name: `Area ${areas.length + 1}`,
    x: Math.round((img.naturalWidth - w) / 2) + off,
    y: Math.round((img.naturalHeight - h) / 2) + off,
    w, h, threshold: null,
  });
  selectedArea = areas.length - 1;
  renderAreaList();
  showSelectedArea();
  markAreasDirty();
}

function removeArea(i) {
  areas.splice(i, 1);
  if (selectedArea >= areas.length) selectedArea = areas.length - 1;
  renderAreaList();
  showSelectedArea();
  markAreasDirty();
}

function clearAreas() {
  if (!areas.length) return;
  areas = [];
  selectedArea = -1;
  renderAreaList();
  showSelectedArea();
  markAreasDirty();
}

function markAreasDirty() { setAreasDirty(true); }

function setAreasDirty(dirty) {
  areasDirty = dirty;
  document.getElementById('areas-dirty').classList.toggle('hidden', !dirty);
  document.getElementById('save-areas-btn').disabled = !dirty;
}

async function saveAreas() {
  const payload = { areas: areas.map((a, i) => ({ ...a, name: (a.name || '').trim() || `Area ${i + 1}` })) };
  const res = await fetch('/api/areas', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const d = await res.json().catch(() => ({}));
    toast(typeof d.detail === 'string' ? d.detail : 'Failed to save areas', 'fail');
    return;
  }
  areas = payload.areas;
  setAreasDirty(false);
  renderAreaList();
  drawAreaOverlays();
  toast(areas.length ? `${areas.length} area${areas.length > 1 ? 's' : ''} saved` : 'Comparing the whole frame', 'ok');
  refreshCropPreviews();
}

function refreshCropPreviews() {
  const wrap = document.getElementById('crop-previews');
  const list = document.getElementById('crop-preview-list');
  const t = Date.now();
  const shown = areas.length ? areas : [{ name: 'Whole frame' }];
  list.innerHTML = shown.map((a, i) => `
    <div class="crop-preview-area">
      <span class="crop-preview-title"><span class="area-swatch" style="background:${areaColor(i)}"></span>${escapeHtml(a.name)}</span>
      <div class="crop-preview-pair">
        ${['closed', 'open'].map(st => `
        <div class="crop-preview-item">
          <span>${st === 'closed' ? 'Closed' : 'Open'}</span>
          <img src="/api/ref/${st}/preview?area=${i}&t=${t}" alt="${st} processed"
               onerror="this.closest('.crop-preview-item').style.display='none'" />
        </div>`).join('')}
      </div>
    </div>`).join('');
  wrap.classList.remove('hidden');
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function loadSettings() {
  const res = await fetch('/api/settings');
  if (!res.ok) return;
  const cfg = await res.json();
  const form = document.getElementById('settings-form');
  // The saved model may not be in the dropdown until models are loaded
  if (cfg.llm_model) ensureModelOption(cfg.llm_model);
  Object.keys(cfg).forEach(k => {
    const el = form.elements[k];
    if (el) el.value = cfg[k];
  });
  // The server never returns the stored password; blank means "keep it"
  form.elements.mqtt_password.placeholder =
    cfg.mqtt_password_set ? '•••••••• (unchanged — type to replace)' : '';
  document.getElementById('snapshot-dir-note').textContent =
    cfg.snapshot_dir ? ` Folder: ${cfg.snapshot_dir}` : '';
  llmMode = cfg.llm_mode || 'off';
  document.getElementById('ai-card').classList.toggle('hidden', llmMode === 'off');
  if (cfg.ssim_threshold) {
    chartThreshold = parseFloat(cfg.ssim_threshold);
  }
  if (cfg.score_history_size) {
    MAX_CHART_POINTS = parseInt(cfg.score_history_size);
  }
}

async function saveSettings(e) {
  e.preventDefault();
  const form = e.target;
  const result = document.getElementById('save-result');
  // Send only what the form shows; the server merges it into the saved
  // config, so fields edited elsewhere (e.g. the crop box) are kept.
  const cfg = {};
  Array.from(form.elements).forEach(el => {
    if (!el.name) return;
    if (el.name === 'mqtt_password' && !el.value) return;
    if (el.name === 'snapshots_enabled') { cfg[el.name] = el.value === 'true'; return; }
    cfg[el.name] = (el.type === 'number') ? parseFloat(el.value) : el.value;
  });
  const res = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  if (res.ok) {
    result.textContent = '✓ Saved';
    result.className = 'test-result ok';
    toast('Settings saved', 'ok');
    form.elements.mqtt_password.value = '';
    loadSettings();
  } else {
    const d = await res.json().catch(() => ({}));
    result.textContent = d.detail || 'Save failed';
    result.className = 'test-result fail';
  }
  setTimeout(() => { result.textContent = ''; }, 4000);
}

async function testRtsp() {
  const url = document.getElementById('rtsp_url').value;
  const el  = document.getElementById('rtsp-test-result');
  el.textContent = 'Testing…'; el.className = 'test-result';
  const res = await fetch('/api/test/rtsp', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rtsp_url: url }),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok) {
    el.textContent = `✓ Connected — ${data.width}×${data.height}`;
    el.className = 'test-result ok';
  } else {
    el.textContent = '✗ ' + (data.detail || 'Failed');
    el.className = 'test-result fail';
  }
}

async function testMqtt() {
  const form = document.getElementById('settings-form');
  const el   = document.getElementById('mqtt-test-result');
  el.textContent = 'Testing…'; el.className = 'test-result';
  const payload = {
    mqtt_broker:   form.elements.mqtt_broker.value,
    mqtt_port:     parseInt(form.elements.mqtt_port.value),
    mqtt_user:     form.elements.mqtt_user.value,
    mqtt_password: form.elements.mqtt_password.value,
  };
  const res = await fetch('/api/test/mqtt', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok) {
    el.textContent = '✓ Connected';
    el.className = 'test-result ok';
  } else {
    el.textContent = '✗ ' + (data.detail || 'Failed');
    el.className = 'test-result fail';
  }
}

async function registerHomeAssistant() {
  const form = document.getElementById('settings-form');
  const el   = document.getElementById('ha-discover-result');
  el.textContent = 'Registering…'; el.className = 'test-result';
  const payload = {
    mqtt_broker:   form.elements.mqtt_broker.value,
    mqtt_port:     parseInt(form.elements.mqtt_port.value),
    mqtt_user:     form.elements.mqtt_user.value,
    mqtt_password: form.elements.mqtt_password.value,
    mqtt_topic:    form.elements.mqtt_topic.value,
  };
  const res = await fetch('/api/homeassistant/discover', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok) {
    el.textContent = '✓ Registered — find "Garage Monitor" under Settings ▸ Devices';
    el.className = 'test-result ok';
    toast('Added to Home Assistant', 'ok');
  } else {
    el.textContent = '✗ ' + (data.detail || 'Failed');
    el.className = 'test-result fail';
  }
}

// ── AI second opinion ─────────────────────────────────────────────────────────
function renderAi(llm) {
  const card = document.getElementById('ai-card');
  if (llm) card.classList.remove('hidden');
  else if (llmMode === 'off') card.classList.add('hidden');
  if (!llm) return;
  const body = document.getElementById('ai-body');
  const when = llm.ts ? relativeTime(llm.ts) : '';
  if (llm.error) {
    body.innerHTML = `<p class="ai-error">${escapeHtml(llm.error)}</p>
      <p class="ai-meta">${escapeHtml(llm.model || '')}${when ? ' · ' + when : ''}</p>`;
    return;
  }
  const meta = [llm.model, `${llm.latency}s`, llm.load >= 1 ? `incl. ${llm.load}s model load` : '',
                llm.manual ? 'manual check' : '', when].filter(Boolean).join(' · ');
  body.innerHTML = `
    <div class="ai-verdict">
      <span class="ai-state is-${escapeHtml(llm.state)}">${escapeHtml(llm.state)}</span>
      <span class="ai-conf">${Math.round(llm.confidence * 100)}% confident</span>
    </div>
    <p class="ai-reason">${escapeHtml(llm.reason)}</p>
    <p class="ai-meta">${escapeHtml(meta)}</p>`;
}

function renderSuggestion(sug) {
  const card = document.getElementById('suggestion-card');
  if (!sug) { card.classList.add('hidden'); lastSuggestionTs = null; return; }
  if (sug.ts === lastSuggestionTs) return;
  lastSuggestionTs = sug.ts;
  document.getElementById('suggestion-img').src = `/api/suggestion.jpg?t=${Date.now()}`;
  document.getElementById('suggestion-text').textContent =
    `The camera frame no longer matches either reference, but the AI has repeatedly seen the door ${sug.state}. ` +
    `Use this frame as the new "${sug.state}" reference? Check the picture first.`;
  card.classList.remove('hidden');
}

async function acceptSuggestion() {
  const res = await fetch('/api/suggestion/accept', { method: 'POST' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) { toast(data.detail || 'Failed', 'fail'); return; }
  toast(`${data.state} reference updated`, 'ok');
  renderSuggestion(null);
  const img = new Image();
  img.onload = () => setRefPreview(data.state, img.src);
  img.src = `/api/ref/${data.state}.jpg?t=${Date.now()}`;
}

async function dismissSuggestion() {
  await fetch('/api/suggestion/dismiss', { method: 'POST' });
  renderSuggestion(null);
}

async function askAi() {
  const btn = document.getElementById('ai-ask-btn');
  btn.disabled = true; btn.textContent = 'Asking…';
  try {
    const res = await fetch('/api/llm/check', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { toast(data.detail || 'AI check failed', 'fail'); return; }
    renderAi({ ...data, ts: new Date().toISOString() });
  } finally {
    btn.disabled = false; btn.textContent = 'Ask now';
  }
}

function ensureModelOption(name) {
  const sel = document.getElementById('llm_model');
  if (![...sel.options].some(o => o.value === name)) {
    sel.add(new Option(name, name));
  }
}

async function loadLlmModels() {
  const url = document.getElementById('llm_url').value.trim();
  const sel = document.getElementById('llm_model');
  const el  = document.getElementById('llm-test-result');
  el.textContent = 'Loading models…'; el.className = 'test-result';
  const res = await fetch('/api/llm/models?url=' + encodeURIComponent(url));
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    el.textContent = '✗ ' + (data.detail || 'Failed'); el.className = 'test-result fail';
    return;
  }
  const keep = sel.value;
  sel.innerHTML = '';
  data.models.forEach(m => {
    const label = `${m.name}${m.size ? ' (' + m.size + ')' : ''}${m.vision ? '' : ' — no vision'}`;
    const opt = new Option(label, m.name);
    opt.disabled = !m.vision;
    sel.add(opt);
  });
  const vision = data.models.filter(m => m.vision);
  sel.value = vision.some(m => m.name === keep) ? keep : (vision[0]?.name || '');
  el.textContent = vision.length
    ? `✓ ${vision.length} vision model${vision.length > 1 ? 's' : ''} found`
    : '✗ No vision-capable models installed';
  el.className = 'test-result ' + (vision.length ? 'ok' : 'fail');
}

async function testLlm() {
  const form = document.getElementById('settings-form');
  const el   = document.getElementById('llm-test-result');
  el.textContent = 'Asking the model… (first call may load it)'; el.className = 'test-result';
  const payload = {};
  ['llm_url', 'llm_model', 'llm_timeout', 'llm_keep_alive'].forEach(k => {
    const f = form.elements[k];
    payload[k] = f.type === 'number' ? parseFloat(f.value) : f.value;
  });
  const res = await fetch('/api/llm/check', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (res.ok) {
    el.textContent = `✓ ${data.state} (${Math.round(data.confidence * 100)}%) in ${data.latency}s — ${data.reason}`;
    el.className = 'test-result ok';
  } else {
    el.textContent = '✗ ' + (data.detail || 'Failed');
    el.className = 'test-result fail';
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initChart();
  // Pick up the configured threshold + history size, then seed the chart from
  // the server's persisted readings before the live WebSocket starts appending.
  loadSettings().then(loadScoreHistory);
  connectWs();

  // Load existing ref previews
  ['closed', 'open'].forEach(state => {
    const img = new Image();
    img.onload = () => {
      setRefPreview(state, img.src);
      enableCropTool(state);
    };
    img.src = `/api/ref/${state}.jpg?t=${Date.now()}`;
  });

  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeEvent(); });
  // Cropper re-lays itself out on resize; follow with the area outlines.
  let resizeT;
  window.addEventListener('resize', () => { clearTimeout(resizeT); resizeT = setTimeout(drawAreaOverlays, 150); });
  window.addEventListener('beforeunload', e => { if (areasDirty) e.preventDefault(); });

  setInterval(() => {
    const el = document.getElementById('status-time');
    if (el.dataset.iso) el.textContent = 'Updated ' + relativeTime(el.dataset.iso);
  }, 15000);
});
