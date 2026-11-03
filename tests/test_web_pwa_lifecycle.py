"""Real service-worker replacement and failed-install recovery at one origin."""
import json

import pytest
from test_web_frontend import browser
from test_web_hydration import project
from test_web_m6_completion import public_preview
from nexo.web_frontend import build

SOURCE='''use nexo.web
pub page Home:
    path = "/"
    view:
        h1("VERSION home")
        a("Guide", href: "/guide")
pub page Guide:
    path = "/guide"
    view:
        h1("VERSION guide")
pub page Missing:
    not_found = true
    view:
        h1("Missing")
'''


def publish(entry, directory, version):
    entry.write_text(SOURCE.replace('VERSION', version))
    files=build(entry,directory,static=True,pwa=True)
    return 'nexo-web-pwa:/'+json.loads(files['nexo-manifest.json'])['pwa']['cache']


def controlled(page, base):
    page.goto(base+'/');page.wait_for_selector('html[data-nexo-pwa="ready"]');page.reload()
    assert page.evaluate('!!navigator.serviceWorker.controller')


UPDATE='''async()=>{
  const registration=await navigator.serviceWorker.getRegistration('/'),previous=registration.active;
  const next=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('No installing worker')),20000);
    const found=()=>{if(registration.installing){clearTimeout(timer);resolve(registration.installing);}};
    registration.addEventListener('updatefound',found,{once:true});
    registration.update().then(found,error=>{clearTimeout(timer);reject(error);});
  });
  const state=await new Promise((resolve,reject)=>{
    const timer=setTimeout(()=>reject(new Error('Worker transition timed out')),20000);
    const check=()=>{if(['activated','redundant'].includes(next.state)){clearTimeout(timer);resolve(next.state);}};
    next.addEventListener('statechange',check);check();
  });
  return {state,previousRetained:registration.active===previous};
}'''


def test_real_browser_pwa_upgrade_retires_only_old_owned_cache(tmp_path,browser):
    entry=project(tmp_path,SOURCE);directory=tmp_path/'dist';old=publish(entry,directory,'One')
    with public_preview(directory) as (base,server):
        context=browser.new_context();page=context.new_page();controlled(page,base)
        page.evaluate("caches.open('other-application').then(c=>c.put('/unrelated',new Response('keep')))" )
        new=publish(entry,directory,'Two');assert old!=new
        assert page.evaluate(UPDATE)=={'state':'activated','previousRetained':False}
        page.reload();assert page.locator('h1').inner_text()=='Two home'
        keys=page.evaluate('caches.keys()');assert new in keys and old not in keys and 'other-application' in keys
        assert page.evaluate("caches.match('/unrelated').then(r=>r.text())")=='keep'
        server.shutdown();server.server_close()
        assert page.goto(base+'/guide/').status==200
        assert page.locator('h1').inner_text()=='Two guide'
        context.close()


@pytest.mark.parametrize('fault',['integrity','missing'])
def test_real_browser_pwa_failed_upgrade_preserves_last_good_offline_site(tmp_path,browser,fault):
    entry=project(tmp_path,SOURCE);directory=tmp_path/'dist';old=publish(entry,directory,'One')
    with public_preview(directory) as (base,server):
        context=browser.new_context();page=context.new_page();controlled(page,base)
        candidate=publish(entry,directory,'Two')
        target=directory/'guide/index.html'
        if fault=='integrity':target.write_text(target.read_text().replace('Two guide','Corrupted guide'))
        else:target.unlink()
        assert page.evaluate(UPDATE)=={'state':'redundant','previousRetained':True}
        keys=page.evaluate('caches.keys()');assert old in keys and candidate not in keys
        server.shutdown();server.server_close()
        assert page.goto(base+'/guide/').status==200
        assert page.locator('h1').inner_text()=='One guide'
        context.close()


def test_real_browser_pwa_failed_same_generation_worker_cannot_delete_live_cache(tmp_path,browser):
    entry=project(tmp_path,SOURCE);directory=tmp_path/'dist';active=publish(entry,directory,'One')
    with public_preview(directory) as (base,server):
        context=browser.new_context();page=context.new_page();controlled(page,base)
        worker=directory/'service-worker.js';worker.write_text(worker.read_text()+'\n// Redeploy worker without changing public asset version.\n')
        pagefile=directory/'guide/index.html';pagefile.write_text(pagefile.read_text().replace('One guide','Corrupted guide'))
        assert page.evaluate(UPDATE)=={'state':'redundant','previousRetained':True}
        assert active in page.evaluate('caches.keys()')
        server.shutdown();server.server_close()
        assert page.goto(base+'/guide/').status==200
        assert page.locator('h1').inner_text()=='One guide'
        context.close()


def test_real_browser_pwa_rollback_reinstalls_previous_public_release(tmp_path,browser):
    entry=project(tmp_path,SOURCE);directory=tmp_path/'dist';first=publish(entry,directory,'One')
    with public_preview(directory) as (base,server):
        context=browser.new_context();page=context.new_page();controlled(page,base)
        second=publish(entry,directory,'Two')
        assert page.evaluate(UPDATE)['state']=='activated'
        page.reload();assert page.locator('h1').inner_text()=='Two home'
        restored=publish(entry,directory,'One');assert restored==first
        assert page.evaluate(UPDATE)=={'state':'activated','previousRetained':False}
        keys=page.evaluate('caches.keys()');assert first in keys and second not in keys
        server.shutdown();server.server_close()
        assert page.goto(base+'/guide/').status==200
        assert page.locator('h1').inner_text()=='One guide'
        context.close()


def test_real_browser_pwa_initial_failure_cleans_candidate_and_can_recover(tmp_path,browser):
    entry=project(tmp_path,SOURCE);directory=tmp_path/'dist';candidate=publish(entry,directory,'One')
    with public_preview(directory) as (base,server):
        pagefile=directory/'guide/index.html';original=pagefile.read_text()
        pagefile.write_text(original.replace('One guide','Corrupted guide'))
        context=browser.new_context();page=context.new_page();page.goto(base+'/')
        page.wait_for_selector('html[data-nexo-pwa="error"]')
        assert candidate not in page.evaluate('caches.keys()')
        assert not page.evaluate('!!navigator.serviceWorker.controller')
        pagefile.write_text(original)
        controlled(page,base)
        assert candidate in page.evaluate('caches.keys()')
        server.shutdown();server.server_close()
        assert page.goto(base+'/guide/').status==200
        assert page.locator('h1').inner_text()=='One guide'
        context.close()
