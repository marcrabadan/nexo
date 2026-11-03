// Numeric, import-free WASM stays off the UI thread and is terminated by its owner.
let instance=null,functions=null;
function number(type,value){
  if(type==='i64'){if(typeof value!=='bigint'||value<-(1n<<63n)||value>=(1n<<63n))throw new TypeError('Expected i64');return value;}
  if(typeof value!=='number'||!Number.isFinite(value))throw new TypeError('Expected finite number');
  if(type==='i32'&&(!Number.isInteger(value)||value<-(2**31)||value>=2**31))throw new TypeError('Expected i32');
  if(type==='f32'){value=Math.fround(value);if(!Number.isFinite(value))throw new TypeError('Expected finite f32');}
  if(!['i32','f32','f64'].includes(type))throw new TypeError('Unknown WASM scalar');return value;
}
self.onmessage=async event=>{
  const message=event.data;
  try{
    if(message.kind==='init'){
      const module=await WebAssembly.compile(message.bytes);
      if(WebAssembly.Module.imports(module).length)throw new TypeError('WASM imports are not allowed');
      instance=await WebAssembly.instantiate(module,{});functions=message.functions;
      for(const name of Object.keys(functions))if(typeof instance.exports[name]!=='function')throw new TypeError('Missing WASM export');
      self.postMessage({id:message.id,ok:true});return;
    }
    if(!instance||!Object.hasOwn(functions,message.name))throw new TypeError('Unknown WASM export');
    const schema=functions[message.name];if(message.args.length!==schema.params.length)throw new TypeError('WASM argument count');
    const args=schema.params.map((type,index)=>number(type,message.args[index]));
    const result=instance.exports[message.name](...args);
    self.postMessage({id:message.id,ok:true,value:schema.result==='void'?undefined:number(schema.result,result)});
  }catch{self.postMessage({id:message.id,ok:false,error:'WASM validation or execution failed'});}
};
