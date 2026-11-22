// Log modal (per-feeder journal over SSE).

// ── Log modal ──────────────────────────────────────────────────────────────
// The per-feeder log viewer (the "logs" button on each card). openLogs opens an
// SSE stream to /api/logs/<key> and appends lines; classifyLine colors them by
// severity; autoScroll sticks to the bottom unless you scroll up. Mirrors the
// system-log window above but for a single service and in a modal overlay.
// ───────────────────────────────────────────────────────────────────────────

let activeSSE=null,autoScroll=true;
function classifyLine(l){const s=l.toLowerCase();if(/error|fatal|failed|failure|crit/.test(s))return 'err';if(/warn|warning/.test(s))return 'warn';if(/started|success|running|active|connected|ok/.test(s))return 'ok';if(/^\s*$/.test(l))return 'dim';return '';}
function appendLine(text){const term=document.getElementById('terminal');const div=document.createElement('div');div.className='log-line '+classifyLine(text);div.textContent=text;term.appendChild(div);while(term.children.length>500)term.removeChild(term.firstChild);if(autoScroll)term.scrollTop=term.scrollHeight;}
function openLogs(key,label,url){closeModal();document.getElementById('modal-title-text').textContent=label+' — logs';document.getElementById('terminal').innerHTML='';document.getElementById('status-text').textContent='Connecting...';document.getElementById('status-dot').className='status-dot inactive';document.getElementById('modal').classList.add('open');lastFocused=document.activeElement;document.querySelector('#modal .modal').focus();autoScroll=true;const term=document.getElementById('terminal');term.onscroll=()=>{autoScroll=term.scrollTop+term.clientHeight>=term.scrollHeight-20;};const es=new EventSource(url||`/api/logs/${encodeURIComponent(key)}`);activeSSE=es;es.onopen=()=>{document.getElementById('status-dot').className='status-dot';document.getElementById('status-text').textContent=`Streaming: ${label}`;};es.onmessage=e=>{try{appendLine(JSON.parse(e.data));}catch{appendLine(e.data);}};es.onerror=()=>{document.getElementById('status-dot').className='status-dot inactive';document.getElementById('status-text').textContent='Stream closed';es.close();};}
function closeModal(){
  if(activeSSE){activeSSE.close();activeSSE=null;}
  document.getElementById('modal').classList.remove('open');
  // Return focus to whatever opened the dialog, or the keyboard user is dumped
  // at the top of the document with no idea where they were.
  if(lastFocused&&document.contains(lastFocused)){lastFocused.focus();}
  lastFocused=null;
}

let lastFocused=null;

// Escape closes, Tab cycles inside. The file had no keydown handling at all, so
// the overlay could only be dismissed with a mouse.
document.addEventListener('keydown',e=>{
  const modal=document.getElementById('modal');
  if(!modal.classList.contains('open'))return;
  if(e.key==='Escape'){e.preventDefault();closeModal();return;}
  if(e.key!=='Tab')return;
  const f=modal.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])');
  if(!f.length)return;
  const first=f[0], last=f[f.length-1];
  if(e.shiftKey&&document.activeElement===first){e.preventDefault();last.focus();}
  else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
});
function closeModalOnOverlay(e){if(e.target===document.getElementById('modal'))closeModal();}
function copyLogs(termId,btn){
  const t=document.getElementById(termId); if(!t) return;
  const text=t.innerText;
  const done=ok=>{const o=btn.innerHTML;btn.innerHTML='<i class="ti ti-'+(ok?'check':'x')+'" aria-hidden="true"></i> '+(ok?'Copied':'Failed');setTimeout(()=>btn.innerHTML=o,1500);};
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(()=>done(true),()=>done(false));
  }else{
    const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy');done(true);}catch{done(false);}document.body.removeChild(ta);
  }
}
function clearTerminal(){document.getElementById('terminal').innerHTML='';}
function scrollToBottom(){const t=document.getElementById('terminal');t.scrollTop=t.scrollHeight;autoScroll=true;}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeModal();});
