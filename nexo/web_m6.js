// Typed public-state boundary shared by hydration and explicit host adapters.
function m6Int(value){if(typeof value!=='bigint'||value<-(1n<<63n)||value>=(1n<<63n))throw new TypeError('Expected int64');return value;}
function m6Value(schema,value,wire=false,depth=0,budget={nodes:100000}){
  if(depth>32||--budget.nodes<0)throw new TypeError('State limit exceeded');
  if(schema.kind==='int'){
    if(wire){if(typeof value!=='string'||value.length>20||! /^-?(0|[1-9][0-9]*)$/.test(value))throw new TypeError('Expected decimal int64');value=BigInt(value);}
    else if(typeof value==='number'&&Number.isSafeInteger(value))value=BigInt(value);
    return m6Int(value);
  }
  if(schema.kind==='str'){
    if(typeof value!=='string'||value.includes('\0'))throw new TypeError('Expected string');
    for(const c of value){const n=c.codePointAt(0);if(n>=0xd800&&n<=0xdfff)throw new TypeError('Expected Unicode scalar string');}
    return value;
  }
  if(schema.kind==='bool'){if(typeof value!=='boolean')throw new TypeError('Expected bool');return value;}
  if(schema.kind==='list'){
    if(!Array.isArray(value)||value.length>10000)throw new TypeError('Expected bounded list');
    return Object.freeze(value.map(item=>m6Value(schema.item,item,wire,depth+1,budget)));
  }
  if(schema.kind!=='record'||!value||typeof value!=='object'||Array.isArray(value)||Object.keys(value).length!==Object.keys(schema.fields).length)throw new TypeError('Expected exact record');
  const out=Object.create(null);
  for(const [name,type]of Object.entries(schema.fields)){if(!Object.hasOwn(value,name))throw new TypeError('Missing record field');out[name]=m6Value(type,value[name],wire,depth+1,budget);}
  return Object.freeze(out);
}
function m6Patch(schema,patch,wire=false){
  if(!patch||typeof patch!=='object'||Array.isArray(patch))throw new TypeError('Expected state patch');
  const out=Object.create(null),budget={nodes:100000};
  for(const name of Object.keys(patch)){if(!Object.hasOwn(schema,name))throw new TypeError('Unknown state field');out[name]=m6Value(schema[name],patch[name],wire,0,budget);}
  if(new TextEncoder().encode(JSON.stringify(out,(_,v)=>typeof v==='bigint'?String(v):v)).length>1048576)throw new TypeError('State payload exceeds 1 MiB');
  return Object.freeze(out);
}
function m6Key(value){
  if(typeof value==='bigint')return 'i'+m6Int(value);
  if(typeof value==='string'){m6Value({kind:'str'},value);return 's'+[...new TextEncoder().encode(value)].map(n=>n.toString(16).padStart(2,'0')).join('');}
  throw new TypeError('Nexo list keys require unique int/str values');
}
function hydrationNavigate(path){location.assign(hydrationHref(path));}
function m6URL(value){
  if(typeof value!=='string'||!value.startsWith('/')||value.startsWith('//')||/[\\\u0000-\u0020\u007f]/.test(value))throw new TypeError('Expected same-origin local URL');
  const url=new URL(value,location.href);if(url.origin!==location.origin||url.search||url.hash||url.username||url.password)throw new TypeError('Expected same-origin local URL');return url;
}
async function m6Bytes(response,max,signal){
  if(!response.ok||Number(response.headers.get('Content-Length'))>max)throw new Error('Asset response rejected');
  const reader=response.body.getReader(),chunks=[];let size=0;
  try{while(true){if(signal.aborted)throw new DOMException('Aborted','AbortError');const {value,done}=await reader.read();if(done)break;size+=value.length;if(size>max)throw new Error('Asset exceeds size limit');chunks.push(value);}}
  catch(error){await reader.cancel();throw error;}
  const bytes=new Uint8Array(size);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}return bytes;
}
async function loadWasm(handle,url,{sha256,functions,timeoutMs=5000}={}){
  if(!handle.capabilities.wasm)throw new Error('WASM capability is not enabled');
  const asset=m6URL(url);if(!/^[0-9a-f]{64}$/i.test(sha256||'')||!functions||typeof functions!=='object'||!Object.keys(functions).length)throw new TypeError('WASM requires integrity and signatures');
  if(!Number.isInteger(timeoutMs)||timeoutMs<1||timeoutMs>30000)throw new TypeError('Invalid WASM timeout');
  const signatures=Object.create(null);
  for(const [name,signature]of Object.entries(functions)){
    if(!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)||!Array.isArray(signature.params)||signature.params.length>16||signature.params.some(type=>!['i32','i64','f32','f64'].includes(type))||!['i32','i64','f32','f64','void'].includes(signature.result))throw new TypeError('Invalid WASM signature');
    signatures[name]={params:[...signature.params],result:signature.result};
  }
  let worker=null,closed=false,next=0,loadTimer=null,unown=()=>{};const controller=new AbortController(),pending=new Map();
  const stop=()=>{if(closed)return;closed=true;clearTimeout(loadTimer);unown();controller.abort();if(worker){worker.onmessage=worker.onerror=null;worker.terminate();worker=null;}for(const item of pending.values()){clearTimeout(item.timer);item.reject(new Error('WASM worker closed'));}pending.clear();};
  unown=handle.onDestroy(stop);loadTimer=setTimeout(stop,timeoutMs);
  try{
    const response=await fetch(asset,{signal:controller.signal,credentials:'same-origin'});
    if(!(response.headers.get('Content-Type')||'').startsWith('application/wasm'))throw new Error('Expected WASM MIME type');
    const bytes=await m6Bytes(response,1048576,controller.signal);
    const hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(n=>n.toString(16).padStart(2,'0')).join('');
    if(hash!==sha256.toLowerCase()||closed)throw new Error('WASM integrity or lifetime check failed');
    worker=new Worker(hydrationWorkerURL,{type:'module'});
    worker.onmessage=event=>{const message=event.data,item=pending.get(message.id);if(!item)return;pending.delete(message.id);clearTimeout(item.timer);message.ok?item.resolve(message.value):item.reject(new Error('WASM validation or execution failed'));};
    worker.onerror=()=>stop();
    const send=(message,transfer=[])=>new Promise((resolve,reject)=>{
      if(closed||pending.size>=32){reject(new Error('WASM worker unavailable'));return;}
      const id=++next,timer=setTimeout(()=>stop(),timeoutMs);pending.set(id,{resolve,reject,timer});try{worker.postMessage({...message,id},transfer);}catch(error){pending.delete(id);clearTimeout(timer);reject(error);}
    });
    await send({kind:'init',bytes:bytes.buffer,functions:signatures},[bytes.buffer]);clearTimeout(loadTimer);
    return Object.freeze({call(name,...args){if(!Object.hasOwn(signatures,name))return Promise.reject(new TypeError('Unknown WASM export'));return send({kind:'call',name,args});},destroy:stop});
  }catch(error){stop();throw error;}
}
function connectEvents(handle,url,{schema,onValue=value=>handle.update(value),onError=()=>{},maxRetries=3}={}){
  if(!handle.capabilities.subscriptions)throw new Error('Subscription capability is not enabled');
  if(!schema||!Number.isInteger(maxRetries)||maxRetries<0||maxRetries>10)throw new TypeError('Subscription requires schema and bounded retries');
  const endpoint=m6URL(url),source=new EventSource(endpoint);let closed=false,errors=0,epoch=null,sequence=0n,unown=()=>{};
  const close=()=>{if(closed)return;closed=true;unown();source.onmessage=source.onerror=null;source.close();};unown=handle.onDestroy(close);
  source.onmessage=event=>{
    if(closed)return;
    try{
      const match=/^([0-9a-f]{32}):([1-9][0-9]{0,18})$/.exec(event.lastEventId);
      if(!match||new TextEncoder().encode(event.data).length>1048576)throw new TypeError('Invalid event envelope');
      const id=BigInt(match[2]);if(id>=(1n<<63n))throw new TypeError('Event id exceeds int64');if(match[1]===epoch&&id<=sequence)return;
      const value=m6Value(schema,JSON.parse(event.data),true);onValue(value);epoch=match[1];sequence=id;
    }catch{close();onError(new Error('Subscription payload rejected'));}
  };
  source.onerror=()=>{if(closed)return;errors++;try{onError(new Error('Subscription connection interrupted'));}finally{if(errors>maxRetries)close();}};
  return Object.freeze({close,get closed(){return closed;},get lastEventId(){return epoch?epoch+':'+sequence:'';}});
}
