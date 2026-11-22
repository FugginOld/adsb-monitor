// Settings view: system log window, settings forms, auto-tune, stack updates,
// feeder connection configs and list, panel loading states, service restart.

// ── System log window (Settings) ─────────────────────────────────────────────
// The embedded terminal at the bottom of Settings. openSysLog opens an SSE stream
// to /api/syslog (whole-system journal) and appends each line; closeSysLog tears
// it down when you leave Settings so the connection doesn't leak.
// ─────────────────────────────────────────────────────────────────────────────

let sysSSE=null;
function setSysStatus(text,on){const s=document.getElementById('sys-status-text');if(s)s.textContent=text;const d=document.getElementById('sys-status-dot');if(d)d.className='status-dot'+(on?'':' inactive');}
function clearSysLog(){const t=document.getElementById('sys-terminal');if(t)t.innerHTML='';}
function openSysLog(){
  closeSysLog();
  const term=document.getElementById('sys-terminal');if(!term)return;
  term.innerHTML='';setSysStatus('Connecting…',false);
  const es=new EventSource('/api/syslog');sysSSE=es;
  es.onopen=()=>setSysStatus('Streaming system journal',true);
  es.onmessage=e=>{let t;try{t=JSON.parse(e.data);}catch{t=e.data;}
    const d=document.createElement('div');d.className='log-line '+classifyLine(t);d.textContent=t;
    term.appendChild(d);while(term.children.length>500)term.removeChild(term.firstChild);
    term.scrollTop=term.scrollHeight;};
  es.onerror=()=>{setSysStatus('Stream closed',false);es.close();sysSSE=null;};
}
function closeSysLog(){if(sysSSE){sysSSE.close();sysSSE=null;}}

function toggleCard(header) {
  header.classList.toggle('collapsed');
  header.nextElementSibling.classList.toggle('hidden');
}

// ── Settings ───────────────────────────────────────────────────────────────
// Loads every Settings card from the API on first open (loadSettings, guarded by
// settingsLoaded) and wires the save buttons that POST changes back: airspy gain/
// rate, receiver lat/lon/range, aircraft-type and uptime panels, version refresh,
// and config backup download. showToast gives the little inline success/error flash.
// ───────────────────────────────────────────────────────────────────────────

function showToast(id, ok, msg) {
  const el=document.getElementById(id);
  if (!el) return;
  // Call sites prefix the message with ✓/✗. Strip it and render the state as an
  // icon: the glyph was read aloud literally ("check mark") by screen readers,
  // and role="status" on the span now announces whatever textContent remains.
  const text=String(msg).replace(/^[✓✗]\s*/,'');
  const icon=ok?'ti-check':'ti-alert-triangle';
  el.innerHTML=`<i class="ti ${icon}" aria-hidden="true" style="margin-right:4px"></i>${esc(text)}`;
  el.className='toast '+(ok?'ok':'err');
  // Errors stay until the next action — a 3.5s window is not enough to read a
  // decoder's stderr, and it is the one message the user actually needs.
  clearTimeout(el._t);
  if (ok) el._t=setTimeout(()=>{el.className='toast';},3500);
}

let settingsLoaded=false;
async function loadSettings() {
  if (settingsLoaded) return;
  settingsLoaded=true;
  loadSdrSettings();   // 1090 / 978 gain + bias-tee
  loadAutotune();
  loadStackUpdates();
  // Airspy
  try {
    const r=await fetch('/api/settings/airspy').then(r=>r.json());
    if(r.ok){
      const st=r.settings, model=st.model||'unknown';
      const gSel=document.getElementById('airspy-gain');
      gSel.replaceChildren(...['auto',...Array.from({length:22},(_,i)=>String(i))].map(v=>new Option(v,v)));
      gSel.value=st.gain;
      const rSel=document.getElementById('airspy-rate');
      rSel.replaceChildren(new Option('decoder default',''),...(st.rate_options||[]).map(v=>new Option(v+' MHz',v)));
      rSel.value=st.sample_rate||''; rSel.disabled=!(st.rate_options||[]).length;
      document.getElementById('airspy-options').value=st.options;
      const info=document.getElementById('airspy-model-info');
      info.textContent='';
      info.append('Detected: ');
      const b=document.createElement('strong'); b.textContent='Airspy '+model.toUpperCase(); info.append(b);
      if(st.live) info.append(` · live: ${st.live.sample_rate} MSPS, gain ${st.live.gain}${st.gain==='auto'?' (auto)':''}`);
    }
  } catch{}
  // Receiver
  try {
    const r=await fetch('/api/settings/receiver').then(r=>r.json());
    if(r.ok){document.getElementById('recv-lat').value=r.settings.lat;document.getElementById('recv-lon').value=r.settings.lon;document.getElementById('recv-range').value=r.settings.max_range;}
  } catch{}
  // Aircraft types
  await loadAircraftTypes();
  // Uptime history
  await loadUptimeHistory();
  // readsb stats
  await loadReadsbStats();
  // Feeder configs
  await loadFeederConfigs();
  // Feeder list
  await loadFeedersTable();
}

async function loadAircraftTypes() {
  const body = document.getElementById('ac-types-body');
  body.innerHTML = skeleton(4);
  try {
    const d = await fetch('/api/stats/aircraft_types').then(r=>r.json());
    const total = d.adsb + d.mlat + d.mode_s + d.tisb + d.other || 1;
    const colors = ['#4ade80','#60a5fa','#f97316','#a78bfa','#94a3b8'];
    const types = [
      {label:'ADS-B', val:d.adsb, color:colors[0]},
      {label:'Mode S', val:d.mode_s, color:colors[2]},
      {label:'MLAT', val:d.mlat, color:colors[1]},
      {label:'TIS-B', val:d.tisb, color:colors[3]},
      {label:'Other', val:d.other, color:colors[4]},
    ].filter(t=>t.val>0);

    // Build SVG donut
    const r=40, cx=50, cy=50, stroke=14;
    const circ = 2*Math.PI*r;
    let offset=0;
    let arcs='';
    for(const t of types){
      const pct=t.val/total;
      const dash=circ*pct;
      arcs+=`<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${t.color}" stroke-width="${stroke}" stroke-dasharray="${dash} ${circ-dash}" stroke-dashoffset="${-offset}" transform="rotate(-90 ${cx} ${cy})"><title>${esc(t.label)}: ${esc(t.val)}</title></circle>`;
      offset+=dash;
    }
    const svg=`<svg viewBox="0 0 100 100" width="80" height="80">${arcs}<text x="50" y="47" text-anchor="middle" font-size="11" fill="var(--text)" font-weight="500">${esc(total)}</text><text x="50" y="59" text-anchor="middle" font-size="8" fill="var(--text3)">total</text></svg>`;
    const legend=types.map(t=>`<div class="type-row"><div class="type-dot" style="background:${t.color}"></div>${esc(t.label)}<span class="type-val">${esc(t.val)} <span style="color:var(--text3);font-weight:400">(${Math.round(t.val/total*100)}%)</span></span></div>`).join('');
    body.innerHTML=`<div class="type-wrap">${svg}<div class="type-legend">${legend}</div></div>`;
  } catch{body.innerHTML=panelError('Could not load aircraft types','loadAircraftTypes');}
}

async function loadUptimeHistory() {
  const body = document.getElementById('uptime-history-body');
  body.innerHTML = skeleton(3);
  try {
    const d = await fetch('/api/stats/uptime/history').then(r=>r.json());
    const rows = Object.entries(d).map(([key, days]) => {
      const bars = days.map(day => {
        const cls = day.pct === null ? 'unknown' : day.pct >= 99 ? 'up' : day.pct >= 90 ? 'up' : 'down';
        const title = day.pct !== null ? `${day.day}: ${day.pct}%` : `${day.day}: no data`;
        return `<div class="utl-bar ${cls}" title="${esc(title)}"></div>`;
      }).join('');
      const avg = days.filter(d=>d.pct!==null);
      const avgPct = avg.length ? Math.round(avg.reduce((s,d)=>s+d.pct,0)/avg.length*10)/10 : null;
      const pctStr = avgPct!==null ? `${avgPct}%` : '—';
      // Get label from feeders
      const label = key.replace(/-/g,' ');
      return `<div class="utl-row">
        <div class="utl-label" title="${esc(key)}">${esc(label)}</div>
        <div class="utl-bars">${bars}</div>
        <div class="utl-pct">${pctStr}</div>
      </div>`;
    }).join('');
    // Day labels
    const firstDays = Object.values(d)[0] || [];
    const dayLabels = firstDays.map(d=>`<div style="flex:1;text-align:center;font-size:10px;color:var(--text3)">${esc(d.day)}</div>`).join('');
    body.innerHTML=`
      <div style="display:flex;margin-left:138px;gap:2px;margin-bottom:4px;">${dayLabels}</div>
      <div class="uptime-timeline">${rows}</div>
      <div style="font-size:10px;color:var(--text3);margin-top:6px;display:flex;gap:12px;">
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--ok-text);opacity:.7;margin-right:3px"></span>Up</span>
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--err-text);opacity:.7;margin-right:3px"></span>Down</span>
        <span><span style="display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--surface3);margin-right:3px"></span>No data</span>
      </div>`;
  } catch{body.innerHTML=panelError('Could not load uptime history','loadUptimeHistory');}
}

function downloadBackup() {
  window.location.href = '/api/backup';
}
function downloadGraphs() {
  window.location.href = '/api/backup/graphs';
}
async function importBackup(kind,input){
  const file=input.files[0]; if(!file){return;}
  const url = kind==='graphs' ? '/api/restore/graphs' : '/api/restore';
  const warn = kind==='graphs'
    ? 'Restore graph history?\n\ncollectd stops briefly and existing RRD files are overwritten.'
    : 'Restore config from this backup?\n\nOverwrites current config files and restarts readsb/dump978.';
  if(!confirm(warn)){ input.value=''; return; }
  showToast('backup-toast',true,'Uploading…');
  try{
    const fd=new FormData(); fd.append('file',file);
    const r=await fetch(url,{method:'POST',body:fd}).then(r=>r.json());
    const n = Array.isArray(r.restored)?r.restored.length:r.restored;
    showToast('backup-toast',r.ok,r.ok?`✓ Restored ${n} item(s)`:'✗ '+(r.error||'failed'));
  }catch(ex){ showToast('backup-toast',false,'✗ '+ex.message); }
  input.value='';
}

async function loadReadsbStats() {
  const body=document.getElementById('readsb-stats-body');
  body.innerHTML = skeleton(4);
  try {
    const s=await fetch('/api/stats/readsb').then(r=>r.json());
    body.innerHTML=`<div class="stats-grid">
      <div class="stat-box"><div class="stat-box-label">Msgs last min</div><div class="stat-box-value">${esc(s.msgs_last_min?.toLocaleString()||'—')}</div></div>
      <div class="stat-box"><div class="stat-box-label">Positions/min</div><div class="stat-box-value">${esc(s.positions_last_min?.toLocaleString()||'—')}</div></div>
      <div class="stat-box"><div class="stat-box-label">ADS-B %</div><div class="stat-box-value">${esc(s.adsb_pct||'—')}%</div></div>
      <div class="stat-box"><div class="stat-box-label">Total msgs</div><div class="stat-box-value">${esc((s.msgs_total/1e6)?.toFixed(1)||'—')}M</div></div>
      <div class="stat-box"><div class="stat-box-label">Total positions</div><div class="stat-box-value">${esc((s.positions_total/1e6)?.toFixed(1)||'—')}M</div></div>
      <div class="stat-box"><div class="stat-box-label">Uptime</div><div class="stat-box-value">${esc(s.uptime_hrs||'—')}h</div></div>
    </div>`;
  } catch{body.innerHTML=panelError('Could not load readsb stats','loadReadsbStats');}
}

// POST Airspy settings. While auto-tune is on the server refuses a gain change
// (409 autotune_enabled) unless override_autotune is set; ask first, and say
// plainly that overriding turns auto-tune off. Returns the final JSON, or
// {ok:false, cancelled:true} if the user backed out.
async function postAirspySettings(payload){
  const post=body=>fetch('/api/settings/airspy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(async r=>({status:r.status,...(await r.json())}));
  let r=await post(payload);
  if(r.status===409&&r.error==='autotune_enabled'){
    if(!confirm(`Auto-tune is ON and is managing the Airspy gain (currently ${r.current_gain}).

`
      +`Set the gain to ${payload.gain} manually?

`
      +`This turns auto-tune OFF so it won't change your gain back. `
      +`You can turn it on again in Settings → Airspy SDR.`)) return {ok:false,cancelled:true};
    r=await post({...payload,override_autotune:true});
    if(r.ok) loadAutotune();
  }
  return r;
}

async function saveAirspy(e){
  const btn=e.target.closest('button');btn.disabled=true;
  try{const r=await postAirspySettings({gain:document.getElementById('airspy-gain').value,sample_rate:document.getElementById('airspy-rate').value,options:document.getElementById('airspy-options').value});
  if(!r.cancelled) showToast('airspy-toast',r.ok,r.ok?'✓ Saved & restarted':'✗ '+(r.error||r.output));
  else showToast('airspy-toast',false,'Not saved — auto-tune left on');}catch(ex){showToast('airspy-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

// ── Airspy auto-tune ───────────────────────────────────────────────────────
// Config + live status + recent changes from /api/autotune. The tuning itself
// runs server-side in the background poller (system/autotune.py).
// ───────────────────────────────────────────────────────────────────────────

let _autotuneEnabled=false;

async function loadAutotune(){
  try{
    const d=await fetch('/api/autotune').then(r=>r.json());
    if(!d.ok) return;
    const c=d.config; _autotuneEnabled=!!c.enabled;
    const badge=document.getElementById('autotune-badge');
    badge.classList.toggle('hidden',!c.enabled);
    badge.title='Auto-tune: '+d.state.status;
    const note=document.getElementById('autotune-gain-note');
    note.classList.toggle('hidden',!c.enabled);
    document.getElementById('autotune-gain-note-text').textContent=
      `Auto-tune is on and manages the gain (${d.state.status}). Saving a different gain will ask before turning auto-tune off.`;
    const en=document.getElementById('autotune-enabled');
    if(!en) return;
    en.checked=c.enabled;
    document.getElementById('autotune-min').value=c.min_gain;
    document.getElementById('autotune-max').value=c.max_gain;
    document.getElementById('autotune-thresholds').textContent=
      `Lowers gain above ${c.strong_high_pct}% of DF17s at ≥${c.strong_rssi_db} dB, raises it below ${c.strong_low_pct}%. Window ${c.window_min} min, min ${c.min_messages} messages.`;
    document.getElementById('autotune-status').textContent='Status: '+d.state.status;
    const ul=document.getElementById('autotune-history');
    ul.replaceChildren(...d.history.map(h=>{const li=document.createElement('li');
      li.textContent=`${new Date(h.ts*1000).toLocaleString()}: ${h.old} → ${h.new} — ${h.reason}`;return li;}));
  }catch{}
}

async function saveAutotune(e){
  const btn=e.target.closest('button');btn.disabled=true;
  try{const r=await fetch('/api/autotune',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      enabled:document.getElementById('autotune-enabled').checked,
      min_gain:parseInt(document.getElementById('autotune-min').value,10),
      max_gain:parseInt(document.getElementById('autotune-max').value,10)})}).then(r=>r.json());
    showToast('autotune-toast',r.ok,r.ok?'✓ Saved':'✗ '+r.error);
    if(r.ok) loadAutotune();
  }catch(ex){showToast('autotune-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

async function saveReceiver(e){
  const btn=e.target.closest('button');btn.disabled=true;
  try{const r=await fetch('/api/settings/receiver',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({lat:document.getElementById('recv-lat').value,lon:document.getElementById('recv-lon').value,max_range:document.getElementById('recv-range').value})}).then(r=>r.json());
  showToast('recv-toast',r.ok,r.ok?'✓ Saved & restarted':'✗ '+(r.error||r.output));}catch(ex){showToast('recv-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

// Bias-tee sends DC up the coax — confirm before arming, revert the box if declined.
function confirmBiasTee(cb){
  if(!cb.checked) return;
  const ok=confirm('Enable bias-tee?\n\nThis feeds ~4.5V DC up the antenna coax to power an inline LNA. '
    +'Only enable it if you have an LNA/filter that expects bias-tee power. Feeding DC into '
    +'gear that does not (or a DC short) can damage the SDR or hardware.\n\nEnable it?');
  if(!ok) cb.checked=false;
}

// Supported gain steps for the R820T/R820T2 tuner (the 29 rtl_test values).
const RTL_GAINS=['0.0','0.9','1.4','2.7','3.7','7.7','8.7','12.5','14.4','15.7','16.6','19.7','20.7','22.9','25.4','28.0','29.7','32.8','33.8','36.4','37.2','38.6','40.2','42.1','43.4','43.9','44.5','48.0','49.6'];
function fillGainSelect(id,special){
  const sel=document.getElementById(id); if(!sel||sel.dataset.filled) return;
  sel.innerHTML=[special,...RTL_GAINS].map(v=>`<option value="${v}">${v}</option>`).join('');
  sel.dataset.filled='1';
}
async function loadSdrSettings(){
  fillGainSelect('sdr1090-gain','auto');
  fillGainSelect('sdr978-gain','max');
  for(const band of ['1090','978']){
    try{
      const r=await fetch('/api/settings/sdr/'+band).then(r=>r.json());
      if(!r.ok) continue;
      const sel=document.getElementById('sdr'+band+'-gain'), g=String(r.settings.gain);
      if(![...sel.options].some(o=>o.value===g)) sel.add(new Option(g,g)); // preserve an unusual saved value
      sel.value=g;
      document.getElementById('sdr'+band+'-biastee').checked=!!r.settings.biastee;
    }catch{}
  }
}

async function saveSdr(band,e){
  const btn=e.target.closest('button');btn.disabled=true;
  try{const r=await fetch('/api/settings/sdr/'+band,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({gain:document.getElementById('sdr'+band+'-gain').value,
                         biastee:document.getElementById('sdr'+band+'-biastee').checked})}).then(r=>r.json());
  showToast('sdr'+band+'-toast',r.ok,r.ok?'✓ Saved & restarted':'✗ '+(r.error||r.output));}
  catch(ex){showToast('sdr'+band+'-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

async function refreshVersions(e){
  const btn=e.target.closest('button');btn.disabled=true;
  showToast('versions-toast',true,'Checking...');
  try{const r=await fetch('/api/versions/refresh',{method:'POST'}).then(r=>r.json());
  const upd=Object.entries(r.versions||{}).filter(([,v])=>v&&v.outdated).map(([k])=>k);
  const msg=!r.ok?'✗ Error':upd.length?`⬆ Update available: ${upd.join(', ')}`:'✓ All up to date';
  showToast('versions-toast',r.ok,msg);
  settingsLoaded=false; await loadSettings();}catch(ex){showToast('versions-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

// ── Stack updates ──────────────────────────────────────────────────────────
// One button per component in system/stack_update.py (readsb, tar1090,
// graphs1090). Each runs wiedehopf's upstream installer as a transient systemd
// unit on the host; the log modal follows its journal. One update at a time,
// polled every 5 s while one runs. Admin port only.
// ───────────────────────────────────────────────────────────────────────────

let _updPoll=null;

async function loadStackUpdates(){
  const c=document.getElementById('update-btns');
  if(!c) return;
  try{
    const d=await fetch('/api/stack/updates').then(r=>r.json());
    if(!d.ok) return;
    if(!d.supported){c.textContent='Updates from the dashboard need systemd — use installer/update-stack.sh.';return;}
    const comps=Object.entries(d.components);
    const running=comps.find(([,v])=>v.state==='running');
    c.replaceChildren(...comps.map(([k,v])=>{
      const ver=(d.versions||{})[k]||{};
      const wrap=document.createElement('span');
      wrap.style.cssText='display:inline-flex;gap:4px;';
      const b=document.createElement('button');
      b.className='btn '+(ver.outdated?'btn-primary':'btn-secondary');
      const icon=document.createElement('i');
      icon.className='ti '+(v.state==='running'?'ti-loader-2':'ti-download'); icon.setAttribute('aria-hidden','true');
      const text=v.state==='running'?`Updating ${v.label}…`
        :ver.outdated?`Update ${v.label} ${ver.installed} → ${ver.latest}`
        :`${v.label} ${ver.installed||''} · up to date`;
      b.append(icon,' '+text);
      b.disabled=!!running;
      b.onclick=()=>startStackUpdate(k,v,ver);
      wrap.append(b);
      if(v.state!=='idle'){
        const l=document.createElement('button');
        l.className='btn btn-secondary';
        l.textContent=v.state==='failed'?'✗ log':v.state==='success'?'✓ log':'log';
        l.title=`${v.label} update: ${v.state}${v.finished?' at '+v.finished:''}`;
        l.onclick=()=>openLogs(k,`${v.label} update`,`/api/stack/update/${encodeURIComponent(k)}/logs`);
        wrap.append(l);
      }
      return wrap;
    }));
    const st=document.getElementById('update-status');
    if(running) st.textContent=`Updating ${running[1].label} — started ${running[1].started||''}. You can close this page; it keeps running.`;
    else{
      const last=comps.filter(([,v])=>v.state==='success'||v.state==='failed')
        .map(([,v])=>`${v.label}: ${v.state==='success'?'updated':'FAILED (exit '+(v.exit_status||'?')+') — see log'}${v.finished?' ('+v.finished+')':''}`);
      st.textContent=last.join(' · ');
    }
    if(running&&!_updPoll) _updPoll=setInterval(loadStackUpdates,5000);
    if(!running&&_updPoll){clearInterval(_updPoll);_updPoll=null;}
  }catch{}
}

async function startStackUpdate(key,comp,ver){
  const what=ver.outdated?`Update ${comp.label} ${ver.installed} → ${ver.latest}?`
    :`${comp.label} looks up to date${ver.installed?' ('+ver.installed+')':''}. Reinstall it anyway?`;
  if(!confirm(`${what}

${comp.impact}

`
    +`This runs wiedehopf's upstream installer as root. Its output opens in the log window.`)) return;
  try{
    const r=await fetch(`/api/stack/update/${encodeURIComponent(key)}`,{method:'POST'}).then(r=>r.json());
    showToast('versions-toast',r.ok,r.ok?`✓ ${r.message}`:`✗ ${r.error}`);
    if(r.ok) openLogs(key,`${comp.label} update`,`/api/stack/update/${encodeURIComponent(key)}/logs`);
  }catch(ex){showToast('versions-toast',false,'✗ '+ex.message);}
  loadStackUpdates();
}

// ── Feeder connection configs ──────────────────────────────────────────────
// The per-feeder settings cards (sharing keys, UUIDs, hosts). Fields are described
// by the backend; renderFeederFields/renderField build the inputs generically,
// with reveal toggles for secret fields and a collapsible "configure" panel.
// ───────────────────────────────────────────────────────────────────────────

async function loadFeederConfigs(){
  const body=document.getElementById('feeder-settings-body');
  let feeders=[];
  try{const d=await fetch('/api/status').then(r=>r.json());feeders=d.feeders;}catch{body.innerHTML=panelError('Could not load feeders','loadFeederConfigs');return;}
  const html=[];
  for(const f of feeders){
    let fields=null,settings={},label=f.label;
    try{const r=await fetch(`/api/settings/feeder/${encodeURIComponent(f.key)}`).then(r=>r.json());
    if(r.ok){fields=r.fields;settings=r.settings;label=r.label;}}catch{}
    const panelId=`fcfg-${f.key}`,toastId=`fcfg-toast-${f.key}`;
    if(!fields){
      html.push(`<div><div style="display:flex;align-items:center;justify-content:space-between;"><div style="font-size:12px;font-weight:500;color:var(--text2);display:flex;align-items:center;gap:6px;"><i class="ti ti-${esc(f.icon||'plug')}" style="font-size:13px" aria-hidden="true"></i>${esc(f.label)}</div><span style="font-size:11px;color:var(--text3)">No config available</span></div></div>`);
    }else{
      html.push(`<div>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;">
          <div style="font-size:12px;font-weight:500;color:var(--text);display:flex;align-items:center;gap:6px;"><i class="ti ti-${esc(f.icon||'plug')}" style="font-size:13px;color:var(--text2)" aria-hidden="true"></i>${esc(label)}</div>
          <button class="cfg-btn" data-click="toggleFeederCfg" data-panel="${esc(panelId)}"><i class="ti ti-settings" aria-hidden="true"></i> configure</button>
        </div>
        <div class="feeder-config-panel" id="${esc(panelId)}">
          ${renderFeederFields(f.key,fields,settings)}
          <div class="btn-row">
            <button class="btn btn-primary" data-click="saveFeederCfg" data-key="${esc(f.key)}" data-panel="${esc(panelId)}" data-toast="${esc(toastId)}"><i class="ti ti-device-floppy" aria-hidden="true"></i> Save &amp; restart</button>
            <span class="toast" role="status" id="${esc(toastId)}"></span>
          </div>
        </div>
      </div>`);
    }
    if(f!==feeders[feeders.length-1])html.push('<div class="divider"></div>');
  }
  body.innerHTML=html.join('');
}

function renderFeederFields(key,fields,settings){
  let html='',i=0;
  while(i<fields.length){
    const f1=fields[i],f2=fields[i+1];
    if(f2&&!f1.readonly&&!f2.readonly){html+=`<div class="field-row">${renderField(key,f1,settings[f1.key]||'')}${renderField(key,f2,settings[f2.key]||'')}</div>`;i+=2;}
    else{html+=renderField(key,f1,settings[f1.key]||'');i++;}
  }
  return html;
}

function renderField(key,f,value){
  const fid=`fcfg-${key}-${f.key}`,ro=f.readonly?'readonly':'';
  const hint=f.readonly?'<div class="field-hint">Read-only</div>':'';
  if(f.type==='password')return`<div class="field"><label class="field-label" for="${esc(fid)}">${esc(f.label)}</label><div class="input-wrap"><input type="password" id="${esc(fid)}" value="${esc(value)}" ${ro}><button class="eye-btn" data-click="toggleReveal" data-input="${esc(fid)}"><i class="ti ti-eye" aria-hidden="true"></i></button></div>${hint}</div>`;
  if(f.type==='select'){const opts=(f.options||[]).map(o=>`<option value="${esc(o)}"${value===o?' selected':''}>${esc(o)}</option>`).join('');return`<div class="field"><label class="field-label" for="${esc(fid)}">${esc(f.label)}</label><select id="${esc(fid)}">${opts}</select>${hint}</div>`;}
  return`<div class="field"><label class="field-label" for="${esc(fid)}">${esc(f.label)}</label><input type="text" id="${esc(fid)}" value="${esc(value)}" ${ro}>${hint}</div>`;
}

function toggleReveal(inputId,btn){const inp=document.getElementById(inputId);const showing=inp.type==='text';inp.type=showing?'password':'text';btn.querySelector('i').className=showing?'ti ti-eye':'ti ti-eye-off';}
function toggleFeederCfg(panelId,btn){const panel=document.getElementById(panelId);const open=panel.classList.toggle('open');btn.innerHTML=open?'<i class="ti ti-chevron-up" aria-hidden="true"></i> collapse':'<i class="ti ti-settings" aria-hidden="true"></i> configure';}

async function saveFeederCfg(key,panelId,toastId,e){
  const btn=e.target.closest('button');btn.disabled=true;
  const panel=document.getElementById(panelId);
  const inputs=panel.querySelectorAll('input:not([readonly]),select');
  const data={};
  inputs.forEach(inp=>{const parts=inp.id.split('-');const fkey=parts.slice(2).join('-');if(fkey)data[fkey]=inp.value;});
  try{const r=await fetch(`/api/settings/feeder/${encodeURIComponent(key)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}).then(r=>r.json());
  showToast(toastId,r.ok,r.ok?`✓ ${r.message||'Saved'}`:`✗ ${r.error||'Error'}`);}catch(ex){showToast(toastId,false,'✗ '+ex.message);}
  btn.disabled=false;
}

// ── Feeder list table ──────────────────────────────────────────────────────
// Editor for which entries appear in the sidebar (the feeders.ini list). Add/
// remove rows and save the table back via the API. esc() HTML-escapes user input
// to prevent markup injection when building rows as strings.
// ───────────────────────────────────────────────────────────────────────────

let feederData=[];
// ── D4: panel loading / failure states ──
// Every data panel used to show nothing while fetching and a dead grey string on
// failure. skeleton() reserves the row height (so the panel doesn't jump when
// data lands) and panelError() gives the user a way out.
function skeleton(rows) {
  return `<div class="skel">${'<div class="skel-row"></div>'.repeat(rows||3)}</div>`;
}
function panelError(msg, retryFn) {
  return `<div class="panel-error"><span>${esc(msg)}</span>`
       + `<button class="action-btn" data-click="retry" data-fn="${esc(retryFn)}">`
       + `<i class="ti ti-refresh" aria-hidden="true"></i> Retry</button></div>`;
}

async function loadFeedersTable(){
  try{const r=await fetch('/api/settings/feeders').then(r=>r.json());feederData=r.feeders;renderFeedersTable();}catch{}
}
function renderFeedersTable(){
  document.getElementById('feeder-rows').innerHTML=feederData.map((f,i)=>`<tr>
    <td style="padding-left:1rem"><select data-change="feederField" data-i="${i}" data-field="kind"><option value="service"${f.kind==='service'?' selected':''}>service</option><option value="docker"${f.kind==='docker'?' selected':''}>docker</option></select></td>
    <td><input type="text" value="${esc(f.key)}"   data-change="feederField" data-i="${i}" data-field="key"></td>
    <td><input type="text" value="${esc(f.label)}" data-change="feederField" data-i="${i}" data-field="label"></td>
    <td><input type="text" value="${esc(f.hint)}"  data-change="feederField" data-i="${i}" data-field="hint"></td>
    <td><input type="text" value="${esc(f.icon)}"  data-change="feederField" data-i="${i}" data-field="icon" placeholder="plug"></td>
    <td><button class="btn btn-danger" style="padding:3px 7px" data-click="removeFeeder" data-i="${i}"><i class="ti ti-trash" aria-hidden="true"></i></button></td>
  </tr>`).join('');
}
function removeFeeder(i){feederData.splice(i,1);renderFeedersTable();}
function addFeederRow(){
  const key=document.getElementById('new-key').value.trim();
  if(!key){showToast('feeders-toast',false,'✗ Service name required');return;}
  feederData.push({key,kind:document.getElementById('new-kind').value,label:document.getElementById('new-label').value||key,hint:document.getElementById('new-hint').value,icon:document.getElementById('new-icon').value||'plug'});
  renderFeedersTable();['new-key','new-label','new-hint','new-icon'].forEach(id=>document.getElementById(id).value='');
}
async function saveFeeders(e){
  const btn=e.target.closest('button');btn.disabled=true;
  try{const r=await fetch('/api/settings/feeders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({feeders:feederData})}).then(r=>r.json());
  showToast('feeders-toast',r.ok,r.ok?'✓ Saved':'✗ '+r.error);if(r.ok)load();}catch(ex){showToast('feeders-toast',false,'✗ '+ex.message);}
  btn.disabled=false;
}

// ── Service restart ────────────────────────────────────────────────────────
// Builds the row of "restart <service>" buttons in Settings from the feeder list;
// each posts to the service-control API (admin port only).
// ───────────────────────────────────────────────────────────────────────────

function buildRestartButtons(feeders){
  const c=document.getElementById('restart-btns');
  if(c.children.length>0)return;
  c.innerHTML=feeders.map(f=>`<button class="btn btn-secondary" data-click="restartService" data-key="${esc(f.key)}" data-label="${esc(f.label)}"><i class="ti ti-refresh" aria-hidden="true"></i> ${esc(f.label)}</button>`).join('');
}
const DECODERS = ['readsb','airspy_adsb','dump978-fa'];
async function restartService(key,label,btn){
  if (DECODERS.includes(key) &&
      !confirm(`Restart ${label}?

This drops the live feed for a few seconds — `
             + `aircraft currently tracked will disappear from the map until it reconnects.`)) return;
  btn.disabled=true;
  try{const r=await fetch(`/api/service/${encodeURIComponent(key)}/restart`,{method:'POST'}).then(r=>r.json());
  showToast('restart-toast',r.ok,r.ok?`✓ ${label} restarted`:`✗ ${r.error}`);}catch(ex){showToast('restart-toast',false,'✗ '+ex.message);}
  setTimeout(()=>{btn.disabled=false;},3000);
}
