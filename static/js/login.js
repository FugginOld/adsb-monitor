// Login page for the admin port (static/login.html). No inline JS: the CSP forbids it.
try { const t = localStorage.getItem('theme'); if (t === 'dark' || t === 'light') document.documentElement.dataset.theme = t; } catch {}

const $ = id => document.getElementById(id);
function showMsg(text) { $('msg').textContent = text; $('msg').hidden = !text; }

async function init() {
  try {
    const s = await fetch('/api/login/status').then(r => r.json());
    if (s.readonly || s.logged_in) { location.replace('/'); return; }
    if (!s.password_set) { $('setup-cmd').textContent = s.set_password_cmd; $('setup').hidden = false; return; }
    $('login-form').hidden = false; $('password').focus();
  } catch { showMsg('Could not reach the monitor. Reload to try again.'); }
}

$('login-form').addEventListener('submit', async e => {
  e.preventDefault();
  showMsg(''); $('login-btn').disabled = true;
  try {
    const r = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'adsb-monitor' },
      body: JSON.stringify({ password: $('password').value }),
    });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d.ok) { location.replace('/'); return; }
    showMsg(d.error || `Login failed (${r.status})`);
    $('password').select();
  } catch { showMsg('Could not reach the monitor.'); }
  $('login-btn').disabled = false;
});

init();
