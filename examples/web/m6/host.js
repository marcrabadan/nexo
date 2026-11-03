// Explicit trusted host adapters; no inline scripts or implicit network features.
const script=document.querySelector('script[src^="/_nexo/hydrate-"]');
const {hydrate,loadWasm,connectEvents}=await import(script.src);
const handle=hydrate(document.querySelector('#app'));
connectEvents(handle,'/events',{schema:{kind:'record',fields:{notice:{kind:'str'}}}});
try{
  const module=await loadWasm(handle,'/add.wasm',{
    sha256:'@@SHA256@@',functions:{add:{params:['i32','i32'],result:'i32'}}
  });
  handle.update({result:await module.call('add',20,22)});
}catch{handle.update({notice:'WASM unavailable; server content remains usable'});}
