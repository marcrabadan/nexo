// Nexo Web M1–M5: direct DOM ownership, typed values and synchronous batches.
function int(value) {
  if (value < -(1n << 63n) || value >= (1n << 63n)) throw new RangeError('Nexo int overflow');
  return value;
}
let batchDepth = 0, flushing = false, nextOwner = 0;
const pendingOwners = new Set();
function flushOwners() {
  if (batchDepth || flushing) return;
  flushing = true;
  try {
    while (pendingOwners.size) {
      // A parent invalidated while flushing must settle before its descendants.
      const owner = [...pendingOwners].sort((a,b) => a.id-b.id)[0];
      pendingOwners.delete(owner);
      owner.flush();
    }
  } finally { flushing = false; }
}
function batch(action) {
  batchDepth++;
  try { return action(); } finally { batchDepth--; flushOwners(); }
}
function createOwner() {
  let disposed = false;
  const effects = [], cleanups = [], dirty = new Set(), watchers = new Map();
  const owner = {
    id: nextOwner++,
    invalidate(name) {
      if (disposed) return;
      dirty.add(name);
      pendingOwners.add(owner);
      for (const callback of [...(watchers.get(name) || [])]) callback();
      flushOwners();
    },
    flush() {
      if (disposed) return;
      const changed = new Set(dirty);
      dirty.clear();
      for (const effect of effects) {
        if (effect.names.some(name => changed.has(name))) effect.update();
      }
    },
    subscribe(name, callback) {
      if (disposed) throw new Error('Context/owner already disposed');
      if (!watchers.has(name)) watchers.set(name,new Set());
      watchers.get(name).add(callback);
      return () => { const set=watchers.get(name); if(set){set.delete(callback);if(!set.size)watchers.delete(name);} };
    },
    link(name, source, field) { cleanups.push(source.subscribe(field, () => owner.invalidate(name))); },
    cleanup(callback) { cleanups.push(callback); },
    state(s) {
      return {
        set(name, value) {
          if (disposed || Object.is(s[name],value)) return;
          s[name] = value;
          owner.invalidate(name);
        },
        bind(names, read, write) {
          let initialized = false, previous;
          const update = () => {
            const next = read();
            if (initialized && Object.is(previous,next)) return;
            previous=next; initialized=true; write(next);
          };
          effects.push({names,update}); update();
        },
        listen(node,event,action) {
          const handler = () => { if(!disposed) batch(action); };
          node.addEventListener(event,handler);
          cleanups.push(() => node.removeEventListener(event,handler));
        }
      };
    },
    batch(action) { if(!disposed) return batch(action); },
    destroy() {
      if(disposed) return;
      disposed=true; pendingOwners.delete(owner);
      // Descendants/slots are disposed before their providers.
      for(const cleanup of cleanups.reverse()) cleanup();
      cleanups.length=effects.length=0;dirty.clear();watchers.clear();
    }
  };
  return owner;
}
function checkedProps(schema, props) {
  if(props===null || typeof props!=='object' || Array.isArray(props)) throw new TypeError('Expected a property object');
  for(const name of Object.keys(props)) if(!Object.hasOwn(schema,name)) throw new TypeError('Unknown property: '+name);
  const values=Object.create(null);
  for(const name of Object.keys(schema)) {
    const field=schema[name];
    let value;
    if(Object.hasOwn(props,name)) value=props[name];
    else if(field.default) value=field.default();
    else throw new TypeError('Missing property: '+name);
    const type=field.type==='int'?'bigint':field.type==='str'?'string':'boolean';
    if(typeof value!==type) throw new TypeError('Property '+name+' requires '+field.type);
    values[name]=type==='bigint'?int(value):value;
  }
  return values;
}
function injected(env,key) {
  if(!env.has(key)) throw new Error('Missing context provider');
  return env.get(key);
}
function routeHref(path) {
  if(typeof path!=='string' || path.length>8192 || !path.startsWith('/') || path.startsWith('//') || /[\\?#\u0000-\u001f\u007f]/.test(path)) throw new TypeError('Expected a local route path');
  return '#'+path;
}
function navigate(path) { window.location.hash=routeHref(path); }
// Conditional/list regions own every descendant and release it before replacement.
function createRegion(parent, owner) {
  const start=document.createComment('nexo'),end=document.createComment('/nexo');
  parent.append(start,end);let current=null;
  function clear(){
    if(current){current.destroy();current=null;}
    while(start.nextSibling&&start.nextSibling!==end)start.nextSibling.remove();
  }
  owner.cleanup(()=>{clear();start.remove();end.remove();});
  return {replace(render){
    clear();const next=createOwner(),fragment=document.createDocumentFragment();current=next;
    try{render(fragment,next);end.before(fragment);}catch(e){clear();throw e;}
  }};
}

function nexoObserve(){}

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

function _nw_d10137633b46a682(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({["endpoint"]: {type:"str", default: () => ("/api/products")},["writable"]: {type:"bool", default: () => true}},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-50906aabea7df467";
try{
const dataScope=useDataScope(env,owner);
r["products"]=createResource(dataScope,owner,"$products",()=>({method:"GET",schema:JSON.parse("{\"kind\": \"list\", \"item\": {\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}}"),url:s["endpoint"],timeout_ms:5000,retries:1,cache_ms:1000,dedupe:true}),"load",[]);
r["create"]=createResource(dataScope,owner,"$create",()=>({method:"POST",schema:JSON.parse("{\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}"),url:s["endpoint"],body:Object.freeze({["name"]:("Nexo handbook")}),bodySchema:JSON.parse("{\"kind\": \"record\", \"fields\": {\"name\": {\"kind\": \"str\"}}}"),timeout_ms:10000,retries:0,cache_ms:0,dedupe:true}),"mutation",[()=>r["products"].reload()]);
api.bind(["endpoint"],()=>({method:"GET",schema:JSON.parse("{\"kind\": \"list\", \"item\": {\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}}"),url:s["endpoint"],timeout_ms:5000,retries:1,cache_ms:1000,dedupe:true}).url,()=>r["products"].initial());
const _node1=document.createElement("main");
root.append(_node1);
const _node2=document.createElement("h1");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
_text3.data=String(("Nexo Web \u2014 typed data"));
const _region4=createRegion(_node1,owner);
api.bind(["$products"],()=>(r["products"].loading?0:((r["products"].error!==null)?1:(r["products"].ready?2:-1))),choice=>_region4.replace((_host7,_owner5)=>{const _api6=_owner5.state(s);
_owner5.link("endpoint",owner,"endpoint");
_owner5.link("writable",owner,"writable");
_owner5.link("$products",owner,"$products");
_owner5.link("$create",owner,"$create");
if(choice===0){
const _node8=document.createElement("p");
_host7.append(_node8);
const _text9=document.createTextNode(""); _node8.append(_text9);
_text9.data=String(("Loading..."));
{const v=String(("status"));_node8.setAttribute("role",v);}
}
if(choice===1){
const _node10=document.createElement("p");
_host7.append(_node10);
const _text11=document.createTextNode(""); _node10.append(_text11);
_api6.bind(["$products"],()=>String((String(r["products"].error["kind"])+": "+String(r["products"].error["message"]))),v=>{_text11.data=v;});
{const v=String(("alert"));_node10.setAttribute("role",v);}
const _node12=document.createElement("button");
_node12.type="button";
_host7.append(_node12);
const _text13=document.createTextNode(""); _node12.append(_text13);
_text13.data=String(("Retry"));
_api6.listen(_node12,"click",r["products"].reload);
}
if(choice===2){
const _node14=document.createElement("ul");
_host7.append(_node14);
const _region15=createRegion(_node14,_owner5);
_api6.bind(["$products"],()=>r["products"].data,items=>_region15.replace((_host18,_owner16)=>{const _api17=_owner16.state(s);
_owner16.link("endpoint",_owner5,"endpoint");
_owner16.link("writable",_owner5,"writable");
_owner16.link("$products",_owner5,"$products");
_owner16.link("$create",_owner5,"$create");
for(const _item36 of items){
const _node19=document.createElement("li");
_host18.append(_node19);
const _text20=document.createTextNode(""); _node19.append(_text20);
_text20.data=String(_item36["name"]);
}
}));
}
}));
const _node21=document.createElement("button");
_node21.type="button";
_node1.append(_node21);
const _text22=document.createTextNode(""); _node21.append(_text22);
_text22.data=String(("Reload"));
api.listen(_node21,"click",r["products"].reload);
const _node23=document.createElement("button");
_node23.type="button";
_node1.append(_node23);
const _text24=document.createTextNode(""); _node23.append(_text24);
_text24.data=String(("Cancel read"));
api.listen(_node23,"click",r["products"].cancel);
const _node25=document.createElement("button");
_node25.type="button";
_node1.append(_node25);
const _text26=document.createTextNode(""); _node25.append(_text26);
_text26.data=String(("Create product"));
api.listen(_node25,"click",r["create"].run);
api.bind(["$create", "writable"],()=>(!(s["writable"])||r["create"].loading),v=>{_node25.disabled=v;});
const _region27=createRegion(_node1,owner);
api.bind(["$create"],()=>(r["create"].loading?0:((r["create"].error!==null)?1:(r["create"].success?2:-1))),choice=>_region27.replace((_host30,_owner28)=>{const _api29=_owner28.state(s);
_owner28.link("endpoint",owner,"endpoint");
_owner28.link("writable",owner,"writable");
_owner28.link("$products",owner,"$products");
_owner28.link("$create",owner,"$create");
if(choice===0){
const _node31=document.createElement("p");
_host30.append(_node31);
const _text32=document.createTextNode(""); _node31.append(_text32);
_text32.data=String(("Saving..."));
{const v=String(("status"));_node31.setAttribute("role",v);}
}
if(choice===1){
const _node33=document.createElement("p");
_host30.append(_node33);
const _text34=document.createTextNode(""); _node33.append(_text34);
_api29.bind(["$create"],()=>String((String(r["create"].error["kind"])+": "+String(r["create"].error["message"]))),v=>{_text34.data=v;});
{const v=String(("alert"));_node33.setAttribute("role",v);}
}
if(choice===2){
const _node35=document.createElement("p");
_host30.append(_node35);
const _text36=document.createTextNode(""); _node35.append(_text36);
_api29.bind(["$create"],()=>String(("Created: "+String(r["create"].data["name"]))),v=>{_text36.data=v;});
{const v=String(("status"));_node35.setAttribute("role",v);}
}
}));
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["endpoint"]: {type:"str", default: () => ("/api/products")},["writable"]: {type:"bool", default: () => true}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
export { _nw_d10137633b46a682 as App };
