// Startup: port mode + UI config, iframe URLs, then the 30 s poll.

// ── Init ───────────────────────────────────────────────────────────────────
// Entry point, run once on load. Fetches mode (admin vs read-only) and UI config
// (tar1090/graphs URLs), points the iframes, then defines load() — the 30s poll
// that refreshes status cards, metrics, alerts, and signal panel — and starts it.
// ───────────────────────────────────────────────────────────────────────────

let TAR1090_URL='',TAR1090_VERSION='',GRAPHS1090_URL='',GRAPHS1090_AIRSPY='',IS_READONLY=false;

async function logout(){
  try{await fetch('/api/logout',{method:'POST'});}catch{}
  location.replace('/login');
}

function withVersion(url,version){
  if(!version)return url;
  const [base,hash='']=url.split('#');
  return base+(base.includes('?')?'&':'?')+'v='+encodeURIComponent(version)+(hash?'#'+hash:'');
}

async function init(){
  try{const m=await fetch('/api/mode').then(r=>r.json());IS_READONLY=m.readonly;if(IS_READONLY){document.getElementById('btn-settings').style.display='none';document.getElementById('btn-logout').style.display='none';}if(m.version)document.getElementById('app-version').textContent='v'+m.version;}catch{}
  try{const cfg=await fetch('/api/ui-config').then(r=>r.json());TAR1090_URL=cfg.tar1090_url||`http://${location.hostname}:8504`;TAR1090_VERSION=cfg.tar1090_version||'';GRAPHS1090_URL=cfg.graphs1090_url||`http://${location.hostname}/graphs1090`;GRAPHS1090_AIRSPY=cfg.graphs1090_airspy||GRAPHS1090_URL;}
  catch{TAR1090_URL=`http://${location.hostname}:8504`;GRAPHS1090_URL=`http://${location.hostname}/graphs1090`;}
  // ?v=<tar1090 version>: a tar1090 update changes the URL, so the browser can't
  // keep showing a cached index.html whose hashed js/css files are gone (blank map).
  document.getElementById('map-frame').src=withVersion(TAR1090_URL,TAR1090_VERSION);
  const sigLink = document.getElementById('signal-history-link');
  if (sigLink) sigLink.href = GRAPHS1090_AIRSPY;
  document.getElementById('graphs-frame').src=`${GRAPHS1090_URL}/`;
  load();setInterval(load,30000);
}

// ── Event wiring ───────────────────────────────────────────────────────────
// No inline handlers (the CSP forbids them). Elements name an action with
// data-click / data-change, plus data-* arguments; one listener per event looks
// the action up here. Markup built from templates uses the same attributes, so
// it is wired the moment it is inserted.
const CLICK_ACTIONS = {
  showView:             el      => showView(el.dataset.view),
  toggleTheme:          ()      => toggleTheme(),
  load:                 ()      => load(),
  toggleSidebar:        ()      => toggleSidebar(),
  applyRecommendedGain: ()      => applyRecommendedGain(),
  logout:               ()      => logout(),
  toggleCard:           el      => toggleCard(el),
  saveAirspy:           (el, e) => saveAirspy(e),
  saveAutotune:         (el, e) => saveAutotune(e),
  saveReceiver:         (el, e) => saveReceiver(e),
  saveSdr:              (el, e) => saveSdr(el.dataset.band, e),
  saveFeeders:          (el, e) => saveFeeders(e),
  refreshVersions:      (el, e) => refreshVersions(e),
  addFeederRow:         ()      => addFeederRow(),
  removeFeeder:         el      => removeFeeder(Number(el.dataset.i)),
  downloadBackup:       ()      => downloadBackup(),
  downloadGraphs:       ()      => downloadGraphs(),
  pickFile:             el      => document.getElementById(el.dataset.target).click(),
  clearSysLog:          ()      => clearSysLog(),
  openSysLog:           ()      => openSysLog(),
  copyLogs:             el      => copyLogs(el.dataset.target, el),
  clearTerminal:        ()      => clearTerminal(),
  scrollToBottom:       ()      => scrollToBottom(),
  closeModal:           ()      => closeModal(),
  closeModalOnOverlay:  (el, e) => closeModalOnOverlay(e),
  openLogs:             el      => openLogs(el.dataset.key, el.dataset.label),
  restartService:       el      => restartService(el.dataset.key, el.dataset.label, el),
  toggleFeederCfg:      el      => toggleFeederCfg(el.dataset.panel, el),
  saveFeederCfg:        (el, e) => saveFeederCfg(el.dataset.key, el.dataset.panel, el.dataset.toast, e),
  toggleReveal:         el      => toggleReveal(el.dataset.input, el),
  revertConfig:         el      => revertConfig(el),
  setTimelineHours:     el      => setTimelineHours(el),
  toggleTimelineTable:  el      => toggleTimelineTable(el),
  retry:                el      => { const fn = globalThis[el.dataset.fn]; if (typeof fn === 'function') fn(); },
};
const CHANGE_ACTIONS = {
  confirmBiasTee: el => confirmBiasTee(el),
  importBackup:   el => importBackup(el.dataset.kind, el),
  feederField:    el => { feederData[Number(el.dataset.i)][el.dataset.field] = el.value; },
};
function wire(eventName, attr, table) {
  document.addEventListener(eventName, e => {
    const el = e.target.closest(`[${attr}]`);
    if (!el) return;
    const fn = table[el.getAttribute(attr)];
    if (fn) fn(el, e);
    else console.error(`no ${eventName} action "${el.getAttribute(attr)}"`);
  });
}
wire('click', 'data-click', CLICK_ACTIONS);
wire('change', 'data-change', CHANGE_ACTIONS);

init();
