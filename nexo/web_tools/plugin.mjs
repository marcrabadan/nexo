// Compiler-first Vite plugin. Vite watches/serves modules; Python owns semantics.
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const execute=promisify(execFile);
const APP='\0nexo:app',START='\0nexo:start';
export async function compile(python,file){
  let stdout;
  try{({stdout}=await execute(python,['-m','nexo.web_bridge',file],{timeout:30000,maxBuffer:16*1024*1024}));}
  catch(error){if(error.stdout)stdout=error.stdout;else throw error;}
  const result=JSON.parse(stdout);
  if(result.schema_version!==1)throw new Error('Unsupported Nexo compiler protocol');
  return result;
}
export function plugin({python,file,initial,watchRoot}){
  let bundle=initial,server,timer,closed=false,sequence=0,queue=Promise.resolve();
  const start=`import * as initial from 'virtual:nexo/app';
let app=null,latestCSS=0;
function error(message){let node=document.querySelector('#nexo-diagnostic');if(!node){node=document.createElement('pre');node.id='nexo-diagnostic';node.setAttribute('role','alert');document.body.prepend(node);}node.textContent=message;}
function clear(){document.querySelector('#nexo-diagnostic')?.remove();}
function apply(module){if(!module)return;try{app?.destroy();app=null;app=module.__nexoMount(document.querySelector('#app'));}catch(e){error('Nexo mount failed: '+e.message);}}
apply(initial);
if(import.meta.hot){
  import.meta.hot.accept('virtual:nexo/app',module=>apply(module));
  import.meta.hot.on('nexo:error',data=>error(data.message));
  import.meta.hot.on('nexo:clear',clear);
  import.meta.hot.on('nexo:css',({version})=>{latestCSS=version;const old=document.querySelector('#nexo-style'),next=old.cloneNode();next.href='/__nexo/app.css?v='+version;next.onload=()=>{if(version!==latestCSS){next.remove();return;}for(const link of document.querySelectorAll('#nexo-style'))if(link!==next)link.remove();};next.onerror=()=>{next.remove();if(version===latestCSS)error('Nexo CSS update failed');};old.after(next);});
  import.meta.hot.dispose(()=>app?.destroy());
}`;
  async function update(){
    if(closed)return;
    let next;
    try{next=await compile(python,file);}catch(error){server.ws.send({type:'custom',event:'nexo:error',data:{message:'Nexo compiler unavailable: '+error.message}});return;}
    if(next.diagnostics.length){const d=next.diagnostics[0];server.ws.send({type:'custom',event:'nexo:error',data:{message:d.code+' '+d.file+':'+d.line+':'+d.column+' '+d.message}});return;}
    const old=bundle;bundle=next;server.watcher.add(next.dependencies);
    if(old.javascript!==next.javascript){
      const module=server.moduleGraph.getModuleById(APP);
      if(module){server.moduleGraph.invalidateModule(module);server.ws.send({type:'update',updates:[{type:'js-update',path:'/__nexo/start.js',acceptedPath:APP,timestamp:Date.now()}]});}
    }
    if(old.css!==next.css)server.ws.send({type:'custom',event:'nexo:css',data:{version:++sequence}});
    server.ws.send({type:'custom',event:'nexo:clear',data:{}});
  }
  return {name:'nexo-web',enforce:'pre',
    resolveId(id){if(id==='virtual:nexo/app')return APP;if(id==='/__nexo/start.js')return START;},
    load(id){
      if(id===START)return start;
      if(id===APP){
        const entry=bundle.bootstrap;
        return {code:bundle.assets['app.js'].replace(/\/\/# sourceMappingURL=.*\n$/,'')+'\nexport function __nexoMount(host){return '+entry+'(host);}\n',map:JSON.parse(bundle.assets['app.js.map'])};
      }
    },
    handleHotUpdate(){return [];},
    configureServer(instance){
      server=instance;server.watcher.add([watchRoot,...bundle.dependencies]);
      const changed=(_,path)=>{
        if(!/\.(?:nexo|css)$/.test(path)&&!path.endsWith('/nexo.toml'))return;
        clearTimeout(timer);timer=setTimeout(()=>{queue=queue.then(update).catch(error=>server.config.logger.error(error.message));},60);
      };
      server.watcher.on('all',changed);
      server.httpServer?.on('close',()=>{closed=true;clearTimeout(timer);server.watcher.off('all',changed);});
      server.middlewares.use(async(req,res,next)=>{
        const path=new URL(req.url,'http://127.0.0.1').pathname;
        if(path==='/__nexo/app.css'){res.setHeader('Content-Type','text/css; charset=utf-8');res.end(bundle.assets['app.css']);return;}
        if(path==='/'||path==='/index.html'){
          const html=bundle.assets['index.html'].replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/,'').replace('href="./app.css"','id="nexo-style" href="/__nexo/app.css"').replace('src="./main.js"','src="/__nexo/start.js"');
          res.setHeader('Content-Type','text/html; charset=utf-8');res.end(await server.transformIndexHtml('/',html));return;
        }
        next();
      });
    }
  };
}
