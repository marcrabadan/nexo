// RFC-0005/0006: verify SSR trees and own reactive conditional lifetimes.
const hydratedRoots = new WeakMap();
function hydrationMismatch() { throw new Error('NW6005: hydration mismatch'); }
function hydrationSession(creating=false) {
  const owners=[], commits=[], regions=new Set();
  let nodes=new WeakSet();
  const cleanups=[];
  return {
    entryState:Object.create(null),entry:null,
    cleanup(callback){let active=true;const run=()=>{if(active){active=false;callback();}};cleanups.push(run);return ()=>{if(!active)return;active=false;const index=cleanups.indexOf(run);if(index>=0)cleanups.splice(index,1);};},
    mark(node){nodes.add(node);return node;},
    cursor(parent,next=parent.firstChild,stop=null) { return {parent,next,stop}; },
    owner() { const owner=createOwner();owners.push(owner);return owner; },
    element(cursor,tag,attributes) {
      if(creating){
        const node=document.createElement(tag);
        for(const [key,value] of Object.entries(attributes))if(value!==false)node.setAttribute(key,value===true?'':String(value));
        nodes.add(node);cursor.parent.append(node);return node;
      }
      const node=cursor.next;
      if(!node || node.nodeType!==1 || node.namespaceURI!=='http://www.w3.org/1999/xhtml' || node.localName!==tag)hydrationMismatch();
      const expected=Object.entries(attributes).filter(([,value])=>value!==false);
      if(node.attributes.length!==expected.length)hydrationMismatch();
      for(const [key,value] of expected)if(node.getAttribute(key)!==(value===true?'':String(value)))hydrationMismatch();
      nodes.add(node);cursor.next=node.nextSibling;return node;
    },
    text(cursor,value) {
      if(creating){const node=document.createTextNode(value);nodes.add(node);cursor.parent.append(node);return node;}
      let node=cursor.next;
      if(node && node.nodeType===3) {
        if(node.data!==value)hydrationMismatch();nodes.add(node);cursor.next=node.nextSibling;return node;
      }
      if(value!=='')hydrationMismatch();
      const before=cursor.next,parent=cursor.parent;
      node=document.createTextNode('');nodes.add(node);commits.push(()=>parent.insertBefore(node,before));return node;
    },
    end(cursor) { if(cursor.next!==cursor.stop)hydrationMismatch(); },
    region(cursor,owner,id,read,render) {
      const choice=read();let start,end;
      if(creating){
        start=document.createComment('nexo-if:'+id+':'+choice);end=document.createComment('/nexo-if:'+id);
        cursor.parent.append(start,end);
      }else{
        start=cursor.next;
        if(!start || start.nodeType!==8 || start.data!=='nexo-if:'+id+':'+choice)hydrationMismatch();
        end=start.nextSibling;
        while(end && end!==cursor.stop && !(end.nodeType===8 && end.data==='/nexo-if:'+id))end=end.nextSibling;
        if(!end || end===cursor.stop)hydrationMismatch();
        cursor.next=end.nextSibling;
      }
      nodes.add(start);nodes.add(end);
      let active=hydrationSession(creating),selected=choice,disposed=false;
      owner.cleanup(()=>{disposed=true;active.destroy();});
      const fragment=creating?document.createDocumentFragment():null;
      const initial=creating?active.cursor(fragment):active.cursor(cursor.parent,start.nextSibling,end);
      render(active,initial,choice);active.end(initial);
      commits.push(()=>{active.commit();if(fragment)end.before(fragment);});
      const region={owns(node){return active.owns(node);},replace(nextChoice){
        if(disposed || nextChoice===selected)return;
        if(!start.parentNode || start.parentNode!==end.parentNode || start.data!=='nexo-if:'+id+':'+selected || end.data!=='/nexo-if:'+id)hydrationMismatch();
        let node=start.nextSibling;
        while(node && node!==end){if(!active.owns(node))hydrationMismatch();node=node.nextSibling;}
        if(node!==end)hydrationMismatch();
        const next=hydrationSession(true),fragment=document.createDocumentFragment(),cursor=next.cursor(fragment);
        try{render(next,cursor,nextChoice);next.end(cursor);next.commit();}catch(error){next.destroy();throw error;}
        active.destroy();
        while(start.nextSibling && start.nextSibling!==end)start.nextSibling.remove();
        end.before(fragment);start.data='nexo-if:'+id+':'+nextChoice;
        active=next;selected=nextChoice;
      }};
      regions.add(region);return region;
    },
    list(cursor,owner,id,read,keyOf,render,dependency){
      let start,end,ordered=[];const entries=new Map();let disposed=false;
      const plan=items=>{
        if(!Array.isArray(items)||items.length>10000)throw new TypeError('Expected bounded list');
        const seen=new Set();return items.map((value,index)=>{const token=keyOf?m6Key(keyOf({value})):'u'+index;if(seen.has(token))throw new TypeError('Duplicate Nexo list keys');seen.add(token);return {token,value};});
      };
      const initial=plan(read());
      if(creating){start=document.createComment('nexo-list:'+id);end=document.createComment('/nexo-list:'+id);cursor.parent.append(start,end);}
      else{
        start=cursor.next;if(!start||start.nodeType!==8||start.data!=='nexo-list:'+id)hydrationMismatch();
        end=start.nextSibling;while(end&&end!==cursor.stop&&!(end.nodeType===8&&end.data==='/nexo-list:'+id))end=end.nextSibling;
        if(!end||end===cursor.stop)hydrationMismatch();cursor.next=end.nextSibling;
      }
      nodes.add(start);nodes.add(end);
      owner.cleanup(()=>{disposed=true;for(const entry of entries.values())entry.session.destroy();entries.clear();ordered=[];});
      function make(token,value,parent,before,claim=false){
        const session=hydrationSession(!claim),row={value};let first,last,fragment;
        if(claim){
          first=before;if(!first||first.nodeType!==8||first.data!=='nexo-row:'+id+':'+token)hydrationMismatch();
          last=first.nextSibling;while(last&&last!==end&&!(last.nodeType===8&&last.data==='/nexo-row:'+id+':'+token))last=last.nextSibling;
          if(!last||last===end)hydrationMismatch();
        }else{fragment=document.createDocumentFragment();first=document.createComment('nexo-row:'+id+':'+token);last=document.createComment('/nexo-row:'+id+':'+token);fragment.append(first);}
        const entry={session,row,start:first,end:last,fragment,token};
        try{
          session.mark(first);session.mark(last);
          const cursor=claim?session.cursor(parent,first.nextSibling,last):session.cursor(fragment,null);
          render(session,cursor,row);session.end(cursor);if(!claim)fragment.append(last);
          return entry;
        }catch(error){session.destroy();throw error;}
      }
      let next=start.nextSibling;
      for(const item of initial){
        const entry=make(item.token,item.value,cursor.parent,next,!creating);entries.set(item.token,entry);ordered.push(item.token);
        if(!creating)next=entry.end.nextSibling;
      }
      if(!creating&&next!==end)hydrationMismatch();
      commits.push(()=>{for(const token of ordered){const entry=entries.get(token);entry.session.commit();if(entry.fragment)end.before(entry.fragment);}});
      const remove=entry=>{entry.session.destroy();let node=entry.start;while(node){const next=node.nextSibling;node.remove();if(node===entry.end)break;node=next;}};
      function verify(){
        if(!start.parentNode||start.parentNode!==end.parentNode||start.data!=='nexo-list:'+id||end.data!=='/nexo-list:'+id)hydrationMismatch();
        let node=start.nextSibling;
        for(const token of ordered){const entry=entries.get(token);if(node!==entry.start||entry.start.data!=='nexo-row:'+id+':'+token||entry.end.data!=='/nexo-row:'+id+':'+token)hydrationMismatch();
          while(node&&node!==entry.end){if(!entry.session.owns(node))hydrationMismatch();node=node.nextSibling;}
          if(node!==entry.end)hydrationMismatch();node=node.nextSibling;
        }
        if(node!==end)hydrationMismatch();
      }
      const region={owns(node){return [...entries.values()].some(entry=>entry.session.owns(node));},update(items){
        if(disposed)return;verify();const planned=plan(items),fresh=new Map(),seen=new Set(planned.map(item=>item.token));
        try{for(const item of planned)if(!keyOf||!entries.has(item.token)){const entry=make(item.token,item.value);fresh.set(item.token,entry);entry.session.commit();}}
        catch(error){for(const entry of fresh.values())entry.session.destroy();throw error;}
        const focused=document.activeElement;let selection;
        if(focused&&typeof focused.selectionStart==='number')selection=[focused.selectionStart,focused.selectionEnd,focused.selectionDirection];
        batch(()=>{
          for(const [token,entry]of entries)if(!seen.has(token)||!keyOf){remove(entry);entries.delete(token);}
          for(const item of planned){let entry=entries.get(item.token);if(!entry){entry=fresh.get(item.token);entries.set(item.token,entry);end.before(entry.fragment);}else if(!Object.is(entry.row.value,item.value)){entry.row.value=item.value;entry.row.owner.invalidate(dependency);}}
          let cursor=end;
          for(let i=planned.length-1;i>=0;i--){const entry=entries.get(planned[i].token);if(entry.end.nextSibling!==cursor){const fragment=document.createDocumentFragment();let node=entry.start;while(node){const next=node.nextSibling;fragment.append(node);if(node===entry.end)break;node=next;}cursor.before(fragment);}cursor=entry.start;}
          ordered=planned.map(item=>item.token);
        });
        if(focused?.isConnected&&document.activeElement!==focused){focused.focus({preventScroll:true});if(selection)try{focused.setSelectionRange(...selection);}catch{}}
      }};
      regions.add(region);return region;
    },
    owns(node) { return nodes.has(node) || [...regions].some(region=>region.owns(node)); },
    state(owner,s) {
      const api=owner.state(s);
      return {
        set:api.set,
        bind(names,read,write) {
          commits.push(()=>{let first=true;api.bind(names,read,value=>{if(first){first=false;return;}write(value);});});
        },
        listen(node,event,action,withEvent=false) { commits.push(()=>api.listen(node,event,action,withEvent)); }
      };
    },
    commit() { for(const commit of commits)commit();commits.length=0; },
    destroy() { const errors=[];for(const cleanup of cleanups.splice(0).reverse())try{cleanup();}catch(error){errors.push(error);}for(const owner of owners.reverse())try{owner.destroy();}catch(error){errors.push(error);}owners.length=commits.length=0;regions.clear();nodes=new WeakSet();if(errors.length)throw new AggregateError(errors,'Hydration cleanup failed'); }
  };
}
function hydrationPath(path) {
  if(typeof path!=='string' || path.length>8192 || !path.startsWith('/') || /[\\?#]/.test(path) || path.includes('//') || /%(?![0-9a-fA-F]{2})/.test(path))hydrationMismatch();
  let parts;
  try {parts=path==='/'?[]:path.replace(/\/$/,'').slice(1).split('/').map(decodeURIComponent);}catch{hydrationMismatch();}
  for(const part of parts)if(part.length>128 || !/^[A-Za-z0-9_-]+$/.test(part) || /^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$/i.test(part))hydrationMismatch();
  if(parts[0]?.toLowerCase()==='_nexo')hydrationMismatch();
  return {url:parts.length?'/'+parts.join('/')+'/':'/',parts};
}
function hydrationHref(path) {
  const {url,parts}=hydrationPath(path);
  for(const route of hydrationRoutes) {
    if(route.length!==parts.length)continue;
    if(route.every((part,i)=>{
      const value=parts[i];
      if(Object.hasOwn(part,'literal'))return part.literal===value;
      if(part.type==='str')return true;
      if(part.type==='bool')return value==='true'||value==='false';
      if(!/^-?(0|[1-9][0-9]*)$/.test(value)||value.length>20)return false;
      try{int(BigInt(value));return true;}catch{return false;}
    }))return url;
  }
  hydrationMismatch();
}
function hydrateRoot(target,build) {
  if(!target || target.nodeType!==1)hydrationMismatch();
  if(hydratedRoots.has(target))return hydratedRoots.get(target);
  if(target.dataset.nexoBuild!==build)hydrationMismatch();
  if(!Object.hasOwn(hydrationPages,target.dataset.nexoPage))hydrationMismatch();
  const descriptor=hydrationPages[target.dataset.nexoPage];
  let raw,props=Object.create(null);
  try{raw=JSON.parse(target.dataset.nexoProps);}catch{hydrationMismatch();}
  if(!raw || typeof raw!=='object' || Array.isArray(raw) || Object.keys(raw).length!==Object.keys(descriptor.types).length)hydrationMismatch();
  for(const [name,type] of Object.entries(descriptor.types)) {
    if(!Object.hasOwn(raw,name))hydrationMismatch();
    const value=raw[name];
    if(type==='int') {
      if(typeof value!=='string' || !/^-?(0|[1-9][0-9]*)$/.test(value) || value.length>20)hydrationMismatch();
      try{props[name]=int(BigInt(value));}catch{hydrationMismatch();}
    } else {
      if(typeof value!==(type==='str'?'string':'boolean'))hydrationMismatch();props[name]=value;
    }
  }
  const session=hydrationSession(),cursor=session.cursor(target);
  try{const source=target.dataset.nexoState||'{}';if(source.length>1048576)hydrationMismatch();session.entryState=m6Patch(descriptor.states,JSON.parse(source),true);}catch{hydrationMismatch();}
  try {
    descriptor.create(session,cursor,props);session.end(cursor);session.commit();
  } catch {session.destroy();hydrationMismatch();}
  let disposed=false;
  const assertLive=()=>{if(disposed)throw new Error('Hydrated root disposed');};
  const handle={capabilities:hydrationCapabilities,get schema(){return JSON.parse(JSON.stringify(descriptor.states));},update(patch){assertLive();session.entry.setState(patch);},snapshot(){assertLive();return session.entry.snapshot();},context(name){assertLive();const id=Object.hasOwn(hydrationContexts,name)?hydrationContexts[name]:null;if(!id||!session.entry.env.has(id))throw new TypeError('Context not visible at page');const value=session.entry.env.get(id);return Object.freeze({setState(patch){assertLive();value.setState(patch);},snapshot(){assertLive();return value.snapshot();},actions:Object.freeze(Object.fromEntries(Object.entries(value.actions).map(([name,fn])=>[name,(...args)=>{assertLive();return fn(...args);}])))});},onDestroy(callback){assertLive();if(typeof callback!=='function')throw new TypeError('Expected cleanup');return session.cleanup(callback);},destroy(){if(disposed)return;disposed=true;try{session.destroy();}finally{if(hydratedRoots.get(target)===handle)hydratedRoots.delete(target);}}};
  hydratedRoots.set(target,handle);return handle;
}
