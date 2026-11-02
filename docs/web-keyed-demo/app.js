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

// Explicit keys retain row owners, DOM identity and local component state.
function createKeyedRegion(parent,owner,dependency,keyOf,render) {
  const end=document.createComment('/nexo-keyed');parent.append(end);
  const entries=new Map();
  function remove(entry){entry.owner.destroy();let node=entry.start;while(node){const next=node.nextSibling;node.remove();if(node===entry.end)break;node=next;}}
  owner.cleanup(()=>{for(const entry of entries.values())remove(entry);entries.clear();end.remove();});
  return {update(items){
    const focused=document.activeElement;
    // Reject ambiguous keys before changing the last good list.
    const seen=new Set(),planned=items.map(value=>{
      const key=keyOf({value});
      if(!['string','bigint'].includes(typeof key)||seen.has(key))throw new TypeError('Nexo list keys must be unique int/str values');
      seen.add(key);return {key,value};
    });
    batch(()=>{
      for(const [key,entry] of entries)if(!seen.has(key)){remove(entry);entries.delete(key);}
      for(const {key,value} of planned){
        let entry=entries.get(key);
        if(!entry){
          const row={value},child=createOwner(),fragment=document.createDocumentFragment();
          const start=document.createComment('nexo-key'),finish=document.createComment('/nexo-key');fragment.append(start);
          try{render(fragment,child,row);fragment.append(finish);end.before(fragment);}
          catch(error){child.destroy();throw error;}
          entry={row,owner:child,start,end:finish};entries.set(key,entry);
        }else if(!Object.is(entry.row.value,value)){
          entry.row.value=value;entry.owner.invalidate(dependency);
        }
      }
      // Move entire row ranges; listeners and descendant ownership stay intact.
      let cursor=end;
      for(let i=planned.length-1;i>=0;i--){
        const entry=entries.get(planned[i].key);
        if(entry.end.nextSibling!==cursor){
          const fragment=document.createDocumentFragment();let node=entry.start;
          while(node){const next=node.nextSibling;fragment.append(node);if(node===entry.end)break;node=next;}
          cursor.before(fragment);
        }
        cursor=entry.start;
      }
      if(focused?.isConnected&&document.activeElement!==focused)focused.focus({preventScroll:true});
    });
  }};
}

function _nw_728430d3d6b4d55a(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({["label"]: {type:"str"}},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-b035639197b0d94e";
try{
s["count"]=0n;
const a_increment=()=>owner.batch(()=>{
api.set("count", int(s["count"]+(1n)));
});
const _node1=document.createElement("div");
root.append(_node1);
const _node2=document.createElement("span");
_node1.append(_node2);
const _text3=document.createTextNode(""); _node2.append(_text3);
api.bind(["label"],()=>String(s["label"]),v=>{_text3.data=v;});
const _node4=document.createElement("button");
_node4.type="button";
_node1.append(_node4);
const _text5=document.createTextNode(""); _node4.append(_text5);
api.bind(["count"],()=>String(("Count: "+String(s["count"]))),v=>{_text5.data=v;});
api.listen(_node4,"click",a_increment);
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({["label"]: {type:"str"}},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
function _nw_d10137633b46a682(target,props={},parentEnv=new Map(),renderSlot=null){
const s=checkedProps({},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;
const root=document.createElement("div"); root.dataset.nexo="nw-0cd4f53c789ea499";
try{
s["query"]=("");
s["selected"]=false;
s["products"]=Object.freeze([Object.freeze({["id"]:1n,["name"]:("One")}),Object.freeze({["id"]:2n,["name"]:("Two")})]);
const a_edit=(event)=>owner.batch(()=>{
api.set("query", event["value"]);
});
const a_select=(event)=>owner.batch(()=>{
api.set("selected", event["checked"]);
});
const a_reorder=()=>owner.batch(()=>{
api.set("products", Object.freeze([Object.freeze({["id"]:2n,["name"]:("Two updated")}),Object.freeze({["id"]:1n,["name"]:("One")})]));
});
const a_remove=()=>owner.batch(()=>{
api.set("products", Object.freeze([Object.freeze({["id"]:2n,["name"]:("Two")})]));
});
const a_restore=()=>owner.batch(()=>{
api.set("products", Object.freeze([Object.freeze({["id"]:1n,["name"]:("One")}),Object.freeze({["id"]:2n,["name"]:("Two")})]));
});
const _node1=document.createElement("main");
root.append(_node1);
appendStatic(_node1,"<h1>Keyed list and typed events</h1>");
appendStatic(_node1,"<label for=\"query\">Filter</label>");
const _node2=document.createElement("input");
_node1.append(_node2);
{const v=String(("query"));_node2.setAttribute("id",v);}
api.bind(["query"],()=>String(s["query"]),v=>{_node2.value=v;});
api.listen(_node2,"input",a_edit,true);
const _node3=document.createElement("output");
_node1.append(_node3);
const _text4=document.createTextNode(""); _node3.append(_text4);
api.bind(["query"],()=>String(s["query"]),v=>{_text4.data=v;});
{const v=String(("status"));_node3.setAttribute("role",v);}
appendStatic(_node1,"<label for=\"selected\">Selected</label>");
const _node5=document.createElement("input");
_node1.append(_node5);
{const v=String(("selected"));_node5.setAttribute("id",v);}
{const v=String(("checkbox"));_node5.setAttribute("type",v);}
api.bind(["selected"],()=>s["selected"],v=>{_node5.checked=v;});
api.listen(_node5,"change",a_select,true);
const _node6=document.createElement("p");
_node1.append(_node6);
const _text7=document.createTextNode(""); _node6.append(_text7);
api.bind(["selected"],()=>String(("Selected: "+String(s["selected"]))),v=>{_text7.data=v;});
const _node8=document.createElement("button");
_node8.type="button";
_node1.append(_node8);
const _text9=document.createTextNode(""); _node8.append(_text9);
_text9.data=String(("Reorder"));
api.listen(_node8,"click",a_reorder);
const _node10=document.createElement("button");
_node10.type="button";
_node1.append(_node10);
const _text11=document.createTextNode(""); _node10.append(_text11);
_text11.data=String(("Remove"));
api.listen(_node10,"click",a_remove);
const _node12=document.createElement("button");
_node12.type="button";
_node1.append(_node12);
const _text13=document.createTextNode(""); _node12.append(_text13);
_text13.data=String(("Restore"));
api.listen(_node12,"click",a_restore);
const _node14=document.createElement("ul");
_node1.append(_node14);
const _keyed15=createKeyedRegion(_node14,owner,"$item51",(_row51)=>_row51.value["id"],(_host16,_owner17,_row51)=>{const _api18=_owner17.state(s);
_owner17.link("query",owner,"query");
_owner17.link("selected",owner,"selected");
_owner17.link("products",owner,"products");
const _node19=document.createElement("li");
_host16.append(_node19);
_api18.bind(["$item51"],()=>String(("row-"+String(_row51.value["id"]))),v=>{_node19.setAttribute("id",v);});
const _child20=_nw_728430d3d6b4d55a(_node19,({["label"]: _row51.value["name"]}),env,null); _owner17.cleanup(()=>_child20.destroy());
_api18.bind(["$item51"],()=>({["label"]: _row51.value["name"]}),v=>_child20.update(v));
});api.bind(["products"],()=>s["products"],items=>_keyed15.update(items));
target.append(root);
return {element:root,env,outlet,update(props){const next=checkedProps({},props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};
}catch(e){owner.destroy();root.remove();throw e;} }
export { _nw_d10137633b46a682 as App };
