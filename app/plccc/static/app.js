/* PLC Command Center v2 front-end. Plain JS, no build step. */
(() => {
'use strict';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = s => s == null ? '' : String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const TYPE_LABELS = {allen_bradley_slc:'AB SLC / MicroLogix', allen_bradley_clx:'AB ControlLogix', siemens_s7:'Siemens S7', modbus_tcp:'Modbus TCP', opc_ua:'OPC UA', mqtt:'MQTT'};
const DEFAULT_PORTS = {allen_bradley_slc:44818, allen_bradley_clx:44818, siemens_s7:102, modbus_tcp:502, opc_ua:4840, mqtt:1883};
const ADDR_HINTS = {allen_bradley_slc:'e.g. N7:6  F8:2  B3:0', allen_bradley_clx:'e.g. MyTag  Array[0]', siemens_s7:'e.g. DB6.REAL108  DB1.DBX0.0', modbus_tcp:'e.g. HR4800  IR200  CO5  (1-indexed)',
  opc_ua:'NodeId, e.g. ns=2;s=Pump.GPM  or  ns=3;i=1002', mqtt:'Topic, e.g. farm/generator/temp'};
const OPCUA_POLICIES = ['None', 'Basic256Sha256', 'Aes128Sha256RsaOaep', 'Aes256Sha256RsaPss'];
const CASE_SENSITIVE = {allen_bradley_clx: 1, opc_ua: 1, mqtt: 1};

const state = { view: 'overview', ov: null, devices: [], openDevices: new Set(), hours: 1, custom: null, chart: null, trendTag: null, trendTag2: '', editing: false, editCards: [] };

// ── utils ──────────────────────────────────────────────────────────────────
const utc = ts => ts ? new Date(ts.replace(' ', 'T') + (ts.endsWith('Z') ? '' : 'Z')) : null;
function ago(ts) {
  const d = utc(ts); if (!d) return 'never';
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return Math.round(s) + 's ago';
  if (s < 3600) return Math.round(s / 60) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}
const fmtTime = ts => { const d = utc(ts); return d ? d.toLocaleString([], {month:'short', day:'numeric', hour:'numeric', minute:'2-digit'}) : '–'; };
function fmtNum(v) {
  if (v == null || v === '') return '–';
  const n = Number(v); if (!isFinite(n)) return String(v);
  const a = Math.abs(n);
  const digits = a >= 1000 ? 0 : a >= 100 ? 1 : a >= 10 ? 2 : 3;
  return n.toLocaleString(undefined, {maximumFractionDigits: Number.isInteger(n) ? 0 : digits});
}
function toast(msg, ms = 2400) { const t = $('#toast'); t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), ms); }
async function api(url, opts = {}) {
  const o = Object.assign({headers: {'Content-Type': 'application/json'}}, opts);
  if (o.body && typeof o.body !== 'string') o.body = JSON.stringify(o.body);
  const res = await fetch(url, o);
  let data = null; try { data = await res.json(); } catch (e) {}
  if (!res.ok) { const err = new Error((data && data.error) || ('HTTP ' + res.status)); err.status = res.status; throw err; }
  return data;
}
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

// ── auth (password kept only for this browser tab) ─────────────────────────
const auth = {
  get pw() { return sessionStorage.getItem('plccc_pw'); },
  set pw(v) { v ? sessionStorage.setItem('plccc_pw', v) : sessionStorage.removeItem('plccc_pw'); renderLock(); },
  get unlocked() { return !!this.pw; }
};
function renderLock() {
  const b = $('#lockBtn'); b.classList.toggle('unlocked', auth.unlocked);
  b.setAttribute('aria-label', auth.unlocked ? 'Lock editing' : 'Unlock editing');
  $('#lockPath').setAttribute('d', auth.unlocked
    ? 'M12 2a5 5 0 0 0-5 5h2a3 3 0 0 1 6 0v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5Z'
    : 'M12 2a5 5 0 0 0-5 5v3H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8a2 2 0 0 0-2-2h-1V7a5 5 0 0 0-5-5Zm-3 8V7a3 3 0 0 1 6 0v3H9Z');
  $('#lockState').textContent = auth.unlocked ? 'Unlocked in this browser tab. Changes are saved immediately.' : 'Locked. Unlock with the dashboard password to change settings.';
  $('#unlockBtn2').textContent = auth.unlocked ? 'Lock' : 'Unlock';
}
function requireUnlock() {
  return new Promise(resolve => {
    if (auth.unlocked) return resolve(true);
    openSheet('Unlock editing', `
      <p class="muted small">Enter the dashboard password. It is kept only in this browser tab.</p>
      <label class="field"><span>Password</span><input type="password" id="pwInput" autocomplete="current-password" enterkeyhint="go"></label>
      <div class="form-error" id="pwErr"></div>
      <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="pwGo">Unlock</button></div>`,
      () => resolve(auth.unlocked));
    const go = async () => {
      const pw = $('#pwInput').value; if (!pw) { $('#pwErr').textContent = 'Password required.'; return; }
      try { await api('/api/auth/check', {method: 'POST', body: {password: pw}}); auth.pw = pw; closeSheet(); toast('Unlocked'); }
      catch (e) { $('#pwErr').textContent = e.status === 401 ? 'Incorrect password.' : e.message; }
    };
    $('#pwGo').onclick = go; $('#pwInput').onkeydown = e => { if (e.key === 'Enter') go(); };
    setTimeout(() => $('#pwInput').focus(), 60);
  });
}
async function authed(url, method, body) {
  if (!(await requireUnlock())) throw new Error('Locked');
  try { return await api(url, {method, body: Object.assign({}, body, {password: auth.pw})}); }
  catch (e) { if (e.status === 401) { auth.pw = null; e.message = 'Incorrect password — unlock again.'; } throw e; }
}

// ── bottom sheet ───────────────────────────────────────────────────────────
let sheetOnClose = null;
function openSheet(title, html, onClose) {
  $('#sheetTitle').textContent = title; $('#sheetBody').innerHTML = html;
  $('#sheet').hidden = false; $('#sheetBackdrop').hidden = false; document.body.style.overflow = 'hidden';
  sheetOnClose = onClose || null;
  $$('[data-close]', $('#sheet')).forEach(b => b.onclick = closeSheet);
}
function closeSheet() {
  $('#sheet').hidden = true; $('#sheetBackdrop').hidden = true; document.body.style.overflow = '';
  const cb = sheetOnClose; sheetOnClose = null; if (cb) cb();
}

// ── navigation ─────────────────────────────────────────────────────────────
function show(view) {
  state.view = view;
  $$('.tabbar button').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  $$('.view').forEach(v => v.classList.toggle('active', v.id === 'view-' + view));
  if (location.hash !== '#' + view) history.replaceState(null, '', '#' + view);
  window.scrollTo({top: 0});
  if (view === 'trends') loadTrend();
  if (view === 'devices') loadDevices();
  if (view === 'settings') renderSettings();
}

// ── overview ───────────────────────────────────────────────────────────────
function allTags(ov) { const out = []; (ov.devices || []).forEach(d => d.tags.forEach(t => out.push(Object.assign({device: d.name, device_id: d.id, device_type: d.type}, t)))); return out; }
function tagStatus(t) {
  if (!t.enabled) return 'off';
  if (t.value == null || t.stale) return 'warn';
  if (t.alarm_enabled && t.alarm_active) return 'bad';
  return 'ok';
}
function limitsText(t) {
  const u = t.units ? ' ' + t.units : ''; const p = [];
  if (t.alarm_low != null) p.push('< ' + fmtNum(t.alarm_low) + u);
  if (t.alarm_high != null) p.push('> ' + fmtNum(t.alarm_high) + u);
  return p.join(' · ');
}
async function loadOverview() {
  try { state.ov = await api('/api/overview'); renderOverview(); if (state.view === 'settings') renderSettings(); }
  catch (e) { $('#banners').innerHTML = `<div class="banner bad">⚠️ Can't reach the dashboard server (${esc(e.message)}). Retrying…</div>`; }
}
function renderOverview() {
  const ov = state.ov; if (!ov) return;
  // header + banners
  const p = ov.poller || {}; const pill = $('#sysPill');
  pill.className = 'pill ' + (p.alive ? 'ok' : 'bad');
  $('.pill-text', pill).textContent = p.alive ? (ov.dry_run ? 'Dry run' : 'Live') : 'Poller down';
  const online = ov.devices.filter(d => d.enabled && d.connected).length, enabled = ov.devices.filter(d => d.enabled).length;
  $('#subtitle').textContent = `${online}/${enabled} devices online · ${ov.active_alarms.length ? ov.active_alarms.length + ' alarm' + (ov.active_alarms.length > 1 ? 's' : '') : 'no alarms'}`;
  let b = '';
  if (ov.dry_run) b += `<div class="banner info">🧪 Dry-run mode: alerts are logged, not sent.</div>`;
  if (p.alive === false) b += `<div class="banner bad">⛔ The poller hasn't reported for ${p.age_s != null ? Math.round(p.age_s) + 's' : 'a while'}. Alarms may not be running.</div>`;
  if (p.alive == null) b += `<div class="banner warn">The unified poller has not started yet on this database.</div>`;
  $('#banners').innerHTML = b;

  // active alarms
  $('#alarmList').innerHTML = ov.active_alarms.map(a => `
    <div class="alarm-card ${a.acked ? 'acked' : ''}">
      <div><div class="t ${a.acked ? '' : 'pulse'}">🚨 ${esc(a.name)} · ${fmtNum(a.value)} ${esc(a.units)}</div>
      <div class="s">${esc(a.device)} · ${esc(a.alarm_stage_label || 'Alarm')} · since ${fmtTime(a.triggered_at)}</div></div>
      ${a.escalating && !a.acked ? `<button class="btn danger" data-ack>Acknowledge</button>` : (a.acked ? '<span class="status-chip warn">Acked</span>' : '')}
    </div>`).join('');
  $$('[data-ack]').forEach(btn => btn.onclick = ackAlarm);

  renderCards();

  // devices
  $('#deviceChips').innerHTML = ov.devices.map(d => `
    <span class="chip"><span class="dot ${!d.enabled ? '' : d.connected ? 'ok' : 'bad'}"></span>${esc(d.name)}
    <span class="when">${d.enabled ? ago(d.last_seen) : 'disabled'}</span></span>`).join('');
  $$('[data-trend]').forEach(el => { el.onclick = () => { state.trendTag = +el.dataset.trend; state.trendTag2 = ''; show('trends'); }; });
}
// ── overview cards (customizable layout) ─────────────────────────────────────
const CHART_HOURS = [1, 6, 24];
const isDiscrete = t => /^BOOL$/i.test(t.data_type || '') || /\.DBX|^B\d+:|^CO\d|^DI\d/i.test(t.address || '');
const devShort = name => String(name || '').replace(/\s*\(.*\)$/, '');
function layoutCards() { return state.editing ? state.editCards : ((state.ov.layout && state.ov.layout.cards) || []); }
function tagById(id) { return allTags(state.ov).find(t => t.id === id); }
function chartLimits(t) {
  if (t.escalating) return {low: state.ov.threshold_gpm, high: null};
  return {low: t.alarm_low, high: t.alarm_high};
}
function chartCardHtml(t, card, i) {
  const ov = state.ov, dev = ov.devices.find(d => d.id === t.device_id) || {tags: []};
  const lim = chartLimits(t), hasLimits = lim.low != null || lim.high != null;
  const below = t.value != null && lim.low != null && t.value < lim.low;
  const above = t.value != null && lim.high != null && t.value > lim.high;
  let chip;
  if (t.value == null || t.stale) chip = '<span class="status-chip warn">No recent data</span>';
  else if (t.alarm_active && (t.escalating || t.alarm_enabled)) chip = `<span class="status-chip bad">${esc(t.alarm_stage_label || 'Alarm')}</span>`;
  else if ((below || above) && !t.alarm_enabled) chip = `<span class="status-chip off">${below ? 'Below threshold' : 'Above limit'} · alarm off</span>`;
  else if (below || above) chip = `<span class="status-chip warn">${below ? 'Below threshold' : 'Above limit'}</span>`;
  else chip = hasLimits ? '<span class="status-chip ok">Normal</span>' : '<span class="status-chip ok">Live</span>';
  const meta = [];
  if (lim.low != null) meta.push(`<span>Alarm below <b>${fmtNum(lim.low)}</b></span>`);
  if (lim.high != null) meta.push(`<span>Alarm above <b>${fmtNum(lim.high)}</b></span>`);
  if (hasLimits) meta.push(`<span>Alarm <b>${t.alarm_enabled ? 'on' : 'off'}</b></span>`);
  if (t.escalating) { const lv = dev.tags.find(x => x.id !== t.id); if (lv) meta.push(`<span>${esc(lv.name)} <b>${fmtNum(lv.value)}</b></span>`); }
  meta.push(`<span><span class="dot ${dev.connected ? 'ok' : 'bad'}"></span> ${dev.connected ? 'PLC online' : 'PLC offline'} · ${ago(t.timestamp)}</span>`);
  const units = t.units || (t.escalating ? 'GPM' : '');
  const edit = state.editing;
  return `
      <div class="card hero" ${edit ? '' : `role="button" tabindex="0" data-trend="${t.id}"`}>
        <div class="hero-top"><div><div class="hero-label">${esc(devShort(t.device))} · ${esc(t.name)}</div></div>${chip}</div>
        <div class="hero-value">${t.value == null ? '–' : fmtNum(t.value)}<small>${esc(units)}</small></div>
        <div class="hero-meta">
          ${meta.join('\n          ')}
        </div>
        <svg class="spark" id="spark-${i}" viewBox="0 0 300 64" preserveAspectRatio="none" aria-hidden="true"></svg>
        ${edit ? editCtl(card, i) : ''}
      </div>`;
}
function tileHtml(t, card, i) {
  const st = tagStatus(t), edit = state.editing, tagName = edit ? 'div' : 'button';
  const badge = t.alarm_enabled ? (t.alarm_active ? '<span class="badge bad">ALARM</span>' : '<span class="badge ok">🔔 armed</span>') : '';
  return `<${tagName} class="card tile ${st}" ${edit ? '' : `data-trend="${t.id}"`}>
      <div class="name">${esc(t.name)}</div><div class="dev">${esc(t.device)}</div>
      <div class="val">${fmtNum(t.value)}<small>${esc(t.units)}</small></div>
      <div class="foot"><span>${ago(t.timestamp)}</span>${badge}</div>${edit ? editCtl(card, i) : ''}</${tagName}>`;
}
function missingHtml(card, i) {
  return `<div class="card tile off"><div class="name">Deleted tag</div><div class="dev">Tag #${card.tag_id} no longer exists</div>${editCtl(card, i)}</div>`;
}
function editCtl(card, i) {
  const t = tagById(card.tag_id);
  const range = card.type === 'chart' ? `<div class="seg mini" role="group" aria-label="Chart range">${CHART_HOURS.map(h =>
    `<button type="button" data-ec="hours" data-i="${i}" data-h="${h}" class="${(card.hours || 6) === h ? 'active' : ''}">${h}h</button>`).join('')}</div>` : '';
  const canSwap = t && (card.type === 'chart' || !isDiscrete(t));
  return `<div class="edit-ctl">
      <button type="button" class="ec-btn" data-ec="up" data-i="${i}" aria-label="Move up" ${moveTarget(i, -1) < 0 ? 'disabled' : ''}>↑</button>
      <button type="button" class="ec-btn" data-ec="down" data-i="${i}" aria-label="Move down" ${moveTarget(i, 1) < 0 ? 'disabled' : ''}>↓</button>
      ${canSwap ? `<button type="button" class="ec-btn wide" data-ec="swap" data-i="${i}">${card.type === 'chart' ? 'Tag box' : 'Chart'}</button>` : ''}
      ${range}
      <button type="button" class="ec-btn del" data-ec="del" data-i="${i}" aria-label="Remove card">✕</button>
    </div>`;
}
const deskMQ = window.matchMedia ? window.matchMedia('(min-width: 960px)') : {matches: false};
// On desktop charts sit in the left column and tag boxes in the right (like the classic Overview), so the
// arrows move a card past the nearest card of the same kind; on phones the order is exactly the list order.
function moveTarget(i, dir) {
  const cs = state.editCards;
  if (!deskMQ.matches) { const j = i + dir; return j >= 0 && j < cs.length ? j : -1; }
  const kind = c => c.type === 'chart' ? 'chart' : 'tile';
  for (let j = i + dir; j >= 0 && j < cs.length; j += dir) if (kind(cs[j]) === kind(cs[i])) return j;
  return -1;
}
function renderCards() {
  const ov = state.ov, cards = layoutCards(), edit = !!state.editing, desk = deskMQ.matches;
  let html = '', charts = '', tileBuf = [], titled = false, sparks = [];
  const flush = () => {
    if (!tileBuf.length) return;
    if (!titled) { html += '\n    <h2 class="section-title">Live values</h2>'; titled = true; }
    html += `\n    <div class="tiles">${tileBuf.join('')}</div>`; tileBuf = [];
  };
  cards.forEach((card, i) => {
    const t = tagById(card.tag_id);
    if (!t) { if (edit) tileBuf.push(missingHtml(card, i)); return; }
    if (card.type === 'chart') {
      const h = `<div class="ov-chart">${chartCardHtml(t, card, i)}</div>`;
      if (desk) charts += h; else { flush(); html += h; }
      sparks.push([i, t, card.hours || 6]);
    } else tileBuf.push(tileHtml(t, card, i));
  });
  flush();
  if (!cards.length) html = `<h2 class="section-title">Live values</h2><div class="empty">${edit ? 'No cards yet. Tap “Add card”.' : 'No cards on the Overview. Tap “Customize” to add some.'}</div>`;
  const ec = $('#ovCards'), ch = $('#ovCharts');
  ec.classList.toggle('editing', edit); ch.classList.toggle('editing', edit);
  ec.innerHTML = html; ch.innerHTML = charts;
  $('#view-overview').classList.toggle('no-charts', !charts);
  $('#ovEditTop').innerHTML = edit ? `
    <div class="edit-bar card">
      <div><div class="eb-title">Customize Overview</div><div class="muted small">${cards.length} card${cards.length === 1 ? '' : 's'} · reorder with ↑ ↓</div></div>
      <button type="button" class="btn primary small" id="ovAdd">＋ Add card</button>
    </div>` : '';
  $('#ovEditBottom').innerHTML = edit ? `
    <div class="edit-actions">
      <button type="button" class="btn ghost small" id="ovReset">Reset to default</button>
      <span class="grow"></span>
      <button type="button" class="btn ghost" id="ovCancel">Cancel</button>
      <button type="button" class="btn primary" id="ovSave">Save</button>
    </div>` : `
    <div class="edit-actions"><span class="grow"></span><button type="button" class="btn ghost small" id="ovEdit">✎ Customize</button></div>`;
  wireEdit();
  sparks.forEach(([i, t, h]) => loadSpark(i, t, h));
}
if (deskMQ.addEventListener) deskMQ.addEventListener('change', () => { if (state.ov) renderOverview(); });
const _sparkCache = {};
async function loadSpark(i, t, hours) {
  const key = t.id + ':' + hours;
  try {
    let d = _sparkCache[key];
    if (!d || Date.now() - d.at > 14000) { d = {at: Date.now(), data: await api(`/api/tag_history?tag_id=${t.id}&hours=${hours}&max_points=300`)}; _sparkCache[key] = d; }
    drawSpark($('#spark-' + i), d.data.points, chartLimits(t), i);
  } catch (e) {}
}
function drawSpark(svg, points, lim, i) {
  const pts = points.filter(p => p.y != null); if (!svg || pts.length < 2) return;
  const lines = [lim.low, lim.high].filter(v => v != null);
  const xs = pts.map(p => utc(p.x).getTime()), ys = pts.map(p => p.y);
  const x0 = Math.min(...xs), x1 = Math.max(...xs); let y0 = Math.min(...ys, ...lines), y1 = Math.max(...ys, ...lines); if (y1 === y0) y1 = y0 + 1;
  const X = x => ((x - x0) / (x1 - x0 || 1)) * 300, Y = y => 60 - ((y - y0) / (y1 - y0)) * 56;
  const line = pts.map((p, k) => (k ? 'L' : 'M') + X(xs[k]).toFixed(1) + ' ' + Y(p.y).toFixed(1)).join(' ');
  const c = cssVar('--c1'), bad = cssVar('--bad');
  svg.innerHTML = `<defs><linearGradient id="sg-${i}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${c}" stop-opacity=".28"/><stop offset="1" stop-color="${c}" stop-opacity="0"/></linearGradient></defs>
      <path d="${line} L300 64 L0 64 Z" fill="url(#sg-${i})"/><path d="${line}" fill="none" stroke="${c}" stroke-width="2" vector-effect="non-scaling-stroke"/>` +
    lines.map(v => `<line x1="0" x2="300" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="${bad}" stroke-width="1" stroke-dasharray="4 4" vector-effect="non-scaling-stroke"/>`).join('');
}
function wireEdit() {
  const on = (id, fn) => { const el = $('#' + id); if (el) el.onclick = fn; };
  on('ovEdit', startEdit); on('ovAdd', openAddCard); on('ovCancel', () => { state.editing = false; renderOverview(); toast('Changes discarded'); });
  on('ovSave', saveLayout); on('ovReset', resetLayout);
  $$('#ovCards [data-ec], #ovCharts [data-ec]').forEach(b => b.onclick = e => {
    e.stopPropagation();
    const i = +b.dataset.i, cs = state.editCards, op = b.dataset.ec;
    if (op === 'up' || op === 'down') { const j = moveTarget(i, op === 'up' ? -1 : 1); if (j >= 0) [cs[j], cs[i]] = [cs[i], cs[j]]; }
    if (op === 'del') cs.splice(i, 1);
    if (op === 'swap') cs[i] = cs[i].type === 'chart' ? {type: 'tile', tag_id: cs[i].tag_id} : {type: 'chart', tag_id: cs[i].tag_id, hours: 6};
    if (op === 'hours') cs[i].hours = +b.dataset.h;
    renderCards();
  });
}
async function startEdit() {
  if (!(await requireUnlock())) return;
  state.editing = true;
  state.editCards = JSON.parse(JSON.stringify((state.ov.layout && state.ov.layout.cards) || []));
  renderOverview();
  $('#ovEditTop').scrollIntoView({block: 'start', behavior: 'smooth'});
}
async function saveLayout() {
  try {
    const r = await authed('/api/overview_layout', 'PUT', {cards: state.editCards});
    state.ov.layout = r; state.editing = false; renderOverview(); toast('Overview saved');
  } catch (e) { if (e.message !== 'Locked') toast(e.message); }
}
async function resetLayout() {
  if (!confirm('Reset the Overview to the default cards?')) return;
  try {
    const r = await authed('/api/overview_layout', 'DELETE', {});
    state.ov.layout = r; state.editing = false; renderOverview(); toast('Overview reset to default');
  } catch (e) { if (e.message !== 'Locked') toast(e.message); }
}
function openAddCard() {
  const ov = state.ov;
  const devs = ov.devices.filter(d => d.tags.length);
  if (!devs.length) { toast('Add a device and tags first'); return; }
  openSheet('Add card', `
    <label class="field"><span>Device</span><select id="acDev">${devs.map(d => `<option value="${d.id}">${esc(d.name)}</option>`).join('')}</select></label>
    <label class="field"><span>Tag</span><select id="acTag"></select></label>
    <div class="field"><span>Card type</span>
      <div class="seg two" id="acType" role="group">
        <button type="button" data-type="tile">Tag box</button><button type="button" data-type="chart">Chart</button>
      </div>
      <small class="muted" id="acHint"></small>
    </div>
    <div class="field" id="acRangeWrap"><span>Chart range</span>
      <div class="seg three" id="acRange" role="group">${CHART_HOURS.map(h => `<button type="button" data-h="${h}" class="${h === 6 ? 'active' : ''}">${h}h</button>`).join('')}</div>
    </div>
    <div class="ac-preview" id="acPreview"></div>
    <div class="sheet-actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="acGo">Add to Overview</button></div>`);
  let type = 'tile', hours = 6;
  const tagSel = $('#acTag');
  const fillTags = () => {
    const d = devs.find(x => x.id === +$('#acDev').value);
    tagSel.innerHTML = d.tags.map(t => `<option value="${t.id}">${esc(t.name)}${t.units ? ' (' + esc(t.units) + ')' : ''}${t.enabled ? '' : ' · disabled'}</option>`).join('');
    sync(true);
  };
  const sync = (pickDefault) => {
    const t = tagById(+tagSel.value); if (!t) return;
    const discrete = isDiscrete(t);
    if (pickDefault) type = discrete ? 'tile' : 'chart';
    if (discrete) type = 'tile';
    $$('#acType button').forEach(b => { b.classList.toggle('active', b.dataset.type === type); b.disabled = discrete && b.dataset.type === 'chart'; });
    $('#acHint').textContent = discrete ? 'On/off tag: tag box only.' : 'Analog tag: chart shows a live trend' + (t.escalating || t.alarm_low != null || t.alarm_high != null ? ' with the alarm line.' : '.');
    $('#acRangeWrap').hidden = type !== 'chart';
    $$('#acRange button').forEach(b => b.classList.toggle('active', +b.dataset.h === hours));
  };
  $('#acDev').onchange = fillTags;
  tagSel.onchange = () => sync(true);
  $$('#acType button').forEach(b => b.onclick = () => { type = b.dataset.type; sync(false); });
  $$('#acRange button').forEach(b => b.onclick = () => { hours = +b.dataset.h; sync(false); });
  $('#acGo').onclick = () => {
    const id = +tagSel.value; if (!id) return;
    state.editCards.push(type === 'chart' ? {type, tag_id: id, hours} : {type, tag_id: id});
    closeSheet(); renderCards();
    const t = tagById(id); toast(`Added ${type === 'chart' ? 'chart' : 'tag box'}: ${t ? t.name : ''} — tap Save to keep it`);
    setTimeout(() => { const all = $$(type === 'chart' ? '#ovCharts .card, #ovCards .ov-chart .card' : '#ovCards .tile'); if (all.length) all[all.length - 1].scrollIntoView({block: 'center', behavior: 'smooth'}); }, 60);
  };
  fillTags();
}
async function ackAlarm() {
  try { await authed('/api/ack', 'POST', {}); toast('Acknowledged — escalations silenced'); setTimeout(loadOverview, 1500); }
  catch (e) { if (e.message !== 'Locked') toast(e.message); }
}

// ── trends ─────────────────────────────────────────────────────────────────
function fillTagSelects() {
  const ov = state.ov; if (!ov) return;
  const opts = ov.devices.map(d => `<optgroup label="${esc(d.name)}">${d.tags.map(t => `<option value="${t.id}">${esc(t.name)}${t.units ? ' (' + esc(t.units) + ')' : ''}</option>`).join('')}</optgroup>`).join('');
  $('#trendTag').innerHTML = opts; $('#trendTag2').innerHTML = '<option value="">None</option>' + opts;
  if (!state.trendTag) { const h = allTags(ov).find(t => t.escalating) || allTags(ov)[0]; state.trendTag = h && h.id; }
  if (state.trendTag2 === '' && state.trendTag) {
    const t = allTags(ov).find(x => x.id === state.trendTag);
    if (t && t.escalating) { const lv = ov.devices.find(d => d.id === t.device_id).tags.find(x => x.id !== t.id); if (lv) state.trendTag2 = String(lv.id); }
  }
  $('#trendTag').value = state.trendTag || ''; $('#trendTag2').value = +state.trendTag2 > 0 ? state.trendTag2 : '';
}
function rangeQuery() {
  if (state.custom) return `start=${encodeURIComponent(state.custom[0])}&end=${encodeURIComponent(state.custom[1])}`;
  return 'hours=' + state.hours;
}
async function loadTrend() {
  if (!state.ov) await loadOverview();
  fillTagSelects();
  const id = state.trendTag; if (!id) return;
  const id2 = +state.trendTag2 > 0 && +state.trendTag2 !== +id ? +state.trendTag2 : null;
  try {
    const [a, b] = await Promise.all([api(`/api/tag_history?tag_id=${id}&${rangeQuery()}`), id2 ? api(`/api/tag_history?tag_id=${id2}&${rangeQuery()}`) : null]);
    const t = allTags(state.ov).find(x => x.id === +id) || {};
    drawChart(a, b, t);
    $('#chartTitle').textContent = a.tag.name + (b ? ' vs ' + b.tag.name : '');
    $('#chartSub').textContent = `${a.stats.count.toLocaleString()} readings${a.downsampled ? ' · condensed for display' : ''}`;
    const u = a.tag.units ? ' ' + a.tag.units : '';
    $('#trendStats').innerHTML = [['Last', a.stats.last], ['Min', a.stats.min], ['Avg', a.stats.avg], ['Max', a.stats.max]]
      .map(([k, v]) => `<div class="stat"><div class="k">${k}</div><div class="v">${fmtNum(v)}</div><div class="u">${esc(u.trim()) || '&nbsp;'}</div></div>`).join('');
  } catch (e) { toast('Could not load trend: ' + e.message); }
}
function drawChart(a, b, tag) {
  const c1 = cssVar('--c1'), c2 = cssVar('--c2'), grid = cssVar('--grid'), text = cssVar('--muted'), bad = cssVar('--bad');
  const hrs = state.custom ? 48 : state.hours;
  const unit = hrs <= 6 ? 'minute' : hrs <= 48 ? 'hour' : 'day';
  const ds = [{label: a.tag.name + (a.tag.units ? ` (${a.tag.units})` : ''), data: a.points, borderColor: c1, backgroundColor: c1 + '22', fill: true, yAxisID: 'y', borderWidth: 2, pointRadius: 0, pointHitRadius: 8, tension: .15}];
  if (b) ds.push({label: b.tag.name + (b.tag.units ? ` (${b.tag.units})` : ''), data: b.points, borderColor: c2, backgroundColor: c2 + '18', fill: false, yAxisID: 'y1', borderWidth: 2, pointRadius: 0, pointHitRadius: 8, tension: .15});
  const thr = tag.escalating ? state.ov.threshold_gpm : null;
  const limits = [];
  if (thr != null) limits.push(['Alarm threshold', thr]);
  else { if (tag.alarm_enabled && tag.alarm_low != null) limits.push(['Low limit', tag.alarm_low]); if (tag.alarm_enabled && tag.alarm_high != null) limits.push(['High limit', tag.alarm_high]); }
  if (a.points.length) limits.forEach(([lbl, v]) => ds.push({label: lbl, data: [{x: a.points[0].x, y: v}, {x: a.points[a.points.length - 1].x, y: v}], borderColor: bad, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, fill: false, yAxisID: 'y'}));
  const cfg = {
    type: 'line', data: {datasets: ds},
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, normalized: true, parsing: {xAxisKey: 'x', yAxisKey: 'y'},
      interaction: {mode: 'index', intersect: false, axis: 'x'},
      scales: {
        x: {type: 'time', time: {unit, tooltipFormat: 'MMM d, h:mm a', displayFormats: {minute: 'h:mm a', hour: 'h a', day: 'MMM d'}}, grid: {color: grid}, ticks: {color: text, maxRotation: 0, autoSkipPadding: 14}},
        y: {position: 'left', grid: {color: grid}, ticks: {color: text}, title: {display: !!a.tag.units, text: a.tag.units, color: text}, beginAtZero: !!tag.escalating},
        y1: {display: !!b, position: 'right', grid: {drawOnChartArea: false}, ticks: {color: text}, title: {display: !!(b && b.tag.units), text: b ? b.tag.units : '', color: text}}
      },
      plugins: {
        legend: {labels: {color: text, boxWidth: 12, usePointStyle: true}},
        tooltip: {mode: 'index', intersect: false},
        zoom: {limits: {x: {min: 'original', max: 'original'}}, pan: {enabled: true, mode: 'x'}, zoom: {wheel: {enabled: true}, pinch: {enabled: true}, mode: 'x'}}
      }
    }
  };
  if (state.chart) state.chart.destroy();
  state.chart = new Chart($('#trendChart'), cfg);
}

// ── devices ────────────────────────────────────────────────────────────────
async function loadDevices() {
  try { const [devs] = await Promise.all([api('/api/devices'), state.ov ? null : loadOverview()]); state.devices = devs; renderDevices(); }
  catch (e) { $('#deviceList').innerHTML = `<div class="card empty">Error loading devices: ${esc(e.message)}</div>`; }
}
function ovTag(id) { return state.ov ? allTags(state.ov).find(t => t.id === id) : null; }
function ovDev(id) { return state.ov ? state.ov.devices.find(d => d.id === id) : null; }
function renderDevices() {
  if (!state.devices.length) { $('#deviceList').innerHTML = '<div class="card empty">No devices configured yet.</div>'; return; }
  $('#deviceList').innerHTML = state.devices.map(d => {
    const o = ovDev(d.id) || {}; const rt = o.runtime || {};
    const st = !d.enabled ? '' : o.connected ? 'ok' : 'bad';
    const stTxt = !d.enabled ? 'Disabled' : o.connected ? 'Online' : (o.last_seen ? 'Offline' : 'No data');
    const tags = d.tags.map(t => {
      const s = ovTag(t.id) || {};
      let badge = '';
      if (t.alarm_enabled) badge = s.alarm_active ? '<span class="badge bad">ALARM</span>' : `<span class="badge ok">🔔 ${esc(limitsText(s.id ? s : t)) || 'armed'}</span>`;
      else if (t.alarm_low != null || t.alarm_high != null || s.escalating) badge = '<span class="badge off">alarm off</span>';
      if (!t.enabled) badge += ' <span class="badge off">disabled</span>';
      return `<div class="tag-row" data-edit-tag="${t.id}" data-dev="${d.id}">
        <span class="dot ${s.value != null && !s.stale ? 'ok' : ''}"></span>
        <div class="main"><div class="tn">${esc(t.name)}</div><div class="ta"><span class="mono">${esc(t.address)}</span> · ${esc(t.data_type)}${s.escalating ? ' · escalating' : ''}</div>${badge}</div>
        <div class="tv">${fmtNum(s.value)} <small>${esc(t.units)}</small></div></div>`;
    }).join('') || '<div class="empty small">No tags yet.</div>';
    return `<div class="card dev-card ${state.openDevices.has(d.id) ? 'open' : ''}" data-dev-card="${d.id}">
      <div class="dev-head" data-toggle="${d.id}">
        <span class="dot ${st}"></span>
        <div class="main"><div class="name">${esc(d.name)}</div>
          <div class="sub">${esc(stTxt)} · ${o.last_seen ? ago(o.last_seen) : '–'} · <span class="mono">${d.type === 'opc_ua' ? esc(d.host.replace(/^opc\.tcp:\/\//i, '')) : esc(d.host) + ':' + (d.port || '–')}</span></div></div>
        <span class="type-badge ${esc(d.type)}">${esc(TYPE_LABELS[d.type] || d.type)}</span>
        <svg class="chev" viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="m9 6 6 6-6 6-1.4-1.4L12.2 12 7.6 7.4 9 6Z"/></svg>
      </div>
      <div class="dev-body">
        ${d.enabled && rt.online === 0 && rt.last_error ? `<div class="dev-err">Last error: ${esc(rt.last_error)} (${rt.fail_count} failed polls)</div>` : ''}
        ${d.description ? `<div class="muted small" style="padding:10px 14px 0">${esc(d.description)}</div>` : ''}
        <div>${tags}</div>
        <div class="dev-actions"><button class="btn small" data-add-tag="${d.id}">+ Tag</button><button class="btn small ghost" data-edit-dev="${d.id}">Edit device</button></div>
      </div></div>`;
  }).join('');
  $$('[data-toggle]').forEach(el => el.onclick = () => { const id = +el.dataset.toggle; state.openDevices.has(id) ? state.openDevices.delete(id) : state.openDevices.add(id); el.parentElement.classList.toggle('open'); });
  $$('[data-edit-dev]').forEach(el => el.onclick = () => deviceSheet(+el.dataset.editDev));
  $$('[data-add-tag]').forEach(el => el.onclick = () => tagSheet(+el.dataset.addTag, null));
  $$('[data-edit-tag]').forEach(el => el.onclick = () => tagSheet(+el.dataset.dev, +el.dataset.editTag));
}
const val = id => $('#' + id).value.trim();
const num = (id, dflt) => { const v = $('#' + id).value; return v === '' ? dflt : Number(v); };
async function deviceSheet(id) {
  if (!(await requireUnlock())) return;
  const d = id ? state.devices.find(x => x.id === id) : {type: 'allen_bradley_slc', enabled: 1, rack: 0, slot: 1, unit_id: 1, word_swap: 0, options: {}};
  const o = d.options || {};
  openSheet(id ? 'Edit device' : 'Add device', `
    <label class="field"><span>Name *</span><input id="fName" value="${esc(d.name)}" placeholder="e.g. Pump Room PLC"></label>
    <label class="field"><span>Type *</span><select id="fType">${Object.entries(TYPE_LABELS).map(([k, v]) => `<option value="${k}" ${k === d.type ? 'selected' : ''}>${v}</option>`).join('')}</select></label>
    <div class="form-grid" id="fHostRow">
      <label class="field"><span id="fHostLbl">Host / IP *</span><input id="fHost" value="${esc(d.host)}" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="192.168.1.10 or hostname"></label>
      <label class="field" id="fPortWrap"><span>Port</span><input id="fPort" type="number" inputmode="numeric" value="${d.port || ''}" placeholder="${DEFAULT_PORTS[d.type]}"></label>
    </div>
    <div class="form-grid" id="fS7"><label class="field"><span>Rack</span><input id="fRack" type="number" inputmode="numeric" value="${d.rack ?? 0}"></label>
      <label class="field"><span>Slot</span><input id="fSlot" type="number" inputmode="numeric" value="${d.slot ?? 1}"></label></div>
    <div id="fMb"><label class="field"><span>Unit ID</span><input id="fUnit" type="number" inputmode="numeric" value="${d.unit_id ?? 1}"></label>
      <label class="check">Word swap (low word first)<span class="switch"><input type="checkbox" id="fSwap" ${d.word_swap ? 'checked' : ''}><span></span></span></label></div>
    <fieldset id="fCreds"><legend>🔑 Login (optional)</legend>
      <div class="form-grid">
        <label class="field"><span>Username</span><input id="fUser" value="${esc(o.username)}" autocapitalize="off" autocorrect="off" spellcheck="false" autocomplete="off"></label>
        <label class="field"><span>Password</span><input id="fPass" type="password" autocomplete="new-password" placeholder="${d.has_password ? '•••••• saved — leave blank to keep' : ''}"></label>
      </div>
    </fieldset>
    <div id="fUa" class="form-grid">
      <label class="field"><span>Security policy</span><select id="fPolicy">${OPCUA_POLICIES.map(p => `<option ${p === (o.security_policy || 'None') ? 'selected' : ''}>${p}</option>`).join('')}</select></label>
      <label class="field" id="fModeWrap"><span>Security mode</span><select id="fMode">${['SignAndEncrypt', 'Sign'].map(m => `<option ${m === (o.security_mode || 'SignAndEncrypt') ? 'selected' : ''}>${m}</option>`).join('')}</select></label>
    </div>
    <div id="fMq">
      <label class="check">Use TLS (encrypted)<span class="switch"><input type="checkbox" id="fTls" ${o.tls ? 'checked' : ''}><span></span></span></label>
      <div class="form-grid">
        <label class="field"><span>Client ID</span><input id="fCid" value="${esc(o.client_id)}" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="automatic"></label>
        <label class="field"><span>Offline if no message for (s)</span><input id="fStale" type="number" inputmode="numeric" value="${o.stale_after || ''}" placeholder="300"></label>
      </div>
    </div>
    <p class="muted small" id="fTypeHelp"></p>
    <label class="field"><span>Description</span><input id="fDesc" value="${esc(d.description)}"></label>
    <label class="check">Enabled<span class="switch"><input type="checkbox" id="fEnabled" ${d.enabled ? 'checked' : ''}><span></span></span></label>
    <div class="form-error" id="fErr"></div>
    <div class="sheet-actions">${id ? '<button class="btn danger" id="fDel">Delete</button>' : '<button class="btn ghost" data-close>Cancel</button>'}<button class="btn primary" id="fSave">Save</button></div>`);
  const HELP = {opc_ua: 'Paste the server’s endpoint URL. Tags use NodeIds (find them with UaExpert or the server’s browser).',
    mqtt: 'The dashboard stays subscribed and keeps the latest message per topic. Tags are topics; add a JSON path for payloads like {"temp": 93}.'};
  const sync = () => {
    const t = $('#fType').value;
    $('#fS7').hidden = t !== 'siemens_s7'; $('#fMb').hidden = t !== 'modbus_tcp';
    $('#fCreds').hidden = !(t === 'opc_ua' || t === 'mqtt'); $('#fUa').hidden = t !== 'opc_ua'; $('#fMq').hidden = t !== 'mqtt';
    $('#fModeWrap').hidden = $('#fPolicy').value === 'None';
    $('#fPortWrap').hidden = t === 'opc_ua';
    $('#fHostRow').classList.toggle('one', t === 'opc_ua');
    $('#fHostLbl').textContent = t === 'opc_ua' ? 'Endpoint URL *' : t === 'mqtt' ? 'Broker host / IP *' : 'Host / IP *';
    $('#fHost').placeholder = t === 'opc_ua' ? 'opc.tcp://192.168.1.50:4840' : t === 'mqtt' ? '192.168.1.20 or broker.example.com' : '192.168.1.10 or hostname';
    $('#fPort').placeholder = t === 'mqtt' && $('#fTls').checked ? 8883 : DEFAULT_PORTS[t];
    $('#fTypeHelp').textContent = HELP[t] || ''; $('#fTypeHelp').hidden = !HELP[t];
  };
  $('#fType').onchange = () => { sync(); if (!id) $('#fPort').value = DEFAULT_PORTS[$('#fType').value]; if ($('#fType').value === 'opc_ua' && !id && !$('#fHost').value) $('#fHost').value = 'opc.tcp://'; };
  $('#fPolicy').onchange = sync;
  $('#fTls').onchange = () => { const p = $('#fPort'); if (+p.value === 1883 && $('#fTls').checked) p.value = 8883; else if (+p.value === 8883 && !$('#fTls').checked) p.value = 1883; sync(); };
  sync();
  if (!id) $('#fPort').value = DEFAULT_PORTS[d.type];
  $('#fSave').onclick = async () => {
    const type = $('#fType').value;
    const body = {name: val('fName'), type, host: val('fHost'), port: num('fPort', 0), rack: num('fRack', 0), slot: num('fSlot', 1),
      unit_id: num('fUnit', 1), word_swap: $('#fSwap').checked ? 1 : 0, description: val('fDesc'), enabled: $('#fEnabled').checked};
    if (type === 'opc_ua' || type === 'mqtt') {
      body.options = {username: val('fUser'), password: $('#fPass').value};
      if (type === 'opc_ua') { body.options.security_policy = $('#fPolicy').value; if ($('#fPolicy').value !== 'None') body.options.security_mode = $('#fMode').value; body.port = 0; }
      else { body.options.tls = $('#fTls').checked; body.options.client_id = val('fCid'); body.options.stale_after = num('fStale', null); }
    }
    if (!body.name || !body.host || body.host === 'opc.tcp://') { $('#fErr').textContent = 'Name and ' + (type === 'opc_ua' ? 'endpoint URL' : 'host') + ' are required.'; return; }
    try { await authed(id ? '/api/devices/' + id : '/api/devices', id ? 'PUT' : 'POST', body); closeSheet(); toast('Device saved'); await loadOverview(); loadDevices(); }
    catch (e) { $('#fErr').textContent = e.message; }
  };
  if (id) $('#fDel').onclick = async () => {
    if (!confirm(`Delete "${d.name}" and all its tags? This cannot be undone.`)) return;
    try { await authed('/api/devices/' + id, 'DELETE', {}); closeSheet(); toast('Device deleted'); await loadOverview(); loadDevices(); }
    catch (e) { $('#fErr').textContent = e.message; }
  };
}
const mqttSplit = a => { const s = String(a || ''); const i = s.indexOf('|'); return i < 0 ? [s, ''] : [s.slice(0, i), s.slice(i + 1)]; };
async function tagSheet(devId, tagId) {
  if (!(await requireUnlock())) return;
  const dev = state.devices.find(x => x.id === devId);
  const t = tagId ? dev.tags.find(x => x.id === tagId) : {data_type: 'REAL', enabled: 1, scale_factor: 1, scale_offset: 0, alarm_enabled: 0};
  const esc8 = (ovTag(tagId) || {}).escalating;
  openSheet(tagId ? 'Edit tag' : 'Add tag · ' + dev.name, `
    <label class="field"><span>Name *</span><input id="tName" value="${esc(t.name)}" placeholder="e.g. Pump GPM"></label>
    ${dev.type === 'mqtt' ? `
    <label class="field"><span>Topic *</span><input id="tAddr" class="mono" value="${esc(mqttSplit(t.address)[0])}" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="farm/generator/temp"><small>Exact topic (no + or # wildcards)</small></label>
    <label class="field"><span>JSON path (optional)</span><input id="tPath" class="mono" value="${esc(mqttSplit(t.address)[1])}" autocapitalize="off" autocorrect="off" spellcheck="false" placeholder="temp  or  data.sensors[0].value"><small>Leave blank when the message is just a number, e.g. 93</small></label>` : `
    <label class="field"><span>${dev.type === 'opc_ua' ? 'NodeId *' : 'Address *'}</span><input id="tAddr" class="mono" value="${esc(t.address)}" autocapitalize="${CASE_SENSITIVE[dev.type] ? 'off' : 'characters'}" autocorrect="off" spellcheck="false" placeholder="${esc(ADDR_HINTS[dev.type] || '')}"><small>${esc(ADDR_HINTS[dev.type] || '')}</small></label>`}
    <div class="form-grid">
      <label class="field"><span>Data type</span><select id="tType">${['REAL', 'INT', 'DINT', 'BOOL', 'WORD'].map(x => `<option ${x === t.data_type ? 'selected' : ''}>${x}</option>`).join('')}</select></label>
      <label class="field"><span>Units</span><input id="tUnits" value="${esc(t.units)}" placeholder="GPM, ft, psi, °F"></label>
      <label class="field"><span>Scale factor</span><input id="tSf" type="number" step="any" inputmode="decimal" value="${t.scale_factor ?? 1}"></label>
      <label class="field"><span>Scale offset</span><input id="tSo" type="number" step="any" inputmode="decimal" value="${t.scale_offset ?? 0}"></label>
    </div>
    <p class="muted small" style="margin-top:-4px">Value = raw × factor + offset</p>
    <label class="field"><span>Description</span><input id="tDesc" value="${esc(t.description)}"></label>
    <label class="check">Enabled<span class="switch"><input type="checkbox" id="tEnabled" ${t.enabled ? 'checked' : ''}><span></span></span></label>
    <fieldset><legend>🔔 Alarm</legend>
      <label class="check">Send alerts<span class="switch"><input type="checkbox" id="tAlarm" ${t.alarm_enabled ? 'checked' : ''}><span></span></span></label>
      ${esc8 ? `<p class="muted small">This tag uses the recharge-pump escalation (Alarm → Escalation 1 → Final warning, reply “ack” to silence). Its threshold is set in Settings.</p>` : `
      <div class="form-grid">
        <label class="field"><span>Low alarm (below)</span><input id="tLow" type="number" step="any" inputmode="decimal" value="${t.alarm_low ?? ''}" placeholder="none"></label>
        <label class="field"><span>High alarm (above)</span><input id="tHigh" type="number" step="any" inputmode="decimal" value="${t.alarm_high ?? ''}" placeholder="none"></label>
      </div>
      <label class="field"><span>Custom message (optional)</span><input id="tMsg" value="${esc(t.alarm_message)}" placeholder="Leave blank for automatic text"></label>`}
    </fieldset>
    <div class="form-error" id="tErr"></div>
    <div class="sheet-actions">${tagId ? '<button class="btn danger" id="tDel">Delete</button>' : '<button class="btn ghost" data-close>Cancel</button>'}<button class="btn primary" id="tSave">Save</button></div>`);
  $('#tSave').onclick = async () => {
    let address = val('tAddr');
    if (dev.type === 'mqtt') { if (/[+#]/.test(address)) { $('#tErr').textContent = 'Use an exact topic, without + or # wildcards.'; return; } if (val('tPath')) address += '|' + val('tPath'); }
    const body = {device_id: devId, name: val('tName'), address, data_type: $('#tType').value, units: val('tUnits'), description: val('tDesc'),
      enabled: $('#tEnabled').checked, alarm_enabled: $('#tAlarm').checked ? 1 : 0,
      alarm_low: esc8 ? t.alarm_low : num('tLow', null), alarm_high: esc8 ? t.alarm_high : num('tHigh', null),
      alarm_message: esc8 ? (t.alarm_message || '') : val('tMsg'), scale_factor: num('tSf', 1), scale_offset: num('tSo', 0)};
    if (!body.name || !body.address) { $('#tErr').textContent = 'Name and address are required.'; return; }
    try { await authed(tagId ? '/api/tags/' + tagId : '/api/tags', tagId ? 'PUT' : 'POST', body); closeSheet(); toast('Tag saved'); state.openDevices.add(devId); await loadOverview(); loadDevices(); }
    catch (e) { $('#tErr').textContent = e.message; }
  };
  if (tagId) $('#tDel').onclick = async () => {
    if (!confirm(`Delete tag "${t.name}"? This cannot be undone.`)) return;
    try { await authed('/api/tags/' + tagId, 'DELETE', {}); closeSheet(); toast('Tag deleted'); await loadOverview(); loadDevices(); }
    catch (e) { $('#tErr').textContent = e.message; }
  };
}

// ── settings ───────────────────────────────────────────────────────────────
function renderSettings() {
  renderLock();
  if (!imsgBusy && (!imsgState || Date.now() - (renderSettings._imsgAt || 0) > 15000)) { renderSettings._imsgAt = Date.now(); loadImessage(); }
  if (!tgBusy && (!tgState || Date.now() - (renderSettings._tgAt || 0) > 15000)) { renderSettings._tgAt = Date.now(); loadTelegram(); }
  const ov = state.ov; if (!ov) return;
  // the pump threshold card only matters when an escalating (pump) tag exists - hidden on fresh installs
  $('#thresholdCard').style.display = allTags(ov).some(t => t.escalating) ? '' : 'none';
  const inp = $('#thresholdInput'); if (document.activeElement !== inp) inp.value = ov.threshold_gpm;
  const tags = allTags(ov).filter(t => t.escalating || t.alarm_low != null || t.alarm_high != null || t.alarm_enabled);
  $('#alarmSwitches').innerHTML = tags.map(t => `
    <div class="li"><div class="main"><div class="t">${esc(t.name)}</div><div class="s">${esc(t.device)} · ${t.escalating ? 'below ' + fmtNum(ov.threshold_gpm) + ' ' + esc(t.units) + ' · escalating' : esc(limitsText(t)) || 'no limits set'}</div></div>
    <label class="switch" aria-label="Alarm for ${esc(t.name)}"><input type="checkbox" data-alarm="${t.id}" ${t.alarm_enabled ? 'checked' : ''}><span></span></label></div>`).join('') || '<div class="empty small">No alarms configured.</div>';
  $$('[data-alarm]').forEach(cb => cb.onchange = async () => {
    const on = cb.checked;
    try { await authed(`/api/tags/${cb.dataset.alarm}/alarm`, 'POST', {alarm_enabled: on}); toast(on ? 'Alarm on' : 'Alarm off'); loadOverview(); }
    catch (e) { cb.checked = !on; if (e.message !== 'Locked') toast(e.message); }
  });
  $('#alertLog').innerHTML = (ov.recent_alerts || []).map(a => `
    <div class="li"><div class="main"><div class="s">${fmtTime(a.timestamp)} · ${esc(a.source || '')} · ${esc(a.kind || '')}${a.dry_run ? ' · dry-run' : ''}</div>
    <div class="alert-text">${esc(a.text)}</div><div class="s">Telegram: ${esc(a.telegram || 'pending')} · iMessage: ${esc(a.imessage || 'pending')}</div></div></div>`).join('') || '<div class="empty small">No alerts recorded yet.</div>';
  const p = ov.poller || {};
  const info = [['Version', ov.version], ['Mode', ov.dry_run ? 'Dry run (alerts not sent)' : 'Live'],
    ['Poller', p.alive ? `running (pid ${p.pid})` : p.alive === false ? 'NOT reporting' : 'not started'], ['Heartbeat', p.heartbeat ? ago(p.heartbeat) : '–'],
    ['Poller started', p.started_at ? fmtTime(p.started_at) : '–'], ['Server time', fmtTime(ov.server_time)]];
  const up = ov.uptime || {}, at = Date.now();
  const upRow = (k, secs) => `<dt>${k}</dt><dd class="uptime" data-up="${secs == null ? '' : secs}" data-at="${at}">${fmtUptime(secs)}</dd>`;
  $('#sysInfo').innerHTML = upRow('Mac uptime', up.mac) + upRow('Poller uptime', up.poller) + upRow('Dashboard uptime', up.dashboard)
    + info.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
}

// ── iMessage alarm group ───────────────────────────────────────────────────
let imsgState = null, imsgBusy = false;
async function loadImessage() {
  try { imsgState = await api('/api/settings/imessage'); renderImessage(); } catch (e) { $('#imsgStatus').textContent = 'Could not load: ' + e.message; }
}
function chatStatusHtml(st) {
  const c = st && st.check;
  if (!c) return '<span class="status-chip off">Not checked yet</span>';
  if (c.status === 'pending' || c.status === 'running') return '<span class="status-chip off">Checking…</span>';
  if (c.status === 'expired') return '<span class="status-chip warn">Poller didn’t answer</span>';
  const r = c.result || {};
  if (r.error) return `<span class="status-chip warn">Couldn’t check</span> <span class="muted small">${esc(r.error)}</span>`;
  if (r.found) return `<span class="status-chip ok">✓ Group found in Messages</span> <span class="muted small">${r.participants != null ? r.participants + ' people · ' : ''}${r.last_message_at ? 'last message ' + ago(r.last_message_at) : ''}${r.matches > 1 ? ' · ' + r.matches + ' groups share this name' : ''} · checked ${ago(c.done_at)}</span>`;
  return `<span class="status-chip bad">No group with this name</span> <span class="muted small">Rename the group in Messages to exactly this name, then tap Check. · checked ${ago(c.done_at)}</span>`;
}
function testStatusHtml(st) {
  const t = st && st.test; if (!t) return '';
  if (t.status === 'pending' || t.status === 'running') return 'Test message: sending…';
  if (t.status === 'expired') return 'Test message: the poller didn’t answer.';
  const r = t.result || {};
  if (r.status === 'ok') return `Test message sent ${ago(t.done_at)} ✓`;
  if (r.status === 'dry-run') return `Test message: dry-run mode, not sent (${ago(t.done_at)}).`;
  return `Test message failed ${ago(t.done_at)}: ${esc(r.error || r.status || 'unknown error')}`;
}
function renderImessage() {
  const st = imsgState; if (!st) return;
  const inp = $('#imsgName'); if (document.activeElement !== inp && !imsgBusy) inp.value = st.chat_name;
  $('#imsgStatus').innerHTML = chatStatusHtml(st);
  $('#imsgTest').innerHTML = testStatusHtml(st);
  $('#imsgDefault').hidden = st.is_default;
  $('#imsgDefault').textContent = `Use default (${st.default})`; inp.placeholder = st.default;
}
async function waitRequest(id, ms = 30000) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    await new Promise(r => setTimeout(r, 1500));
    const r = await api('/api/requests/' + id);
    if (!['pending', 'running'].includes(r.status)) return r;
  }
  return null;
}
async function imsgAction(kind) {
  if (imsgBusy) return; imsgBusy = true;
  const btns = ['#imsgSave', '#imsgCheck', '#imsgSend'].map(s => $(s)); btns.forEach(b => b.disabled = true);
  try {
    let r;
    if (kind === 'save') {
      const name = $('#imsgName').value.trim(); if (!name) { toast('Enter the group name'); return; }
      r = await authed('/api/settings/imessage', 'POST', {chat_name: name}); imsgState = r; renderImessage(); toast('Alarm group set to “' + r.chat_name + '”');
    } else if (kind === 'reset') {
      r = await authed('/api/settings/imessage', 'POST', {chat_name: imsgState.default}); imsgState = r; renderImessage(); toast('Back to “' + r.chat_name + '”');
    } else if (kind === 'check') {
      r = await authed('/api/settings/imessage/check', 'POST', {});
    } else {
      if (!confirm(`Send a test message to the “${imsgState.chat_name}” group now?`)) return;
      r = await authed('/api/settings/imessage/test', 'POST', {}); toast('Sending test message…');
    }
    await loadImessage();
    if (r && r.request_id) { await waitRequest(r.request_id); await loadImessage(); }
  } catch (e) { if (e.message !== 'Locked') toast(e.message); }
  finally { imsgBusy = false; btns.forEach(b => b.disabled = false); }
}

// ── Telegram alerts (v2.3) ─────────────────────────────────────────────────
let tgState = null, tgBusy = false;
async function loadTelegram() {
  try { tgState = await api('/api/settings/telegram'); renderTelegram(); } catch (e) { $('#tgSummary').textContent = 'Could not load: ' + e.message; }
}
function renderTelegram() {
  const st = tgState; if (!st) return;
  $('#tgEnabled').checked = st.enabled;
  const src = st.token_source === 'env' ? ' (from the .env file)' : '';
  $('#tgTokenHint').textContent = st.token_set ? `Saved: ${st.token_masked}${src}. Leave blank to keep it.` : 'No token saved yet.';
  $('#tgToken').placeholder = st.token_set ? 'Leave blank to keep the saved token' : 'Paste the token from @BotFather';
  const n = st.chats.length;
  $('#tgSummary').innerHTML = !st.enabled ? '<span class="status-chip off">Off</span> Alarms are not sent to Telegram (iMessage still works).'
    : !st.token_set ? '<span class="status-chip warn">Not set up</span> Follow the steps below.'
    : !n ? '<span class="status-chip warn">No chat</span> Add the group the alarms should go to.'
    : `<span class="status-chip ok">On</span> Alarms go to ${n} chat${n > 1 ? 's' : ''}.`;
  $('#tgHelp').open = !st.token_set || !n;
  $('#tgChats').innerHTML = st.chats.map((c, i) => `<div class="li"><div class="main"><div class="t">${esc(c.title || 'Chat')}</div><div class="s tg-id">${esc(c.id)}</div></div>
    <button class="btn ghost small" data-tg-remove="${i}">Remove</button></div>`).join('')
    || '<div class="empty small">No chats yet.</div>';
  $$('[data-tg-remove]').forEach(b => b.onclick = () => {
    const c = st.chats[+b.dataset.tgRemove];
    if (!confirm(`Stop sending alarms to “${c.title || c.id}”?`)) return;
    tgSave({chats: st.chats.filter((_, i) => i !== +b.dataset.tgRemove)}, 'Chat removed');
  });
}
async function tgSave(body, msg) {
  if (tgBusy) return null; tgBusy = true;
  try { tgState = await authed('/api/settings/telegram', 'POST', body); renderTelegram(); if (msg) toast(msg); return tgState; }
  catch (e) { if (e.message !== 'Locked') toast(e.message, 4000); return null; }
  finally { tgBusy = false; }
}
function tgAddChat(id, title) {
  id = String(id).trim();
  if (!/^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{3,31})$/.test(id)) { toast('Chat IDs are numbers like -1001234567890'); return; }
  if (tgState.chats.some(c => c.id === id)) { toast('Already in the list'); return; }
  return tgSave({chats: tgState.chats.concat([{id, title: title || ''}])}, 'Chat added').then(r => { if (r) { $('#tgChatId').value = ''; renderFound(); } });
}
let tgFound = null;
function renderFound() {
  const f = tgFound, el = $('#tgFound'); if (!f) { el.innerHTML = ''; return; }
  const head = f.bot && f.bot.username ? `<div class="s muted small">Bot: @${esc(f.bot.username)}</div>` : '';
  const rows = (f.chats || []).map((c, i) => {
    const added = tgState && tgState.chats.some(x => x.id === c.id);
    return `<div class="li"><div class="main"><div class="t">${esc(c.title)}</div><div class="s"><span class="tg-id">${esc(c.id)}</span> · ${esc(c.type === 'private' ? 'private chat' : c.type)}</div></div>
      ${added ? '<span class="status-chip ok">Added</span>' : `<button class="btn primary small" data-tg-add="${i}">Add</button>`}</div>`;
  }).join('');
  el.innerHTML = head + (f.message ? `<p class="small">${esc(f.message)}</p>` : '') + rows;
  $$('[data-tg-add]').forEach(b => b.onclick = () => { const c = f.chats[+b.dataset.tgAdd]; tgAddChat(c.id, c.title); });
}
async function tgAction(kind) {
  if (tgBusy) return;
  const btns = ['#tgSaveToken', '#tgFind', '#tgTest', '#tgAddChat'].map(s => $(s));
  try {
    if (kind === 'token') {
      const t = $('#tgToken').value.trim(); if (!t) { toast('Paste the bot token first'); return; }
      if (await tgSave({token: t}, 'Bot token saved')) $('#tgToken').value = '';
      return;
    }
    tgBusy = true; btns.forEach(b => b.disabled = true);
    if (kind === 'find') {
      $('#tgFound').innerHTML = '<p class="small muted">Asking Telegram…</p>';
      try { tgFound = await authed('/api/settings/telegram/find', 'POST', {token: $('#tgToken').value.trim()}); }
      catch (e) { tgFound = e.message === 'Locked' ? null : {chats: [], message: e.message}; }
      renderFound();
    } else if (kind === 'test') {
      const n = tgState.chats.length;
      if (!confirm(`Send a test message to ${n} Telegram chat${n === 1 ? '' : 's'} now?`)) return;
      $('#tgResult').textContent = 'Sending…';
      try {
        const r = await authed('/api/settings/telegram/test', 'POST', {});
        $('#tgResult').innerHTML = r.results.map(x => `${esc(x.title || x.id)}: ${x.ok ? 'sent ✓' : 'failed: ' + esc(x.error)}`).join('<br>');
      } catch (e) { $('#tgResult').textContent = e.message === 'Locked' ? '' : e.message; }
    }
  } finally { tgBusy = false; btns.forEach(b => b.disabled = false); }
}

function fmtUptime(secs) {
  if (secs == null || secs === '' || isNaN(secs)) return '–';
  secs = Math.max(0, Math.floor(secs));
  const d = Math.floor(secs / 86400), h = Math.floor(secs % 86400 / 3600), m = Math.floor(secs % 3600 / 60), s = secs % 60;
  const pad = n => String(n).padStart(2, '0');
  return (d ? d + 'd ' : '') + pad(h) + ':' + pad(m) + ':' + pad(s);
}
function tickUptimes() {
  $$('#sysInfo .uptime').forEach(el => {
    if (el.dataset.up === '') return;
    el.textContent = fmtUptime(+el.dataset.up + (Date.now() - +el.dataset.at) / 1000);
  });
}

// ── wiring ─────────────────────────────────────────────────────────────────
function init() {
  $$('.tabbar button').forEach(b => b.onclick = () => show(b.dataset.view));
  $('#sheetClose').onclick = closeSheet; $('#sheetBackdrop').onclick = closeSheet;
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && !$('#sheet').hidden) closeSheet(); });
  const lock = () => { if (auth.unlocked) { auth.pw = null; toast('Locked'); } else requireUnlock(); };
  $('#lockBtn').onclick = lock; $('#unlockBtn2').onclick = lock;
  $$('#rangeSeg button').forEach(b => b.onclick = () => {
    $$('#rangeSeg button').forEach(x => x.classList.toggle('active', x === b));
    if (b.dataset.h === 'custom') { $('#customRange').hidden = false; return; }
    $('#customRange').hidden = true; state.custom = null; state.hours = +b.dataset.h; loadTrend();
  });
  $('#applyRange').onclick = () => { const s = $('#startTime').value, e = $('#endTime').value; if (!s || !e) { toast('Pick a start and end time'); return; } state.custom = [s, e]; loadTrend(); };
  $('#trendTag').onchange = e => { state.trendTag = +e.target.value; state.trendTag2 = ''; const t = ovTag(state.trendTag); if (!(t && t.escalating)) state.trendTag2 = '0'; loadTrend(); };
  $('#trendTag2').onchange = e => { state.trendTag2 = e.target.value || '0'; loadTrend(); };
  $('#resetZoom').onclick = () => state.chart && state.chart.resetZoom();
  $$('[data-step]').forEach(b => b.onclick = () => { const i = $('#thresholdInput'); i.value = Math.max(0, (+i.value || 0) + +b.dataset.step); });
  $('#saveThreshold').onclick = async () => {
    const v = parseInt($('#thresholdInput').value, 10); if (isNaN(v) || v < 0) { toast('Enter a valid number'); return; }
    try { const r = await authed('/api/threshold', 'POST', {threshold_gpm: v}); toast('Threshold set to ' + fmtNum(r.threshold_gpm) + ' GPM'); loadOverview(); }
    catch (e) { if (e.message !== 'Locked') toast(e.message); }
  };
  $('#addDeviceBtn').onclick = () => deviceSheet(null);     // was never wired up before v2.2
  $('#imsgSave').onclick = () => imsgAction('save'); $('#imsgCheck').onclick = () => imsgAction('check');
  $('#imsgSend').onclick = () => imsgAction('test'); $('#imsgDefault').onclick = () => imsgAction('reset');
  $('#tgSaveToken').onclick = () => tgAction('token'); $('#tgFind').onclick = () => tgAction('find');
  $('#tgTest').onclick = () => tgAction('test');
  $('#tgAddChat').onclick = () => tgAddChat($('#tgChatId').value);
  $('#tgChatId').onkeydown = e => { if (e.key === 'Enter') tgAddChat($('#tgChatId').value); };
  $('#tgEnabled').onchange = async () => {
    const want = $('#tgEnabled').checked;
    if (!(await tgSave({enabled: want}, want ? 'Telegram alerts on' : 'Telegram alerts off'))) $('#tgEnabled').checked = !want;
  };
  renderLock();
  const start = (location.hash || '').slice(1);
  loadOverview().then(() => show(['overview', 'trends', 'devices', 'settings'].includes(start) ? start : 'overview'));
  setInterval(() => { if (document.visibilityState === 'visible') { loadOverview().then(() => { if (state.view === 'devices' && $('#sheet').hidden) renderDevices(); }); } }, 15000);
  setInterval(() => { if (state.view === 'settings') tickUptimes(); }, 1000);
  setInterval(() => { if (document.visibilityState === 'visible' && state.view === 'trends' && !state.custom) loadTrend(); }, 60000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') loadOverview(); });
  window.addEventListener('hashchange', () => { const v = location.hash.slice(1); if (['overview', 'trends', 'devices', 'settings'].includes(v) && v !== state.view) show(v); });
}
document.addEventListener('DOMContentLoaded', init);
})();
