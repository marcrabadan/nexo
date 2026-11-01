// Explicitly opt-in local events. Fixed allowlist; no transport or persistence.
function nexoObserve(kind, fields) {
  try {
    if(!['mount','request','navigation'].includes(kind))return;
    const detail={schema_version:1,kind};
    if(['started','loading','ready','error','cancelled','disposed'].includes(fields.phase))detail.phase=fields.phase;
    if(['load','mutation'].includes(fields.operation))detail.operation=fields.operation;
    if(fields.code!==undefined)detail.code=['Http','Network','Timeout','Cancelled','Validation','Limit','Security'].includes(fields.code)?fields.code:'Unknown';
    if(Number.isInteger(fields.status)&&fields.status>=0&&fields.status<=599)detail.status=fields.status;
    if(Number.isFinite(fields.duration_ms)&&fields.duration_ms>=0)detail.duration_ms=Math.round(fields.duration_ms);
    globalThis.dispatchEvent(new CustomEvent('nexo:web',{detail:Object.freeze(detail)}));
  } catch { /* Monitoring cannot change application semantics. */ }
}
