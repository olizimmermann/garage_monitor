'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let cropper = null;
let cropImageLoaded = false;
let currentCropSource = 'closed';
let ws = null;
let feedInterval = null;
let lastHistoryLength = 0;
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
    data: {
      labels: [],
      datasets: [
        {
          label: 'Closed',
          data: [],
          borderColor: '#22c55e',
          backgroundColor: 'rgba(34,197,94,.08)',
          fill: true,
          tension: 0.35,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 2,
        },
        {
          label: 'Open',
          data: [],
          borderColor: '#f59e0b',
          backgroundColor: 'rgba(245,158,11,.08)',
          fill: true,
          tension: 0.35,
          pointRadius: 2,
          pointHoverRadius: 5,
          borderWidth: 2,
        },
        {
          label: 'Threshold',
          data: [],
          borderColor: 'rgba(239,68,68,.55)',
          borderDash: [5, 4],
          borderWidth: 1.5,
          pointRadius: 0,
          fill: false,
          tension: 0,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
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
          callbacks: {
            label: ctx => {
              if (ctx.dataset.label === 'Threshold') return null;
              return ` ${ctx.dataset.label}: ${ctx.parsed.y.toFixed(3)}`;
            },
          },
        },
      },
    },
  });
}

function redrawScoreChart() {
  if (!chart) return;
  chart.data.labels = scorePoints.map(p => formatTs(p.ts));
  chart.data.datasets[0].data = scorePoints.map(p => p.closed);
  chart.data.datasets[1].data = scorePoints.map(p => p.open);
  chart.data.datasets[2].data = scorePoints.map(() => chartThreshold);
  chart.update('none');
  if (scorePoints.length) document.getElementById('chart-empty').classList.add('hidden');
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
      scorePoints.push({ ts: p.ts, closed: p.score_closed, open: p.score_open }));
    redrawScoreChart();
  } catch (_) {}
}

function updateScoreChart(s) {
  if (!chart || !s.last_update) return;
  if (s.score_closed === 0 && s.score_open === 0) return;

  const last = scorePoints[scorePoints.length - 1];
  if (last && last.ts === s.last_update) return;

  scorePoints.push({ ts: s.last_update, closed: s.score_closed, open: s.score_open });
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

  // Scores
  const sc = s.score_closed, so = s.score_open;
  document.getElementById('bar-closed').style.width = Math.round(sc * 100) + '%';
  document.getElementById('bar-open').style.width   = Math.round(so * 100) + '%';
  document.getElementById('val-closed').textContent = sc ? sc.toFixed(3) : '—';
  document.getElementById('val-open').textContent   = so ? so.toFixed(3) : '—';

  // Error
  const err = document.getElementById('error-banner');
  if (s.error) { err.textContent = s.error; err.classList.remove('hidden'); }
  else         { err.classList.add('hidden'); }

  // History
  if (s.history && s.history.length !== lastHistoryLength) {
    lastHistoryLength = s.history.length;
    renderHistory(s.history);
  }

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

function renderHistory(history) {
  const el = document.getElementById('history-list');
  if (!history.length) {
    el.innerHTML = '<p class="empty-state">No events yet.</p>';
    return;
  }
  el.innerHTML = [...history].reverse().map(h => `
    <div class="history-item">
      <span class="history-dot dot-${h.state}"></span>
      <span class="history-state" style="color:var(--${h.state === 'closed' ? 'closed' : h.state === 'open' ? 'open' : 'unknown'})">${h.state}</span>
      <span class="history-scores">c=${h.score_closed.toFixed(3)} o=${h.score_open.toFixed(3)}</span>
      <span class="history-time">${formatTs(h.ts)}</span>
    </div>
  `).join('');
}

function formatTs(isoStr) {
  const d = new Date(isoStr);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
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

// ── Crop tool ─────────────────────────────────────────────────────────────────
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
  cropper = new Cropper(img, {
    viewMode: 1,
    autoCropArea: 0.6,
    movable: false,
    rotatable: false,
    scalable: false,
    zoomable: false,
    ready() {
      loadCropFromSettings();
      document.getElementById('apply-crop-btn').disabled = false;
    },
    crop(event) {
      const d = event.detail;
      document.getElementById('coord-x').textContent = Math.round(d.x);
      document.getElementById('coord-y').textContent = Math.round(d.y);
      document.getElementById('coord-w').textContent = Math.round(d.width);
      document.getElementById('coord-h').textContent = Math.round(d.height);
    }
  });
}

async function loadCropFromSettings() {
  try {
    const res = await fetch('/api/settings');
    const cfg = await res.json();
    if (cfg.crop_w && cfg.crop_h && cropper) {
      cropper.setData({
        x: cfg.crop_x, y: cfg.crop_y,
        width: cfg.crop_w, height: cfg.crop_h,
      });
    }
  } catch (_) {}
}

function resetCrop() {
  if (cropper) cropper.reset();
}

async function applyCrop() {
  if (!cropper) return;
  const d = cropper.getData(true);
  const payload = { x: d.x, y: d.y, w: d.width, h: d.height };
  const res = await fetch('/api/crop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) { toast('Failed to save crop', 'fail'); return; }
  toast('Crop applied', 'ok');
  refreshCropPreviews();
}

async function refreshCropPreviews() {
  const wrap = document.getElementById('crop-previews');
  const pc   = document.getElementById('crop-preview-closed');
  const po   = document.getElementById('crop-preview-open');
  const t    = Date.now();
  pc.src = `/api/ref/closed/preview?t=${t}`;
  po.src = `/api/ref/open/preview?t=${t}`;
  pc.onerror = () => pc.closest('.crop-preview-item').style.display = 'none';
  po.onerror = () => po.closest('.crop-preview-item').style.display = 'none';
  wrap.classList.remove('hidden');
}

// ── Settings ──────────────────────────────────────────────────────────────────
async function loadSettings() {
  const res = await fetch('/api/settings');
  if (!res.ok) return;
  const cfg = await res.json();
  const form = document.getElementById('settings-form');
  Object.keys(cfg).forEach(k => {
    const el = form.elements[k];
    if (el) el.value = cfg[k];
  });
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
  const cfg = {};
  ['rtsp_url','mqtt_broker','mqtt_port','mqtt_user','mqtt_password',
   'mqtt_topic','check_interval','ssim_threshold','score_history_size',
   'crop_x','crop_y','crop_w','crop_h'].forEach(k => {
    const el = form.elements[k];
    if (el) {
      const v = el.value;
      cfg[k] = (el.type === 'number') ? parseFloat(v) : v;
    }
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

  setInterval(() => {
    const el = document.getElementById('status-time');
    if (el.dataset.iso) el.textContent = 'Updated ' + relativeTime(el.dataset.iso);
  }, 15000);
});
