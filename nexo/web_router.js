// Static-host-compatible fragment router. No network, HTML injection or globals.
function createRouter(target, routes, fallbacks) {
  let disposed=false, current=null, page=null, layouts=[], fallback=null;
  const initialTitle=document.title;
  const initialDescription=document.querySelector('meta[name="description"]');
  const initialContent=initialDescription?.getAttribute('content');
  let description=initialDescription;
  function metadata(values) {
    document.title=values.title ?? initialTitle;
    if(values.description!==undefined || initialDescription) {
      if(!description) {description=document.createElement('meta');description.name='description';document.head.append(description);}
      description.content=values.description ?? initialContent ?? '';
    } else if(description) {description.remove();description=null;}
  }
  function clearPage() {if(page){page.destroy();page=null;}if(fallback){fallback.remove();fallback=null;}}
  function clearLayouts(keep=0) {while(layouts.length>keep)layouts.pop().handle.destroy();}
  function match(path) {
    if(!path.startsWith('/') || path.length>8192 || /[\\?#\u0000-\u001f\u007f]/.test(path))return null;
    if(path.length>1)path=path.replace(/\/+$/,'');
    let segments;
    try {
      segments=path==='/'?[]:path.slice(1).split('/').map(s=>decodeURIComponent(s));
      if(segments.some(s=>!s || /[\/\\\u0000-\u001f\u007f]/.test(s)))return null;
    } catch {return null;}
    for(const route of routes) {
      if(route.parts.length!==segments.length)continue;
      const props=Object.create(null);let valid=true;
      for(let i=0;i<segments.length;i++) {
        const part=route.parts[i], value=segments[i];
        if(Object.hasOwn(part,'literal')) {if(part.literal!==value){valid=false;break;}}
        else if(part.type==='str')props[part.name]=value;
        else if(part.type==='bool') {if(!['true','false'].includes(value)){valid=false;break;}props[part.name]=value==='true';}
        else {
          if(!/^-?(0|[1-9][0-9]*)$/.test(value) || value.length>20){valid=false;break;}
          try {props[part.name]=int(BigInt(value));} catch {valid=false;break;}
        }
      }
      if(valid)return {route,props};
    }
    return null;
  }
  function renderRoute(route,props) {
    let keep=0;
    while(keep<layouts.length && keep<route.layouts.length && layouts[keep].id===route.layouts[keep].id)keep++;
    clearPage();clearLayouts(keep);
    let host=keep?layouts[keep-1].handle.outlet:target;
    let env=keep?layouts[keep-1].handle.env:new Map();
    for(let i=keep;i<route.layouts.length;i++) {
      const item=route.layouts[i];
      const handle=item.create(host,{},env);
      layouts.push({id:item.id,handle});host=handle.outlet;env=handle.env;
    }
    page=route.create(host,props,env);
    metadata(route.meta(props));
    const focus=page.element.querySelector("main,h1,h2") || page.element;
    if(!focus.hasAttribute("tabindex"))focus.tabIndex=-1;
    focus.focus();
  }
  function message(text) {
    fallback=document.createElement('p');fallback.setAttribute('role','alert');fallback.textContent=text;target.append(fallback);
    metadata({});
  }
  function render() {
    if(disposed)return;
    const path=window.location.hash.slice(1)||'/';
    if(current===path)return;
    current=path;
    try {
      const found=match(path);
      if(found)renderRoute(found.route,found.props);
      else if(fallbacks.not_found)renderRoute(fallbacks.not_found,{});
      else {clearPage();clearLayouts();message('Page not found.');}
    } catch {
      clearPage();clearLayouts();
      if(fallbacks.error) {
        try {renderRoute(fallbacks.error,{});return;} catch {clearPage();clearLayouts();}
      }
      message('Unable to render this page.');
    }
  }
  window.addEventListener('hashchange',render);
  render();
  return {
    navigate(path){if(!disposed)navigate(path);},
    destroy(){
      if(disposed)return;
      disposed=true;window.removeEventListener('hashchange',render);
      clearPage();clearLayouts();document.title=initialTitle;
      if(initialDescription)initialDescription.setAttribute('content',initialContent ?? '');
      else if(description)description.remove();
    }
  };
}
