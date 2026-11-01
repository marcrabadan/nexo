// M3: bounded asynchronous same-origin JSON, scoped to one mounted application.
const DATA_SCOPE=Symbol('Nexo Web request scope');
function requestError(kind,message,status=0){return Object.freeze({kind,message,status:BigInt(status)});}
function badRequest(kind,message,status=0){throw requestError(kind,message,status);}
function validateJSON(value,schema,depth=0){
  if(depth>32)badRequest('Validation','JSON nesting exceeds 32 levels');
  switch(schema.kind){
    case 'str':if(typeof value!=='string')badRequest('Validation','Expected JSON str');return value;
    case 'bool':if(typeof value!=='boolean')badRequest('Validation','Expected JSON bool');return value;
    case 'int':
      if(typeof value==='bigint'){int(value);if(value>9007199254740991n||value< -9007199254740991n)badRequest('Validation','JSON int exceeds safe wire range');return value;}
      if(!Number.isSafeInteger(value))badRequest('Validation','Expected safe JSON integer');return BigInt(value);
    case 'list':
      if(!Array.isArray(value)||value.length>10000)badRequest('Validation','Expected JSON list with at most 10000 items');
      return Object.freeze(value.map(item=>validateJSON(item,schema.item,depth+1)));
    case 'record':{
      if(value===null||typeof value!=='object'||Array.isArray(value))badRequest('Validation','Expected JSON record');
      const keys=Object.keys(schema.fields);
      if(Object.keys(value).length!==keys.length||keys.some(key=>!Object.hasOwn(value,key)))badRequest('Validation','JSON record fields do not match contract');
      const out=Object.create(null);for(const key of keys)out[key]=validateJSON(value[key],schema.fields[key],depth+1);return Object.freeze(out);
    }
    default:badRequest('Validation','Unsupported JSON contract');
  }
}
function requestURL(path){
  if(typeof path!=='string'||path.length>8192||!path.startsWith('/')||path.startsWith('//')||/[\\#\u0000-\u0020\u007f]/.test(path))badRequest('Security','Expected same-origin API path');
  const url=new URL(path,location.origin);
  if(url.origin!==location.origin||url.username||url.password)badRequest('Security','Cross-origin API request denied');
  if(location.protocol!=='https:'&&!['localhost','127.0.0.1','[::1]'].includes(location.hostname))badRequest('Security','Production API requests require HTTPS');
  return url.href;
}
function useDataScope(env,owner){
  if(env.has(DATA_SCOPE))return env.get(DATA_SCOPE);
  const scope={pending:new Map(),cache:new Map(),epoch:0};env.set(DATA_SCOPE,scope);
  owner.cleanup(()=>{scope.epoch++;for(const entry of scope.pending.values())entry.controller.abort();scope.pending.clear();scope.cache.clear();});
  scope.invalidate=()=>{scope.epoch++;scope.cache.clear();};
  return scope;
}
function canonical(value){
  if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
  if(value&&typeof value==='object')return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
  return JSON.stringify(value);
}
async function readJSON(response,signal){
  const media=(response.headers.get('content-type')||'').split(';')[0].trim().toLowerCase();
  if(media!=='application/json'&&!/^application\/[\w.-]+\+json$/.test(media))badRequest('Validation','Response must be JSON');
  const reader=response.body?.getReader();if(!reader)badRequest('Validation','Response body is empty');
  const parts=[];let size=0;
  try{
    while(true){const {done,value}=await reader.read();if(done)break;size+=value.byteLength;if(size>1048576)badRequest('Limit','JSON response exceeds 1 MiB');parts.push(value);if(signal.aborted)throw signal.reason;}
  }catch(e){await reader.cancel().catch(()=>{});throw e;}finally{reader.releaseLock();}
  const bytes=new Uint8Array(size);let offset=0;for(const part of parts){bytes.set(part,offset);offset+=part.length;}
  try{return JSON.parse(new TextDecoder('utf-8',{fatal:true}).decode(bytes));}catch{badRequest('Validation','Malformed JSON response');}
}
function waitRetry(ms,signal){return new Promise((resolve,reject)=>{
  const abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(signal.reason);};
  const timer=setTimeout(()=>{signal.removeEventListener('abort',abort);resolve();},ms);
  signal.addEventListener('abort',abort,{once:true});if(signal.aborted)abort();
});}
function acquireRequest(scope,spec,force){
  const url=requestURL(spec.url);
  const headers={'Accept':'application/json'};let body;
  if(spec.method!=='GET'){
    const token=document.querySelector('meta[name="nexo-csrf-token"]')?.content;
    if(!token||token.length>4096||/[^\x21-\x7e]/.test(token))badRequest('Security','A server-provided CSRF token is required');
    headers['X-CSRF-Token']=token;
    if(spec.bodySchema){const checked=validateJSON(spec.body,spec.bodySchema);body=JSON.stringify(checked,(_,v)=>typeof v==='bigint'?Number(v):v);headers['Content-Type']='application/json';if(new TextEncoder().encode(body).length>65536)badRequest('Limit','JSON request exceeds 64 KiB');}
  }
  const key=canonical({url,method:spec.method,schema:spec.schema,timeout:spec.timeout_ms,retries:spec.retries,cache:spec.cache_ms,epoch:scope.epoch});
  const now=Date.now();
  // Expired entries are removed and the cache is bounded independently of URL churn.
  for(const [k,v] of scope.cache)if(v.until<=now)scope.cache.delete(k);
  if(!force&&spec.method==='GET'&&spec.cache_ms&&scope.cache.has(key))return {promise:Promise.resolve(scope.cache.get(key).value),release(){}};
  const shared=spec.method==='GET'&&spec.dedupe;
  let entry=shared?scope.pending.get(key):null;
  if(!entry){
    const controller=new AbortController();const epoch=scope.epoch;
    entry={controller,refs:0,promise:null};
    const timer=setTimeout(()=>controller.abort(requestError('Timeout','Request timed out')),spec.timeout_ms);
    entry.promise=(async()=>{
      for(let attempt=0;;attempt++){
        if(controller.signal.aborted)throw controller.signal.reason;
        try{
          const response=await fetch(url,{method:spec.method,headers,body,signal:controller.signal,credentials:'same-origin',redirect:'error',cache:'no-store',referrerPolicy:'same-origin'});
          if(!response.ok){await response.body?.cancel();badRequest('Http','Request failed with HTTP '+response.status,response.status);}
          const value=validateJSON(await readJSON(response,controller.signal),spec.schema);
          if(spec.method==='GET'&&spec.cache_ms&&epoch===scope.epoch){
            if(scope.cache.size>=64)scope.cache.delete(scope.cache.keys().next().value);
            scope.cache.set(key,{value,until:Date.now()+spec.cache_ms});
          }
          return value;
        }catch(error){
          if(controller.signal.aborted)throw controller.signal.reason;
          const retryable=!error?.kind||(error.kind==='Http'&&(error.status===429n||error.status>=500n));
          if(spec.method!=='GET'||!retryable||attempt>=spec.retries)throw error?.kind?error:requestError('Network','Network request failed');
          await waitRetry(100*(2**attempt),controller.signal);
        }
      }
    })().finally(()=>{clearTimeout(timer);if(scope.pending.get(key)===entry)scope.pending.delete(key);});
    if(shared)scope.pending.set(key,entry);
  }
  entry.refs++;let released=false;
  return {promise:entry.promise,release(){if(released)return;released=true;if(--entry.refs===0){if(scope.pending.get(key)===entry)scope.pending.delete(key);entry.controller.abort(requestError('Cancelled','Request cancelled'));}}};
}
function createResource(scope,owner,dep,readSpec,kind,invalidate){
  let disposed=false,generation=0,active=null;
  const resource={loading:false,ready:false,success:false,data:null,error:null,reload:()=>start(true),run:()=>start(false),cancel};
  function publish(values){if(disposed)return;owner.batch(()=>{Object.assign(resource,values);owner.invalidate(dep);});}
  function cancel(){if(disposed)return;nexoObserve('request',{operation:kind,phase:'cancelled'});generation++;active?.release();active=null;publish({loading:false,ready:false,success:false,data:null,error:requestError('Cancelled','Request cancelled')});}
  async function start(force){
    if(disposed)return;
    if(kind==='mutation'&&active)return; // One explicit write at a time; never replay it.
    const current=++generation;active?.release();active=null;
    const started=performance.now();
    nexoObserve('request',{operation:kind,phase:'loading'});
    publish({loading:true,ready:false,success:false,error:null,data:null});
    let lease;
    try{
      lease=acquireRequest(scope,readSpec(),force);active=lease;
      const data=await lease.promise;
      if(disposed||current!==generation)return;
      if(kind==='mutation'){scope.invalidate();for(const reload of invalidate)reload();}
      nexoObserve('request',{operation:kind,phase:'ready',duration_ms:Math.round(performance.now()-started)});
      publish({loading:false,ready:true,success:kind==='mutation',data,error:null});
    }catch(error){
      if(!disposed&&current===generation){
        const failure=error?.kind?error:requestError('Network','Request could not be completed');
        nexoObserve('request',{operation:kind,phase:'error',code:failure.kind,status:Number(failure.status),duration_ms:Math.round(performance.now()-started)});
        publish({loading:false,ready:false,success:false,data:null,error:failure});
      }
    }finally{lease?.release();if(current===generation)active=null;}
  }
  // Initial reads honor an optional cache; reload deliberately bypasses it.
  if(kind==='load')resource.initial=()=>start(false);
  owner.cleanup(()=>{disposed=true;generation++;active?.release();active=null;});
  return resource;
}
