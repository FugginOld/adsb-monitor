// ── Timeline view ──────────────────────────────────────────────────────────
// "Did that gain step help?" at a glance: messages per second, max range, the
// Airspy gain and its strong-signal % on one shared time axis, one small
// panel each (never two scales on one panel). Gain steps are drawn as vertical
// markers through every panel; restarts, config saves and stack updates sit in
// an events lane below. A crosshair + tooltip reads all four at once, and a
// table view holds the same numbers. Below the chart, "what changed" lists
// everything from /api/timeline's merged feed. Data: /api/timeline?hours=N.
// ───────────────────────────────────────────────────────────────────────────

let TL_HOURS = 24, TL_TABLE = false, TL_DATA = null, TL_TIMER = null, TL_THRESH = null;

const TL_PANELS = [
  {key: 'msg_rate',     title: 'Messages / s',        fmt: v => Math.round(v).toLocaleString(), zero: true},
  {key: 'max_range_nm', title: 'Max range (nm)',      fmt: v => Math.round(v) + ' nm'},
  {key: 'gain',         title: 'Airspy gain',         fmt: v => String(Math.round(v)),           step: true},
  {key: 'strong_pct',   title: 'Strong signals (%)',  fmt: v => v.toFixed(1) + ' %',             zero: true, thresh: true},
];
const TL_KIND = {
  gain:    {icon: 'ti-adjustments', label: 'Gain'},
  restart: {icon: 'ti-refresh',     label: 'Restart'},
  config:  {icon: 'ti-settings',    label: 'Config'},
  update:  {icon: 'ti-download',    label: 'Update'},
  state:   {icon: 'ti-heartbeat',   label: 'Service'},
  data:    {icon: 'ti-antenna',     label: 'Data flow'},
};
const TL_LEVEL = {ok: {icon: 'ti-check', label: 'ok'}, warn: {icon: 'ti-alert-triangle', label: 'warning'},
                  error: {icon: 'ti-alert-octagon', label: 'problem'}};

async function loadTimeline() {
  const box = document.getElementById('tl-chart');
  try {
    const [d, at] = await Promise.all([
      fetch('/api/timeline?hours=' + TL_HOURS).then(r => r.json()),
      TL_THRESH ? null : fetch('/api/autotune').then(r => r.json()).catch(() => null),
    ]);
    if (at && at.config) TL_THRESH = {high: at.config.strong_high_pct, low: at.config.strong_low_pct};
    TL_DATA = d;
    renderTimeline();
  } catch { if (box) box.textContent = 'Could not load the timeline.'; }
}

function openTimeline() {
  loadTimeline();
  clearInterval(TL_TIMER);
  TL_TIMER = setInterval(loadTimeline, 60000);
}
function closeTimeline() { clearInterval(TL_TIMER); TL_TIMER = null; }

function setTimelineHours(el) {
  TL_HOURS = Number(el.dataset.hours) || 24;
  document.querySelectorAll('[data-click="setTimelineHours"]').forEach(b =>
    b.setAttribute('aria-pressed', String(b === el)));
  loadTimeline();
}
function toggleTimelineTable(el) {
  TL_TABLE = !TL_TABLE;
  el.setAttribute('aria-pressed', String(TL_TABLE));
  el.querySelector('span').textContent = TL_TABLE ? 'Chart' : 'Table';
  renderTimeline();
}

function tlTime(ts, withDay) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
  if (!withDay) return t;
  const today = new Date().toDateString() === d.toDateString();
  return (today ? '' : d.toLocaleDateString([], {weekday: 'short'}) + ' ') + t;
}

function renderTimeline() {
  if (!TL_DATA) return;
  renderTimelineLinks(TL_DATA.links || {});
  renderChanges(TL_DATA.changes || []);
  const box = document.getElementById('tl-chart');
  const rows = TL_DATA.metrics || [];
  if (!rows.length) { box.innerHTML = '<p class="field-hint">No samples in this window yet.</p>'; return; }
  box.innerHTML = TL_TABLE ? timelineTable(rows) : timelineSvg(rows, box.clientWidth || 800);
  if (!TL_TABLE) wireCrosshair(box, rows);
}

function niceTicks(min, max) {
  if (min === max) { max = min + 1; }
  const span = max - min, raw = span / 2;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw) || raw;
  const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
  const out = [];
  for (let v = lo; v <= hi + step / 2; v += step) out.push(+v.toFixed(6));
  return out;
}

// Room a gain-step label ("15→14", 10 px font) and a threshold label
// ("raise gain < 0.5 %") need so neighbours don't overlap.
const TL_MARKER_LABEL_GAP = 34;
const TL_THRESH_LABEL_W = 110;

// Geometry shared by the chart and its crosshair.
function tlGeom(width) {
  const left = 52, right = 14, panelH = 64, gap = 26, top = 22, lane = 18;
  const n = TL_PANELS.length;
  const lanesTop = top + n * (panelH + gap) - gap + 12;
  return {left, right, panelH, gap, top, lane, lanesTop, width,
          height: lanesTop + lane + 26, plotW: Math.max(120, width - left - right)};
}

function timelineSvg(rows, width) {
  const g = tlGeom(width);
  const end = TL_DATA.metrics.length ? Math.max(Date.now() / 1000, rows[rows.length - 1].ts) : Date.now() / 1000;
  const start = end - TL_DATA.hours * 3600;
  const x = ts => g.left + (ts - start) / (end - start) * g.plotW;
  const maxGap = (TL_DATA.bucket_s || 300) * 2.5;
  let svg = `<svg class="tl-svg" width="${g.width}" height="${g.height}" viewBox="0 0 ${g.width} ${g.height}" role="img" aria-label="Messages per second, max range, Airspy gain and strong-signal share over the last ${TL_DATA.hours} hours, with gain changes marked">`;

  TL_PANELS.forEach((p, i) => {
    const y0 = g.top + i * (g.panelH + g.gap);
    const vals = rows.map(r => r[p.key]).filter(v => v != null);
    svg += `<text class="tl-title" x="${g.left}" y="${y0 - 7}">${esc(p.title)}</text>`;
    if (!vals.length) {
      svg += `<text class="tl-empty" x="${g.left + 8}" y="${y0 + g.panelH / 2}">no data in this window</text>`;
      return;
    }
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (p.zero) lo = Math.min(0, lo);
    if (p.thresh && TL_THRESH) hi = Math.max(hi, TL_THRESH.high * 1.2);
    if (p.step) { lo = Math.floor(lo) - 1; hi = Math.ceil(hi) + 1; }
    const ticks = niceTicks(lo, hi);
    lo = ticks[0]; hi = ticks[ticks.length - 1];
    const y = v => y0 + g.panelH - (v - lo) / (hi - lo || 1) * g.panelH;
    ticks.forEach(t => {
      svg += `<line class="tl-grid" x1="${g.left}" x2="${g.left + g.plotW}" y1="${y(t)}" y2="${y(t)}"/>`;
      svg += `<text class="tl-axis" x="${g.left - 6}" y="${y(t) + 3}" text-anchor="end">${esc(p.step ? String(t) : (Math.abs(t) >= 1000 ? (t / 1000) + 'k' : String(t)))}</text>`;
    });
    if (p.thresh && TL_THRESH) {
      // Both labels sit at the right end; when the two lines are close (a 5 % and a
      // 0.5 % line on a 0–100 scale) the second label moves left of the first.
      let labelEnd = g.left + g.plotW - 4, prevY = null;
      [['high', 'lower gain >'], ['low', 'raise gain <']].forEach(([k, lbl]) => {
        const v = TL_THRESH[k];
        if (v == null || v < lo || v > hi) return;
        const text = `${lbl} ${v} %`;
        if (prevY != null && Math.abs(y(v) - prevY) < 12) labelEnd -= TL_THRESH_LABEL_W;
        prevY = y(v);
        svg += `<line class="tl-thresh" x1="${g.left}" x2="${g.left + g.plotW}" y1="${y(v)}" y2="${y(v)}"/>`;
        svg += `<text class="tl-axis tl-thresh-label" x="${labelEnd}" y="${y(v) - 3}" text-anchor="end">${esc(text)}</text>`;
      });
    }
    // the line, broken where samples are missing
    let d = '', prev = null;
    rows.forEach(r => {
      const v = r[p.key];
      if (v == null) { prev = null; return; }
      const px = x(r.ts).toFixed(1), py = y(v).toFixed(1);
      if (!prev || r.ts - prev.ts > maxGap) d += `M${px},${py}`;
      else if (p.step) d += `H${px}V${py}`;
      else d += `L${px},${py}`;
      prev = r;
    });
    svg += `<path class="tl-line" d="${d}"/>`;
  });

  // gain steps through every panel
  const panelsBottom = g.top + TL_PANELS.length * (g.panelH + g.gap) - g.gap;
  // Every step gets its line; a label is drawn only where it clears the previous
  // one, so a burst of auto-tune steps doesn't print on top of itself (the
  // crosshair tooltip and the list below still give every step).
  let lastLabelX = -Infinity;
  (TL_DATA.changes || []).filter(c => c.kind === 'gain' && c.ts >= start)
    .sort((a, b) => a.ts - b.ts).forEach(c => {
      const cx = x(c.ts), step = `${c.old}→${c.new}`;
      svg += `<line class="tl-marker${c.level === 'warn' ? ' warn' : ''}" x1="${cx}" x2="${cx}" y1="${g.top - 4}" y2="${panelsBottom}"/>`;
      if (cx - lastLabelX < TL_MARKER_LABEL_GAP) return;
      lastLabelX = cx;
      svg += `<text class="tl-marker-label" x="${cx + 3}" y="${g.top + 9}">${esc(step)}</text>`;
    });

  // events lane: restarts, config, updates, service and data-flow changes
  svg += `<text class="tl-title" x="${g.left}" y="${g.lanesTop - 2}">Other changes</text>`;
  svg += `<line class="tl-grid" x1="${g.left}" x2="${g.left + g.plotW}" y1="${g.lanesTop + g.lane / 2 + 4}" y2="${g.lanesTop + g.lane / 2 + 4}"/>`;
  (TL_DATA.changes || []).filter(c => c.kind !== 'gain' && c.ts >= start).forEach(c => {
    const cx = x(c.ts), cy = g.lanesTop + g.lane / 2 + 4;
    svg += `<circle class="tl-event ${esc(c.level || 'info')}" cx="${cx}" cy="${cy}" r="4"><title>${esc(tlTime(c.ts, true) + ' · ' + (TL_KIND[c.kind] || {label: c.kind}).label + ' · ' + c.subject + ': ' + c.detail)}</title></circle>`;
  });

  // time axis
  // label every N hours, N chosen so labels stay >= ~70 px apart at this width
  const stepH = [1, 2, 3, 4, 6, 8, 12].find(h => g.plotW / (TL_DATA.hours / h) >= 70) || 12;
  const first = Math.ceil(start / (stepH * 3600)) * stepH * 3600;
  const axisY = g.lanesTop + g.lane + 18;
  for (let t = first; t <= end; t += stepH * 3600) {
    const tx = x(t), anchor = tx > g.left + g.plotW - 30 ? 'end' : tx < g.left + 30 ? 'start' : 'middle';
    svg += `<text class="tl-axis" x="${tx}" y="${axisY}" text-anchor="${anchor}">${esc(tlTime(t))}</text>`;
  }
  svg += `<line class="tl-cross hidden" id="tl-cross" x1="0" x2="0" y1="${g.top - 4}" y2="${g.lanesTop + g.lane}"/>`;
  svg += `<rect class="tl-hit" x="${g.left}" y="0" width="${g.plotW}" height="${g.height}"/>`;
  return svg + '</svg><div class="tl-tip hidden" id="tl-tip" role="status" aria-live="polite"></div>';
}

function wireCrosshair(box, rows) {
  const svg = box.querySelector('svg'), hit = box.querySelector('.tl-hit');
  const cross = document.getElementById('tl-cross'), tip = document.getElementById('tl-tip');
  if (!svg || !hit) return;
  const g = tlGeom(box.clientWidth || 800);
  const end = Math.max(Date.now() / 1000, rows[rows.length - 1].ts), start = end - TL_DATA.hours * 3600;
  const move = ev => {
    const rect = svg.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const ts = start + (px - g.left) / g.plotW * (end - start);
    let best = rows[0];
    rows.forEach(r => { if (Math.abs(r.ts - ts) < Math.abs(best.ts - ts)) best = r; });
    const bx = g.left + (best.ts - start) / (end - start) * g.plotW;
    cross.setAttribute('x1', bx); cross.setAttribute('x2', bx);
    cross.classList.remove('hidden');
    tip.innerHTML = `<b>${esc(tlTime(best.ts, true))}</b>` + TL_PANELS.map(p =>
      `<div><span>${esc(p.title)}</span><span>${best[p.key] == null ? '—' : esc(p.fmt(best[p.key]))}</span></div>`).join('');
    tip.classList.remove('hidden');
    const left = Math.min(bx + 12, rect.width - tip.offsetWidth - 4);
    tip.style.left = Math.max(4, left) + 'px';
    tip.style.top = (g.top + 4) + 'px';
  };
  const leave = () => { cross.classList.add('hidden'); tip.classList.add('hidden'); };
  hit.addEventListener('pointermove', move);
  hit.addEventListener('pointerdown', move);
  hit.addEventListener('pointerleave', leave);
}

function timelineTable(rows) {
  const head = '<tr><th scope="col">Time</th>' + TL_PANELS.map(p => `<th scope="col">${esc(p.title)}</th>`).join('') + '</tr>';
  const body = rows.slice().reverse().map(r => '<tr><td>' + esc(tlTime(r.ts, true)) + '</td>' +
    TL_PANELS.map(p => `<td>${r[p.key] == null ? '—' : esc(p.fmt(r[p.key]))}</td>`).join('') + '</tr>').join('');
  return `<div class="tl-table-wrap"><table class="tl-table"><caption>Averages per ${esc(String(Math.round((TL_DATA.bucket_s || 300) / 60)))} min (range: maximum), newest first</caption><thead>${head}</thead><tbody>${body}</tbody></table></div>`;
}

function renderChanges(changes) {
  const list = document.getElementById('tl-changes');
  if (!list) return;
  if (!changes.length) { list.innerHTML = '<li class="field-hint">Nothing changed in this window.</li>'; return; }
  list.innerHTML = changes.map(c => {
    const k = TL_KIND[c.kind] || {icon: 'ti-point', label: c.kind};
    const lv = TL_LEVEL[c.level];
    return `<li class="tl-change ${esc(c.level || 'info')}">`
      + `<span class="tl-when">${esc(tlTime(c.ts, true))}</span>`
      + `<span class="tl-kind"><i class="ti ${esc(k.icon)}" aria-hidden="true"></i> ${esc(k.label)}</span>`
      + `<span class="tl-what"><b>${esc(c.subject)}</b> ${esc(c.detail)}</span>`
      + (lv ? `<span class="tl-level"><i class="ti ${esc(lv.icon)}" aria-hidden="true"></i> ${esc(lv.label)}</span>` : '<span></span>')
      + '</li>';
  }).join('');
}

function renderTimelineLinks(links) {
  const box = document.getElementById('tl-links');
  if (!box || !TAR1090_URL) return;
  const base = TAR1090_URL.replace(/\/+$/, '') + '/';
  const a = (href, icon, text, title) =>
    `<a class="btn btn-secondary" href="${esc(href)}" target="_blank" rel="noopener" title="${esc(title)}"><i class="ti ${icon}" aria-hidden="true"></i> ${esc(text)} <i class="ti ti-external-link" aria-hidden="true"></i></a>`;
  let html = a(base + '?pTracks=' + encodeURIComponent(TL_DATA.hours), 'ti-route',
               `All tracks, last ${TL_DATA.hours} h`, 'tar1090 with every track it kept in this window: the real coverage');
  if (links.outline) html += a(base + '?outlineWidth=3&hideSidebar', 'ti-shape', 'Range outline',
                               'tar1090 with readsb\'s range outline drawn thicker');
  html += links.heatmap
    ? a(base + '?heatmap=200000', 'ti-flame', 'Heatmap', 'tar1090 position heatmap')
    : '<span class="field-hint">Heatmap: needs readsb\'s <code>--heatmap</code> option.</span>';
  box.innerHTML = html;
}

window.addEventListener('resize', () => {
  if (TL_TIMER && !TL_TABLE) { clearTimeout(window._tlRs); window._tlRs = setTimeout(renderTimeline, 150); }
});
