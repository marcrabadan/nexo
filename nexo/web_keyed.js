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
