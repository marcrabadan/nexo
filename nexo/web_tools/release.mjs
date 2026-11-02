// Trusted optimized static build. No project config, env, public files or plugins.
import {mkdtemp,rm,writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {loadVite} from './vite.mjs';
import {createRequire} from 'node:module';
let root;
try{
  let input='';for await(const chunk of process.stdin)input+=chunk;
  const payload=JSON.parse(input);
  const {vite:{build},packageFile}=await loadVite(payload.project);
  root=await mkdtemp(join(tmpdir(),'nexo-release-'));
  for(const [name,text] of Object.entries(payload.files)){
    if(!['index.html','app.js','main.js','app.css','app.js.map','app.css.map'].includes(name))throw new Error('NW5001: Unexpected compiler input');
    await writeFile(join(root,name),text);
  }
  const originals=new Map();
  const mapped=payload.sourcemap?'map-'+createHash('sha256').update(payload.files['app.js.map']).digest('hex').slice(0,12)+'-':'';
  const result=await build({configFile:false,envDir:false,envPrefix:'NEXO_WEB_NO_ENV_',root,base:'./',publicDir:false,logLevel:'silent',
    plugins:[{name:'nexo-compiler-map',enforce:'pre',transform(code,id){
      if(payload.sourcemap&&id.split('?')[0]===join(root,'app.js')){
        const map=JSON.parse(payload.files['app.js.map']);
        // Preserve identity through Vite's normalized source filenames, then remove tags.
        map.sourcesContent=map.sourcesContent.map((source,index)=>{
          const tagged=source+'\n# nexo-map-source:'+Buffer.from(map.sources[index]).toString('base64');
          originals.set(tagged,{name:map.sources[index],source});return tagged;
        });
        return {code:code.replace(/\/\/# sourceMappingURL=.*\n$/,''),map};
      }
    }}],
    build:{write:false,target:'es2022',cssTarget:'esnext',minify:true,cssMinify:'lightningcss',sourcemap:payload.sourcemap,
      modulePreload:{polyfill:false},assetsInlineLimit:0,reportCompressedSize:false,
      rolldownOptions:{output:{entryFileNames:'assets/[name]-'+mapped+'[hash].js',chunkFileNames:'assets/[name]-'+mapped+'[hash].js',assetFileNames:'assets/[name]-[hash][extname]'}}}
  });
  const files={};
  for(const output of (Array.isArray(result)?result:[result]))for(const asset of output.output){
    files[asset.fileName]=asset.type==='chunk'?asset.code:typeof asset.source==='string'?asset.source:Buffer.from(asset.source).toString('utf8');
  }
  for(const name of Object.keys(files))if(name.endsWith('.map')){
    const map=JSON.parse(files[name]);
    map.sourcesContent?.forEach((source,index)=>{const original=originals.get(source);if(original){map.sources[index]=original.name;map.sourcesContent[index]=original.source;}});
    files[name]=JSON.stringify(map);
  }
  if(payload.sourcemap){
    const {transform}=createRequire(packageFile)('lightningcss');
    const input=JSON.parse(payload.files['app.css.map']);
    const result=transform({filename:'app.css',code:Buffer.from(payload.files['app.css'].replace(/\/\*# sourceMappingURL=.*?\*\/\s*$/,'')),
      minify:true,sourceMap:true,inputSourceMap:JSON.stringify(input)});
    const names=Object.keys(files).filter(name=>name.endsWith('.css'));
    if(names.length!==1)throw new Error('Expected one compiled CSS asset');
    const old=names[0],map=JSON.parse(Buffer.from(result.map).toString('utf8'));
    const suffix=createHash('sha256').update(result.map).digest('hex').slice(0,12);
    const name=old.slice(0,-4)+'-map-'+suffix+'.css';map.file=name.split('/').at(-1);
    delete files[old];
    files[name]=Buffer.from(result.code).toString('utf8')+'\n/*# sourceMappingURL='+map.file+'.map */\n';
    files[name+'.map']=JSON.stringify(map);
    files['index.html']=files['index.html'].replaceAll(old,name);
  }
  process.stdout.write(JSON.stringify({schema_version:1,files}));
}catch(error){console.error(error.message.startsWith('NW4001:')?error.message:'NW5001: Release optimization failed: '+error.message);process.exitCode=1;}
finally{if(root)await rm(root,{recursive:true,force:true});}
