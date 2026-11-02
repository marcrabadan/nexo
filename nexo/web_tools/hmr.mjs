// Per-application dev ownership. No global component state or production bundle.
export function createHotApp(host){
  let definitions=null,app=null,entry=null;
  const records=new Set(),key=Symbol.for('nexo.web.hmr');
  const registry={mount(id,fallback,target,props,env,slot){
    let current=(definitions[id]?.create||fallback)(target,props,env,slot);
    let live=true;
    const record={id,get element(){return current.element;},refresh(create){
      const old=current,anchor=document.createComment('nexo-hmr');
      old.element.before(anchor);
      try{
        const next=create(target,props,env,slot,old.snapshot());
        anchor.replaceWith(next.element);current=next;old.destroy();
      }finally{anchor.remove();}
    }};
    records.add(record);
    return {get element(){return current.element;},get env(){return current.env;},get outlet(){return current.outlet;},
      update(next){props=next;current.update(next);},destroy(){if(!live)return;live=false;records.delete(record);current.destroy();}};
  }};
  function reset(module){
    app?.destroy();app=null;records.clear();
    const env=new Map([[key,registry]]);app=module.__nexoMount(host,env);
  }
  return {apply(module){
    const next=module.__nexoDefinitions;
    const old=definitions,previousEntry=entry;definitions=next;entry=module.__nexoEntry;
    if(!old){reset(module);return;}
    const ids=Object.keys(next),changed=ids.filter(id=>old[id]?.fingerprint!==next[id].fingerprint);
    if(previousEntry!==entry||ids.length!==Object.keys(old).length||ids.some(id=>!old[id]||old[id].signature!==next[id].signature)||changed.some(id=>next[id].kind!=='component')){
      reset(module);return;
    }
    const affected=[...records].filter(record=>changed.includes(record.id));
    // Refresh outermost changed boundaries; they dispose their own descendants.
    const roots=affected.filter(record=>!affected.some(parent=>parent!==record&&parent.element.contains(record.element)));
    try{for(const record of roots)if(records.has(record))record.refresh(next[record.id].create);}
    catch(error){reset(module);throw error;}
  },destroy(){app?.destroy();app=null;records.clear();definitions=null;}};
}
