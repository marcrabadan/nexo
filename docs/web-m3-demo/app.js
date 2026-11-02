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
        listen(node,event,action,withEvent=false) {
          const handler = event => {
            if(disposed)return;
            if(!withEvent){batch(action);return;}
            const target=event.currentTarget;
            const value=Object.freeze({value:typeof target.value==='string'?target.value:'',checked:target.checked===true});
            batch(()=>action(value));
          };
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
// Only compiler-escaped constant markup reaches this helper; never runtime data.
const staticTemplates=new Map();
function appendStatic(parent,markup){
  let template=staticTemplates.get(markup);
  if(!template){template=document.createElement('template');template.innerHTML=markup;staticTemplates.set(markup,template);}
  parent.append(template.content.cloneNode(true));
}
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
const root=document.createElement("div"); root.dataset.nexo="nw-b0c39178dffe9945";
try{
const dataScope=useDataScope(env,owner);
r["products"]=createResource(dataScope,owner,"$products",()=>({method:"GET",schema:JSON.parse("{\"kind\": \"list\", \"item\": {\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}}"),url:s["endpoint"],timeout_ms:5000,retries:1,cache_ms:1000,dedupe:true}),"load",[]);
r["create"]=createResource(dataScope,owner,"$create",()=>({method:"POST",schema:JSON.parse("{\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}"),url:s["endpoint"],body:Object.freeze({["name"]:("Nexo handbook")}),bodySchema:JSON.parse("{\"kind\": \"record\", \"fields\": {\"name\": {\"kind\": \"str\"}}}"),timeout_ms:10000,retries:0,cache_ms:0,dedupe:true}),"mutation",[()=>r["products"].reload()]);
api.bind(["endpoint"],()=>({method:"GET",schema:JSON.parse("{\"kind\": \"list\", \"item\": {\"kind\": \"record\", \"fields\": {\"id\": {\"kind\": \"int\"}, \"name\": {\"kind\": \"str\"}}}}"),url:s["endpoint"],timeout_ms:5000,retries:1,cache_ms:1000,dedupe:true}).url,()=>r["products"].initial());
const _node1=document.createElement("main");
root.append(_node1);
appendStatic(_node1,"<h1>Nexo Web \u2014 typed data</h1>");
const _region2=createRegion(_node1,owner);
api.bind(["$products"],()=>(r["products"].loading?0:((r["products"].error!==null)?1:(r["products"].ready?2:-1))),choice=>_region2.replace((_host5,_owner3)=>{const _api4=_owner3.state(s);
_owner3.link("endpoint",owner,"endpoint");
_owner3.link("writable",owner,"writable");
_owner3.link("$products",owner,"$products");
_owner3.link("$create",owner,"$create");
if(choice===0){
appendStatic(_host5,"<p role=\"status\">Loading...</p>");
}
if(choice===1){
const _node6=document.createElement("p");
_host5.append(_node6);
const _text7=document.createTextNode(""); _node6.append(_text7);
_api4.bind(["$products"],()=>String((String(r["products"].error["kind"])+": "+String(r["products"].error["message"]))),v=>{_text7.data=v;});
{const v=String(("alert"));_node6.setAttribute("role",v);}
const _node8=document.createElement("button");
_node8.type="button";
_host5.append(_node8);
const _text9=document.createTextNode(""); _node8.append(_text9);
_text9.data=String(("Retry"));
_api4.listen(_node8,"click",r["products"].reload);
}
if(choice===2){
const _node10=document.createElement("ul");
_host5.append(_node10);
const _region11=createRegion(_node10,_owner3);
_api4.bind(["$products"],()=>r["products"].data,items=>_region11.replace((_host14,_owner12)=>{const _api13=_owner12.state(s);
_owner12.link("endpoint",_owner3,"endpoint");
_owner12.link("writable",_owner3,"writable");
_owner12.link("$products",_owner3,"$products");
_owner12.link("$create",_owner3,"$create");
for(const _item36 of items){
const _node15=document.createElement("li");
_host14.append(_node15);
const _text16=document.createTextNode(""); _node15.append(_text16);
_text16.data=String(_item36["name"]);
}
}));
}
}));
const _node17=document.createElement("button");
_node17.type="button";
_node1.append(_node17);
const _text18=document.createTextNode(""); _node17.append(_text18);
_text18.data=String(("Reload"));
api.listen(_node17,"click",r["products"].reload);
const _node19=document.createElement("button");
_node19.type="button";
_node1.append(_node19);
const _text20=document.createTextNode(""); _node19.append(_text20);
_text20.data=String(("Cancel read"));
api.listen(_node19,"click",r["products"].cancel);
const _node21=document.createElement("button");
_node21.type="button";
_node1.append(_node21);
const _text22=document.createTextNode(""); _node21.append(_text22);
_text22.data=String(("Create product"));
api.listen(_node21,"click",r["create"].run);
api.bind(["$create", "writable"],()=>(!(s["writable"])||r["create"].loading),v=>{_node21.disabled=v;});
const _region23=createRegion(_node1,owner);
api.bind(["$create"],()=>(r["create"].loading?0:((r["create"].error!==null)?1:(r["create"].success?2:-1))),choice=>_region23.replace((_host26,_owner24)=>{const _api25=_owner24.state(s);
_owner24.link("endpoint",owner,"endpoint");
_owner24.link("writable",owner,"writable");
_owner24.link("$products",owner,"$products");
_owner24.link("$create",owner,"$create");
if(choice===0){
appendStatic(_host26,"<p role=\"status\">Saving...</p>");
}
if(choice===1){
const _node27=document.createElement("p");
_host26.append(_node27);
const _text28=document.createTextNode(""); _node27.append(_text28);
_api25.bind(["$create"],()=>String((String(r["create"].error["kind"])+": "+String(r["create"].error["message"]))),v=>{_text28.data=v;});
{const v=String(("alert"));_node27.setAttribute("role",v);}
}
if(choice===2){
const _node29=document.createElement("p");
_host26.append(_node29);
const _text30=document.createTextNode(""); _node29.append(_text30);
_api25.bind(["$create"],()=>String(("Created: "+String(r["create"].data["name"]))),v=>{_text30.data=v;});
{const v=String(("status"));_node29.setAttribute("role",v);}
}
}));
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["endpoint"]: {type:"str", default: () => ("/api/products")},["writable"]: {type:"bool", default: () => true}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
export { _nw_d10137633b46a682 as App };
