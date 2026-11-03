"""Explicit public static-site PWA generation. No runtime/API responses are cached."""
import hashlib
import json

from .web_release import MANIFEST, input_revision, metadata, serialized


ICON = '<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512"><rect width="512" height="512" rx="96" fill="#162840"/><path d="M128 384V128h56l144 176V128h56v256h-56L184 208v176z" fill="#fff"/></svg>\n'
REGISTER = """if('serviceWorker' in navigator){(async()=>{let registration=await navigator.serviceWorker.getRegistration('/');if(!registration)registration=await navigator.serviceWorker.register('/service-worker.js',{scope:'/'});else registration.update().catch(()=>{});if(!registration.active){const worker=registration.installing||registration.waiting;if(worker)await new Promise((resolve,reject)=>{const check=()=>{if(worker.state==='activated')resolve();if(worker.state==='redundant')reject(new Error('PWA install failed'));};worker.addEventListener('statechange',check);check();});}await navigator.serviceWorker.ready;document.documentElement.dataset.nexoPwa='ready';})().catch(()=>{document.documentElement.dataset.nexoPwa='error';});}\n"""


def augment(files):
    output = dict(files)
    manifest = json.loads(output.pop(MANIFEST)) if MANIFEST in output else {'schema_version':1,'format':'nexo-web-static','entry':'index.html','routes':{'/':'index.html'},'sourcemap':False,'observe':False,'input_sha256':input_revision(files)}
    for name in list(output):
        if name.endswith('.html'):
            page = output[name].replace("script-src 'none'", "script-src 'self'").replace("connect-src 'none'", "connect-src 'self'")
            page = page.replace("base-uri 'none'", "worker-src 'self'; manifest-src 'self'; img-src 'self'; base-uri 'none'")
            page = page.replace('</head>', '<link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#162840"></head>')
            page = page.replace('</body>', '<script type="module" src="/pwa-register.js"></script></body>')
            output[name] = page
    output['pwa-icon.svg'] = ICON
    output['pwa-register.js'] = REGISTER
    output['manifest.webmanifest'] = serialized({'id':'/','name':'Nexo Web','short_name':'Nexo','start_url':'/','scope':'/','display':'standalone','background_color':'#162840','theme_color':'#162840','icons':[{'src':'/pwa-icon.svg','sizes':'any','type':'image/svg+xml','purpose':'any maskable'}]})
    routes = manifest['routes']
    inverse = {name:url for url,name in routes.items()}
    assets = [{'url':inverse.get(name,'/'+name),'integrity':metadata(content)['integrity']} for name,content in sorted(output.items())]
    version = hashlib.sha256(serialized(assets).encode()).hexdigest()
    worker = '''const PREFIX='nexo-web-pwa:/';
const CACHE=PREFIX+VERSION;
const ASSETS=PLAN;
const ALLOWED=new Set(ASSETS.map(asset=>asset.url));
self.addEventListener('install',event=>event.waitUntil((async()=>{const existing=(await caches.keys()).includes(CACHE);const cache=await caches.open(CACHE);try{for(const asset of ASSETS){const response=await fetch(new Request(asset.url,{cache:'reload',integrity:asset.integrity}));if(!response.ok)throw new Error('Precache failed');await cache.put(asset.url,response);}await self.skipWaiting();}catch(error){if(!existing)await caches.delete(CACHE);throw error;}})()));
self.addEventListener('activate',event=>event.waitUntil((async()=>{for(const name of await caches.keys())if(name.startsWith(PREFIX)&&name!==CACHE)await caches.delete(name);await self.clients.claim();})()));
self.addEventListener('fetch',event=>{const request=event.request,url=new URL(request.url);if(request.method!=='GET'||url.origin!==self.location.origin||url.search)return;
if(ALLOWED.has(url.pathname)){event.respondWith(caches.open(CACHE).then(async cache=>(await cache.match(url.pathname))||fetch(request)));return;}
if(request.mode==='navigate'&&ALLOWED.has('/404.html'))event.respondWith(fetch(request).catch(async()=>{const response=await (await caches.open(CACHE)).match('/404.html');return new Response(await response.arrayBuffer(),{status:404,headers:response.headers});}));
});
'''.replace('VERSION', json.dumps(version)).replace('PLAN', serialized(assets).strip())
    output['service-worker.js'] = worker
    manifest['pwa'] = {'scope':'/','cache':version,'policy':'explicit-public-precache-only'}
    manifest['input_sha256'] = input_revision({'source':manifest['input_sha256'],'pwa':True})
    manifest['files'] = {name:metadata(content) for name,content in sorted(output.items())}
    return {**output, MANIFEST:serialized(manifest)}
