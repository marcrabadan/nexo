// Trusted loopback runner; never loads project JS config, public files or .env.
import {join,dirname} from 'node:path';
import {mkdtemp,rm,readFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {compile,plugin} from './plugin.mjs';
import {loadVite} from './vite.mjs';
const options=Object.fromEntries(Array.from({length:(process.argv.length-2)/2},(_,i)=>[process.argv[2+i*2],process.argv[3+i*2]]));
let root;
try{
  const {vite:{createServer},packageFile}=await loadVite(options['--root']);
  const initial=await compile(options['--python'],options['--file']);
  if(initial.diagnostics.length)throw new Error(JSON.stringify(initial.diagnostics));
  root=await mkdtemp(join(tmpdir(),'nexo-vite-'));
  const server=await createServer({configFile:false,envDir:false,envPrefix:'NEXO_WEB_NO_ENV_',root,publicDir:false,
    plugins:[plugin({python:options['--python'],file:options['--file'],initial,watchRoot:options['--root']})],
    optimizeDeps:{noDiscovery:true,include:[]},
    server:{host:'127.0.0.1',port:Number(options['--port']),strictPort:true,watch:{ignored:['**/dist/**']},allowedHosts:['127.0.0.1','localhost'],cors:false,fs:{strict:true,allow:[root,dirname(packageFile)]},hmr:{overlay:false}}
  });
  // This runs before all Vite handlers, including internal module/file routes.
  function trusted(req){const host=req.headers.host,port=server.httpServer?.address()?.port;return [ '127.0.0.1:'+port,'localhost:'+port].includes(host)&&(!req.headers.origin||req.headers.origin==='http://'+host);}
  server.middlewares.stack.unshift({route:'',handle(req,res,next){
    if(!trusted(req)){res.statusCode=403;res.end('Nexo Web dev: origin/host denied');return;}
    res.setHeader('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self' ws://"+req.headers.host+"; base-uri 'none'; form-action 'none'; frame-ancestors 'none'");
    res.setHeader('X-Content-Type-Options','nosniff');res.setHeader('Cache-Control','no-store');res.setHeader('Referrer-Policy','no-referrer');next();
  }});
  server.httpServer.prependListener('upgrade',(req,socket)=>{if(!trusted(req)||!req.headers.origin)socket.destroy();});
  await server.listen();
  console.log('Nexo Web dev http://127.0.0.1:'+server.httpServer.address().port+'/ (Vite, local only)');
  let stopping=false;
  const close=async()=>{if(stopping)return;stopping=true;await server.close();await rm(root,{recursive:true,force:true});process.exit(0);};
  process.on('SIGINT',close);process.on('SIGTERM',close);
}catch(error){console.error(String(error.message));if(root)await rm(root,{recursive:true,force:true});process.exitCode=1;}
