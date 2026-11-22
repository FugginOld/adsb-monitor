// Shared basics: the fetch wrapper (CSRF header, 401 -> login), theme,
// mobile sidebar and the sparkline helper. Loaded first; the other scripts
// are classic scripts too, so they share this file's top-level names.

// Every write carries X-Requested-With; the server refuses writes without it
// (cross-site request guard, system/auth.py csrf_guard).
{
  const _fetch = window.fetch.bind(window);
  window.fetch = (url, opts = {}) => {
    const m = (opts.method || 'GET').toUpperCase();
    if (m !== 'GET' && m !== 'HEAD') {
      const h = new Headers(opts.headers || {});
      h.set('X-Requested-With', 'adsb-monitor');
      opts = { ...opts, headers: h };
    }
    // Admin port: a 401 means the login session ended (expired, logged out, or the
    // password was changed); go to the login page instead of showing broken panels.
    return _fetch(url, opts).then(r => {
      if (r.status === 401 && !IS_READONLY) location.replace('/login');
      return r;
    });
  };
}

// HTML-escape a value for innerHTML text or a quoted attribute. Use it on every
// server-provided string (feeder labels/keys/hints from feeders.ini, error and
// status messages, versions, aircraft types); null/undefined become ''.
function esc(v){
  return String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Theme ──────────────────────────────────────────────────────────────────
// Cycles auto -> dark -> light, persisted in localStorage; applyTheme sets the
// data-theme attribute the CSS variables key off.
// ───────────────────────────────────────────────────────────────────────────

let theme = localStorage.getItem('theme') || 'auto';
function applyTheme(t) {
  if (t === 'auto') document.documentElement.removeAttribute('data-theme');
  else document.documentElement.setAttribute('data-theme', t);
}
applyTheme(theme);
function toggleTheme() {
  theme = theme === 'auto' ? 'dark' : theme === 'dark' ? 'light' : 'auto';
  localStorage.setItem('theme', theme);
  applyTheme(theme);
  document.getElementById('theme-btn').querySelector('i').className =
    theme === 'dark' ? 'ti ti-sun' : theme === 'light' ? 'ti ti-moon' : 'ti ti-sun-moon';
}

// ── Mobile sidebar ─────────────────────────────────────────────────────────
// On narrow screens the sidebar slides in/out; this toggles that class.
// ───────────────────────────────────────────────────────────────────────────

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

// ── Sparkline ──────────────────────────────────────────────────────────────
// Draws the little inline trend graphs (aircraft count, message rate) as a raw
// SVG polyline from a numbers array — no charting library.
// ───────────────────────────────────────────────────────────────────────────

function drawSparkline(svgId, data, color) {
  const svg = document.getElementById(svgId);
  if (!svg || data.length < 2) return;
  const w = 100, h = 28;
  const min = Math.min(...data), max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => {
    const x = (i / (data.length - 1)) * w;
    const y = h - ((v - min) / range) * h * 0.85 - 2;
    return `${x},${y}`;
  }).join(' ');
  svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"/>`;
}
