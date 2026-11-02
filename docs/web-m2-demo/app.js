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

function _nw_e9c1d14ae985fc95(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
try{
s["count"]=0n;
const a_increment=()=>owner.batch(()=>{
api.set("count", int(s["count"]+(1n)));
});
return {values:s,actions:{["increment"]:a_increment},subscribe:owner.subscribe,destroy:()=>owner.destroy()};
}catch(e){owner.destroy(); throw e;} }
function _nw_16d0639fd57925d4(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({["heading"]: {type:"str"}},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-f5b95dd571591718";
try{
const _node1=document.createElement("article");
root.append(_node1);
{const v=String(("card"));_node1.setAttribute("class",v);}
const _node2=document.createElement("h2");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["heading"],()=>String(s["heading"]),v=>{_text3.data=v;});
const _outlet4=document.createElement("div"); _node1.append(_outlet4); outlet=_outlet4;
if(renderSlot){ const content=renderSlot(_outlet4); owner.cleanup(()=>content.destroy()); }
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["heading"]: {type:"str"}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_7a89dfcabf245f9c(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({["label"]: {type:"str"},["start"]: {type:"int", default: () => 0n}},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-f5a54a7973eae228";
try{
s["count"]=s["start"];
const a_increment=()=>owner.batch(()=>{
api.set("count", int(s["count"]+(1n)));
});
const _node1=document.createElement("section");
root.append(_node1);
const _node2=document.createElement("p");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["count", "label"],()=>String((String(s["label"])+": "+String(s["count"]))),v=>{_text3.data=v;});
const _node4=document.createElement("button");
_node4.type="button";
_node1.append(_node4);
const _text5=document.createTextNode(""); _node4.append(_text5);
_text5.data=String(("Local +"));
api.listen(_node4,"click",a_increment);
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["label"]: {type:"str"},["start"]: {type:"int", default: () => 0n}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_856f187f9f88710f(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-18de891966daa744";
try{
{const provided=_nw_e9c1d14ae985fc95(); env.set("_nw_e9c1d14ae985fc95",provided); owner.cleanup(()=>provided.destroy());}
x["visits"]=injected(env,"_nw_e9c1d14ae985fc95");
owner.link("@visits.count",x["visits"],"count");
s["opens"]=0n;
const a_open=()=>owner.batch(()=>{
api.set("opens", int(s["opens"]+(1n)));
});
const _node1=document.createElement("header");
root.append(_node1);
appendStatic(_node1,"<h1>Nexo Web M2</h1>");
const _node2=document.createElement("p");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["@visits.count", "opens"],()=>String(("Shared visits: "+String(x["visits"].values["count"])+"; layout clicks: "+String(s["opens"]))),v=>{_text3.data=v;});
const _node4=document.createElement("button");
_node4.type="button";
_node1.append(_node4);
const _text5=document.createTextNode(""); _node4.append(_text5);
_text5.data=String(("Layout +"));
api.listen(_node4,"click",a_open);
const _node6=document.createElement("nav");
_node1.append(_node6);
const _node7=document.createElement("a");
_node6.append(_node7);
const _text8=document.createTextNode(""); _node7.append(_text8);
_text8.data=String(("Home"));
{const v=routeHref(("/"));_node7.setAttribute("href",v);}
const _node9=document.createElement("a");
_node6.append(_node9);
const _text10=document.createTextNode(""); _node9.append(_text10);
_text10.data=String(("User 7"));
{const v=routeHref(("/users/7"));_node9.setAttribute("href",v);}
const _outlet11=document.createElement("div"); root.append(_outlet11); outlet=_outlet11;
if(renderSlot){ const content=renderSlot(_outlet11); owner.cleanup(()=>content.destroy()); }
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_d5f423e9782b0a43(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-e0ccc30471b19863";
try{
s["opens"]=0n;
const a_open=()=>owner.batch(()=>{
api.set("opens", int(s["opens"]+(1n)));
});
const _node1=document.createElement("section");
root.append(_node1);
appendStatic(_node1,"<h2>Persistent nested layout</h2>");
const _node2=document.createElement("p");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["opens"],()=>String(("Nested layout clicks: "+String(s["opens"]))),v=>{_text3.data=v;});
const _node4=document.createElement("button");
_node4.type="button";
_node1.append(_node4);
const _text5=document.createTextNode(""); _node4.append(_text5);
_text5.data=String(("Nested +"));
api.listen(_node4,"click",a_open);
const _outlet6=document.createElement("div"); _node1.append(_outlet6); outlet=_outlet6;
if(renderSlot){ const content=renderSlot(_outlet6); owner.cleanup(()=>content.destroy()); }
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_c0fb999e591baaae(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-cea61bbe918a90be";
try{
x["visits"]=injected(env,"_nw_e9c1d14ae985fc95");
owner.link("@visits.count",x["visits"],"count");
s["label"]=("Local count");
const a_rename=()=>owner.batch(()=>{
api.set("label", ("Updated count"));
});
const _node1=document.createElement("main");
root.append(_node1);
const _slot2=(host)=>{const _owner3=createOwner(); const _api4=_owner3.state(s); const _root5=document.createElement("div"); _root5.dataset.nexo="nw-cea61bbe918a90be"; try{
_owner3.link("@visits.count",owner,"@visits.count");
_owner3.link("label",owner,"label");
const _node6=document.createElement("p");
_root5.append(_node6);
const _text7=document.createTextNode(""); _node6.append(_text7);
_api4.bind(["@visits.count"],()=>String(("Shared in slot: "+String(x["visits"].values["count"]))),v=>{_text7.data=v;});
const _node8=document.createElement("button");
_node8.type="button";
_root5.append(_node8);
const _text9=document.createTextNode(""); _node8.append(_text9);
_text9.data=String(("Shared +"));
_api4.listen(_node8,"click",x["visits"].actions["increment"]);
const _node10=document.createElement("button");
_node10.type="button";
_root5.append(_node10);
const _text11=document.createTextNode(""); _node10.append(_text11);
_text11.data=String(("Rename"));
_api4.listen(_node10,"click",a_rename);
const _child12=_nw_7a89dfcabf245f9c(_root5,({["label"]: s["label"]}),env,null); _owner3.cleanup(()=>_child12.destroy());
_api4.bind(["label"],()=>({["label"]: s["label"]}),v=>_child12.update(v));
host.append(_root5); return {destroy(){_owner3.destroy(); _root5.remove();}}; }catch(e){_owner3.destroy(); _root5.remove(); throw e;} };
const _child13=_nw_16d0639fd57925d4(_node1,({["heading"]: s["label"]}),env,_slot2); owner.cleanup(()=>_child13.destroy());
api.bind(["label"],()=>({["heading"]: s["label"]}),v=>_child13.update(v));
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_8a074904e9bfd7d6(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({["id"]: {type:"int"}},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-47d6b64bf965d2dc";
try{
x["visits"]=injected(env,"_nw_e9c1d14ae985fc95");
owner.link("@visits.count",x["visits"],"count");
const _node1=document.createElement("main");
root.append(_node1);
const _node2=document.createElement("h2");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["id"],()=>String(("User "+String(s["id"]))),v=>{_text3.data=v;});
const _node4=document.createElement("p");
_node1.append(_node4);
const _text5=document.createTextNode(""); _node4.append(_text5);
api.bind(["@visits.count"],()=>String(("Shared on another page: "+String(x["visits"].values["count"]))),v=>{_text5.data=v;});
const _node6=document.createElement("button");
_node6.type="button";
_node1.append(_node6);
const _text7=document.createTextNode(""); _node6.append(_text7);
_text7.data=String(("Shared +"));
api.listen(_node6,"click",x["visits"].actions["increment"]);
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["id"]: {type:"int"}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_0d578415a6de1f54(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-5a6c768eb367b31b";
try{
const _node1=document.createElement("main");
root.append(_node1);
appendStatic(_node1,"<h2>Page not found</h2>");
const _node2=document.createElement("a");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
_text3.data=String(("Return home"));
{const v=routeHref(("/"));_node2.setAttribute("href",v);}
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_afc8ac8d9e84d15b(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-7ddb6a249ff7fab4";
try{
appendStatic(root,"<main><h2>Unable to render this page</h2></main>");
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
export { _nw_856f187f9f88710f as Shell };
export { _nw_d5f423e9782b0a43 as Details };
export { _nw_c0fb999e591baaae as Home };
export { _nw_8a074904e9bfd7d6 as User };
export { _nw_0d578415a6de1f54 as NotFound };
export { _nw_afc8ac8d9e84d15b as Failure };
// Static-host-compatible fragment router. No network, HTML injection or globals.
function createRouter(target, routes, fallbacks, rootEnv=new Map()) {
  let disposed=false, current=null, page=null, layouts=[], fallback=null;
  const initialTitle=document.title;
  const initialDescription=document.querySelector('meta[name="description"]');
  const initialContent=initialDescription?.getAttribute('content');
  let description=initialDescription;
  function metadata(values) {
    document.title=values.title ?? initialTitle;
    if(values.description!==undefined || initialDescription) {
      if(!description) {description=document.createElement('meta');description.name='description';document.head.append(description);}
      description.content=values.description ?? initialContent ?? '';
    } else if(description) {description.remove();description=null;}
  }
  function clearPage() {if(page){page.destroy();page=null;}if(fallback){fallback.remove();fallback=null;}}
  function clearLayouts(keep=0) {while(layouts.length>keep)layouts.pop().handle.destroy();}
  function match(path) {
    if(!path.startsWith('/') || path.length>8192 || /[\\?#\u0000-\u001f\u007f]/.test(path))return null;
    if(path.length>1)path=path.replace(/\/+$/,'');
    let segments;
    try {
      segments=path==='/'?[]:path.slice(1).split('/').map(s=>decodeURIComponent(s));
      if(segments.some(s=>!s || /[\/\\\u0000-\u001f\u007f]/.test(s)))return null;
    } catch {return null;}
    for(const route of routes) {
      if(route.parts.length!==segments.length)continue;
      const props=Object.create(null);let valid=true;
      for(let i=0;i<segments.length;i++) {
        const part=route.parts[i], value=segments[i];
        if(Object.hasOwn(part,'literal')) {if(part.literal!==value){valid=false;break;}}
        else if(part.type==='str')props[part.name]=value;
        else if(part.type==='bool') {if(!['true','false'].includes(value)){valid=false;break;}props[part.name]=value==='true';}
        else {
          if(!/^-?(0|[1-9][0-9]*)$/.test(value) || value.length>20){valid=false;break;}
          try {props[part.name]=int(BigInt(value));} catch {valid=false;break;}
        }
      }
      if(valid)return {route,props};
    }
    return null;
  }
  function renderRoute(route,props) {
    let keep=0;
    while(keep<layouts.length && keep<route.layouts.length && layouts[keep].id===route.layouts[keep].id)keep++;
    clearPage();clearLayouts(keep);
    let host=keep?layouts[keep-1].handle.outlet:target;
    let env=keep?layouts[keep-1].handle.env:rootEnv;
    for(let i=keep;i<route.layouts.length;i++) {
      const item=route.layouts[i];
      const handle=item.create(host,{},env);
      layouts.push({id:item.id,handle});host=handle.outlet;env=handle.env;
    }
    page=route.create(host,props,env);
    metadata(route.meta(props));
    const focus=page.element.querySelector("main,h1,h2") || page.element;
    if(!focus.hasAttribute("tabindex"))focus.tabIndex=-1;
    focus.focus();
    nexoObserve('navigation',{phase:'ready'});
  }
  function message(text) {
    fallback=document.createElement('p');fallback.setAttribute('role','alert');fallback.textContent=text;target.append(fallback);
    metadata({});
  }
  function render() {
    if(disposed)return;
    const path=window.location.hash.slice(1)||'/';
    if(current===path)return;
    current=path;
    nexoObserve('navigation',{phase:'started'});
    try {
      const found=match(path);
      if(found)renderRoute(found.route,found.props);
      else if(fallbacks.not_found)renderRoute(fallbacks.not_found,{});
      else {clearPage();clearLayouts();message('Page not found.');}
    } catch {
      nexoObserve('navigation',{phase:'error'});
      clearPage();clearLayouts();
      if(fallbacks.error) {
        try {renderRoute(fallbacks.error,{});return;} catch {clearPage();clearLayouts();}
      }
      message('Unable to render this page.');
    }
  }
  window.addEventListener('hashchange',render);
  render();
  return {
    navigate(path){if(!disposed)navigate(path);},
    destroy(){
      if(disposed)return;
      disposed=true;window.removeEventListener('hashchange',render);
      clearPage();clearLayouts();document.title=initialTitle;
      if(initialDescription)initialDescription.setAttribute('content',initialContent ?? '');
      else if(description)description.remove();
    }
  };
}

export function mountApp(target,env=new Map()){return createRouter(target,[{create:_nw_8a074904e9bfd7d6,layouts:[{id:"_nw_856f187f9f88710f",create:_nw_856f187f9f88710f},{id:"_nw_d5f423e9782b0a43",create:_nw_d5f423e9782b0a43}],meta:(s)=>({title:("Nexo Web \u2014 User "+String(s["id"]))}),"path":"/users/{id}","parts":[{"literal": "users"}, {"name": "id", "type": "int"}],"score":1},{create:_nw_c0fb999e591baaae,layouts:[{id:"_nw_856f187f9f88710f",create:_nw_856f187f9f88710f}],meta:(s)=>({title:("Nexo Web \u2014 Home"),description:("A compiled Nexo component, layout and context example.")}),"path":"/","parts":[],"score":0}],{not_found:{create:_nw_0d578415a6de1f54,layouts:[{id:"_nw_856f187f9f88710f",create:_nw_856f187f9f88710f}],meta:(s)=>({title:("Nexo Web \u2014 Not found")})},error:{create:_nw_afc8ac8d9e84d15b,layouts:[],meta:(s)=>({title:("Nexo Web \u2014 Error")})}},env);}
