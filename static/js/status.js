// Sidebar status: status cards, alert banner, gain-apply button, view toggle.

// ── Status cards ───────────────────────────────────────────────────────────
// The heart of the sidebar. makeCard(f) builds one feeder/service card's HTML
// from an /api/status entry (badge, last-seen, running-for, uptime bars, logs
// button); uptimePctClass colors the percentage. The load() loop in init()
// calls these to (re)render the list each poll.
// ───────────────────────────────────────────────────────────────────────────

const CORE_KEYS = ['airspy_adsb','readsb','dump978-fa','tar1090'];

function uptimePctClass(pct) {
  if (pct === null) return '';
  if (pct >= 99) return 'good';
  if (pct >= 95) return 'warn';
  return 'bad';
}

function makeCard(f) {
  // Running but no data for dataflow.STALE_AFTER_S -> amber "no data" instead of green.
  const noData = f.status==='ok' && f.data && f.data.state==='stale';
  const bc = noData?'badge-warn':f.status==='ok'?'badge-ok':f.status==='error'?'badge-error':'badge-checking';
  const bt = noData?'no data':f.status==='ok'?'running':f.status==='error'?'stopped':'checking';
  const flowTip = f.data && f.data.detail ? f.data.detail : 'Last feed activity';
  let hint = f.hint||'';
  if (f.key==='tar1090') hint='Live map embedded';
  let ver='', uptime='', lastSeen='';
  if (CORE_KEYS.includes(f.key)) {  // version only matters for core services, not feeders
    if (f.version) {
      const vs = f.version.startsWith('v')?f.version:'v'+f.version;
      const upd = f.outdated?`<span class="update-badge" title="Latest: ${esc(f.latest)}"><i class="ti ti-arrow-up" aria-hidden="true"></i> update</span>`:'';
      ver=`<span class="version-tag">${esc(vs)}</span>${upd}`;
    } else if (f.version === null) {
      ver=`<span class="version-tag" style="color:var(--text3)">checking...</span>`;
    }
  }
  if (f.uptime_str) uptime=`<span class="uptime-tag" title="Running for"><i class="ti ti-clock" style="font-size:10px" aria-hidden="true"></i> ${esc(f.uptime_str)}</span>`;
  if (f.uptime_pct !== null && f.uptime_pct !== undefined) {
    const cls = uptimePctClass(f.uptime_pct);
    uptime += `<span class="uptime-pct ${cls}" title="7-day uptime">${esc(f.uptime_pct)}%</span>`;
  }
  if (f.last_seen) {
    const live = f.last_seen === 'live';
    const cls = live?' live':f.last_seen==='no data'?' nodata':'';
    lastSeen = `<span class="last-seen${cls}" title="${esc(flowTip)}">${live?'<i class="ti ti-point-filled" aria-hidden="true"></i> live':esc(f.last_seen)}</span>`;
  }
  // MLAT sync for a feeder whose MLAT client has no card of its own (dataflow.observe_mlat)
  const m = f.data && f.data.mlat;
  if (m) lastSeen += `<span class="mlat-tag${m.ok?' ok':' bad'}" title="${esc(m.detail)}">MLAT${m.ok?'':' !'}</span>`;
  // Compact uptime bars (7 days)
  let uptimeBars = '';
  if (f.uptime_bars && f.uptime_bars.length) {
    const barHtml = f.uptime_bars.map((pct, i) => {
      const cls = pct === null ? 'cub-unknown' : pct >= 95 ? 'cub-up' : 'cub-down';
      const day = new Date(Date.now() - (6-i)*86400000).toLocaleDateString('en-US',{month:'numeric',day:'numeric'});
      const tip = pct !== null ? `${day}: ${pct}% uptime` : `${day}: no data`;
      return `<div class="cub ${cls}" title="${esc(tip)}"></div>`;
    }).join('');
    const known = f.uptime_bars.filter(p => p !== null);
    const good  = known.filter(p => p >= 95).length;
    const summary = known.length
      ? `7-day uptime: ${good} of ${known.length} days at or above 95%`
      : '7-day uptime: no data recorded';
    uptimeBars = `<div class="card-uptime-bars" role="img" aria-label="${summary}">${barHtml}</div>`;
  }

  return `<div class="card">
    <div class="card-top"><span class="card-name"><i class="ti ti-${esc(f.icon||'plug')}" aria-hidden="true"></i>${esc(f.label)}</span><span class="badge ${bc}"${noData?` title="${esc(flowTip)}"`:''}>${bt}</span></div>
    <div class="card-hint">${esc(hint)}</div>
    <div class="card-footer">
      <div class="card-meta">${ver}${uptime}${lastSeen}</div>
      <div class="card-actions">
        ${IS_READONLY?'':`<button class="action-btn" data-click="openLogs" data-key="${esc(f.key)}" data-label="${esc(f.label)}"><i class="ti ti-terminal-2" aria-hidden="true"></i> logs</button>`}
      </div>
    </div>
    ${uptimeBars}
  </div>`;
}

let sparkAcData=[], sparkMsgData=[];

let lastGoodPoll = null;

// Single polite status region for the whole dashboard. Metrics update on a 30s
// timer; without this a screen-reader user has no way to know anything changed.
function announce(msg) {
  const el=document.getElementById('sr-status');
  if (el) el.textContent=msg;
}

async function load() {
  document.getElementById('ri').classList.add('spinning');
  try {
    const d = await fetch('/api/status').then(r=>r.json());
    document.getElementById('m-ac').textContent  = d.metrics.aircraft;
    document.getElementById('m-msg').textContent = d.metrics.msg_rate;
    document.getElementById('m-rng').textContent = d.metrics.max_range_nm||'—';
    document.getElementById('m-rng-sub').textContent =
      d.metrics.max_range_15m_nm ? `nm · 15-min max ${d.metrics.max_range_15m_nm}` : 'nautical miles';
    const core = d.feeders.filter(f=> CORE_KEYS.includes(f.key));
    const feed = d.feeders.filter(f=>!CORE_KEYS.includes(f.key));
    document.getElementById('core-cards').innerHTML   = core.map(makeCard).join('');
    document.getElementById('feeder-cards').innerHTML = feed.map(makeCard).join('');
    document.getElementById('m-feed').textContent     = feed.filter(f=>f.status==='ok').length;
    document.getElementById('m-feed-sub').textContent = `of ${feed.length}`;
    buildRestartButtons(d.feeders);
    announce(`${d.metrics.aircraft} aircraft, ${d.metrics.msg_rate} messages per second, `
           + `${feed.filter(f=>f.status==='ok').length} of ${feed.length} feeders up.`);
    lastGoodPoll = new Date();
    document.getElementById('ts').textContent='Updated '+lastGoodPoll.toLocaleTimeString();
    document.getElementById('ts').classList.remove('stale');
  } catch {
    document.getElementById('core-cards').innerHTML='<div class="error-msg">Cannot reach /api/status</div>';
    const ts=document.getElementById('ts');
    ts.textContent = lastGoodPoll
      ? 'Stale — last update '+lastGoodPoll.toLocaleTimeString()
      : 'Stale — no successful update yet';
    ts.classList.add('stale');
  }
  document.getElementById('ri').classList.remove('spinning');

  // Alerts
  checkAlerts();
  // Sparklines
  loadSparklines();
  // System stats
  loadSystemStats();
  // Auto-tune state first, so the signal panel knows whether to offer Apply
  await loadAutotune();
  // Signal stats
  loadSignalStats();
}

async function loadSparklines() {
  try {
    const hist = await fetch('/api/stats/history?minutes=60').then(r=>r.json());
    sparkAcData  = hist.map(h=>h.aircraft);
    sparkMsgData = hist.map(h=>h.msg_rate);
    drawSparkline('spark-ac',  sparkAcData,  '#4ade80');
    drawSparkline('spark-msg', sparkMsgData, '#60a5fa');
  } catch {}
}

async function loadSystemStats() {
  try {
    const s = await fetch('/api/stats/system').then(r=>r.json());
    const cpuEl  = document.getElementById('sys-cpu');
    const tempEl = document.getElementById('sys-temp');
    const memEl  = document.getElementById('sys-mem');
    const diskEl = document.getElementById('sys-disk');
    cpuEl.textContent  = s.cpu_pct!==undefined  ? s.cpu_pct+'%'  : '—';
    tempEl.textContent = s.cpu_temp!==undefined ? s.cpu_temp+'°' : '—';
    memEl.textContent  = s.mem_pct!==undefined   ? s.mem_pct+'%'  : '—';
    diskEl.textContent = s.disk_pct!==undefined  ? s.disk_pct+'%' : '—';
    cpuEl.className  = 'sys-stat-value' + (s.cpu_pct>80?' bad':s.cpu_pct>60?' warn':'');
    tempEl.className = 'sys-stat-value' + (s.cpu_temp>75?' bad':s.cpu_temp>65?' warn':'');
    memEl.className  = 'sys-stat-value' + (s.mem_pct>85?' bad':s.mem_pct>70?' warn':'');
    diskEl.className = 'sys-stat-value' + (s.disk_pct>90?' bad':s.disk_pct>75?' warn':'');
  } catch {}
}

function toggleEl(id, show){ const e=document.getElementById(id); if(e) e.classList.toggle('hidden', !show); }

function setBar(id, val, lo, hi, unit, fixed){
  const t=document.getElementById(id), b=document.getElementById(id+'-bar');
  if(val===null||val===undefined){ if(t)t.textContent='—'; if(b)b.style.width='0%'; return; }
  if(t)t.textContent=val.toFixed(fixed)+unit;
  if(b)b.style.width=Math.max(0,Math.min(100,(val-lo)/(hi-lo)*100))+'%';
}

function fillBand(p, d){
  const dot=document.getElementById(p+'-dot');
  if(dot) dot.className='band-dot '+(d.active?'up':'down');
  document.getElementById(p+'-ac').textContent  = (d.aircraft ?? '—');
  document.getElementById(p+'-msg').textContent = (d.msg_rate ?? '—');
  setBar(p+'-rssi', d.rssi, -30, 0, ' dBFS', 1);      // dBFS: -30 weak .. 0 max
  if('snr'   in d) setBar(p+'-snr',   d.snr,   0,  40, ' dB',   1);
  if('noise' in d) setBar(p+'-noise', d.noise, -50, -15, ' dBFS', 1);
}

// Dispatcher: show only the bands/decoders actually present. Airspy panel and its
// settings card appear only when an Airspy is installed; 1090/978 blocks per band.
async function loadSignalStats() {
  let bands;
  try { bands = await fetch('/api/stats/bands').then(r=>r.json()); } catch { return; }
  toggleEl('signal-panel', bands.has_airspy);
  toggleEl('airspy-card',  bands.has_airspy);
  if (bands.has_airspy) loadAirspySignal();
  toggleEl('panel-1090', !!bands.b1090); if (bands.b1090) fillBand('b1090', bands.b1090);
  toggleEl('panel-978',  !!bands.b978);  if (bands.b978)  fillBand('b978',  bands.b978);
  toggleEl('sdr1090-card', bands.has_rtl1090);  // settings cards: only for present SDRs
  toggleEl('sdr978-card',  bands.has_978);
}

async function loadAirspySignal() {
  try {
    const d = await fetch('/api/stats/airspy').then(r=>r.json());
    const s = d.stats;
    if (!s || !s.rssi) return;
    const rssiMed  = s.rssi.median;
    const snrMed   = s.snr.median;
    const noiseMed = s.noise.median;
    document.getElementById('sig-rssi').textContent  = rssiMed.toFixed(0)+' dB';
    document.getElementById('sig-snr').textContent   = snrMed.toFixed(1)+' dB';
    document.getElementById('sig-noise').textContent = noiseMed.toFixed(0)+' dB';
    // Bars: RSSI 0-90dB range, SNR 0-30dB, noise 0-70dB
    document.getElementById('sig-rssi-bar').style.width  = Math.min(100, rssiMed/90*100)+'%';
    document.getElementById('sig-snr-bar').style.width   = Math.min(100, snrMed/30*100)+'%';
    document.getElementById('sig-noise-bar').style.width = Math.min(100, noiseMed/70*100)+'%';
    // Gain recommendation
    const rec = d.recommendation;
    if (rec) {
      const el  = document.getElementById('gain-rec');
      const btn = document.getElementById('gain-apply-btn');
      el.className = `gain-rec ${rec.action}`;
      el.innerHTML = `<i class="ti ti-${rec.action==='ok'?'check':'alert-triangle'}" aria-hidden="true"></i> ${esc(rec.reason)}`;
      el.classList.remove('hidden');
      if (rec.action !== 'ok' && _autotuneEnabled) {
        _gainRecommendedValue = null;
        btn.classList.add('hidden');
        el.append(' (auto-tune is on)');
      } else if (rec.action !== 'ok') {
        // Extract suggested gain from reason string e.g. "try gain 19"
        const m = rec.reason.match(/try gain (\d+)/);
        if (m) {
          _gainRecommendedValue = parseInt(m[1]);
          btn.classList.remove('hidden');
          btn.textContent = '';
          btn.innerHTML = `<i class="ti ti-wand" aria-hidden="true"></i> Apply: set gain to ${esc(_gainRecommendedValue)}`;
        }
      } else {
        _gainRecommendedValue = null;
        btn.classList.add('hidden');
      }
    }
  } catch {}
}

// ── Alert banner ───────────────────────────────────────────────────────────
// Polls /api/alerts and shows/hides the top banner listing any service currently
// down, and any running feeder that has sent no data for 5 min (data_alerts),
// so problems are visible without scanning every card.
// ───────────────────────────────────────────────────────────────────────────

async function checkAlerts() {
  try {
    const d = await fetch('/api/alerts').then(r=>r.json());
    const banner = document.getElementById('alert-banner');
    const items  = document.getElementById('alert-items');
    const parts = [];
    if (d.alerts && d.alerts.length)
      parts.push('Service down: ' + d.alerts.map(a=>`${a.label} (${a.detail})`).join(', '));
    if (d.data_alerts && d.data_alerts.length)
      parts.push('No data: ' + d.data_alerts.map(a=>`${a.label} (${a.detail})`).join(', '));
    if (parts.length) {
      items.textContent = parts.join(' · ');
      banner.classList.add('visible');
    } else {
      banner.classList.remove('visible');
    }
  } catch {}
}

// ── Gain apply button ──────────────────────────────────────────────────────
// The "Apply: set gain to N" shortcut shown with the airspy recommendation —
// posts the suggested gain to the settings API and restarts airspy_adsb.
// ───────────────────────────────────────────────────────────────────────────

let _gainRecommendedValue = null;

async function applyRecommendedGain() {
  if (!_gainRecommendedValue) return;
  const btn = document.getElementById('gain-apply-btn');
  btn.disabled = true;
  try {
    // Read current settings first
    const cur = await fetch('/api/settings/airspy').then(r=>r.json());
    if (!cur.ok) { btn.disabled=false; return; }
    const payload = { ...cur.settings, gain: String(_gainRecommendedValue) };
    const r = await postAirspySettings(payload);
    if (r.ok) {
      document.getElementById('gain-rec').textContent = 'Gain updated — restarting airspy_adsb and checking it comes back…';
      setTimeout(loadSignalStats, 5000);
    }
    if (!r.cancelled) followCheck('airspy-toast', r);   // reports a rollback if airspy_adsb doesn't come back
  } catch {}
  btn.disabled = false;
}

// ── View toggle ────────────────────────────────────────────────────────────
// Switches the main area between Map / Graphs / Settings. showView hides the
// others, marks the active nav button, lazy-loads Settings on first open, and
// opens/closes the system-log stream as you enter/leave Settings.
// ───────────────────────────────────────────────────────────────────────────

const VIEWS = ['map','graphs','settings'];
const TLABELS = {
  map:      '<i class="ti ti-map-pin" aria-hidden="true"></i> <span>tar1090 live map</span>',
  graphs:   '<i class="ti ti-chart-line" aria-hidden="true"></i> <span>Performance graphs</span>',
  settings: '<i class="ti ti-settings" aria-hidden="true"></i> <span>Settings</span>',
};
function showView(v) {
  if (v==='settings' && IS_READONLY) return;
  VIEWS.forEach(id => document.getElementById('view-'+id).classList.toggle('hidden', id!==v));
  document.querySelectorAll('.vbtn').forEach(b=>b.classList.remove('active'));
  document.getElementById('btn-'+v).classList.add('active');
  document.getElementById('toolbar-label').innerHTML=TLABELS[v]||'';
  if (v==='settings') { loadSettings(); openSysLog(); }
  else closeSysLog();
}
