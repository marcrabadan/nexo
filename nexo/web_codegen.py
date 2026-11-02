"""Browser semantic analysis and deterministic direct-DOM lowering (M1–M5)."""
from __future__ import annotations

import json
import html
import hashlib
from pathlib import Path
import re
from urllib.parse import unquote

from . import ast as A
from . import web_frontend as F
from .web_model import Binding, reference
from . import web_data as D


def at(declaration,message,code,line=None):
    try:
        F.fail(line or declaration.line,message,code)
    except F.WebError as error:
        error.file=str(declaration.unit.path or '<web>')
        raise


def members(nodes):
    for node in nodes:
        yield node
        yield from members(node.children)


def schema(d):
    fields=[]
    for name,p in d.props.items():
        default='' if p.default is None else ', default: () => '+F.value(p.default,{},p.line)[0]
        fields.append(f'[{json.dumps(name)}]: {{type:{json.dumps(p.type)}{default}}}')
    return '{'+','.join(fields)+'}'


def prop_object(node, child, symbols):
    if node.text is not None:
        F.fail(node.line,'Component properties must use named arguments.', 'NW2002')
    supplied=dict(node.attrs)
    for name,p in child.props.items():
        if name not in supplied and p.default is None:
            F.fail(node.line,f'Missing required property {name} on {child.name}.','NW2002')
    fields,dependencies=[],set()
    for name,expr in node.attrs:
        if name not in child.props:
            F.fail(node.line,f'Unknown property {name} on {child.name}.','NW2002')
        js,typ,deps=F.value(expr,symbols,node.line)
        if typ!=child.props[name].type:
            F.fail(node.line,f'Property {name} requires {child.props[name].type}.','NW2002')
        fields.append(f'[{json.dumps(name)}]: {js}')
        dependencies |= deps
    return '({'+','.join(fields)+'})', dependencies


def context_action(expr,symbols,line):
    if isinstance(expr,A.Call) and not expr.args and isinstance(expr.func,A.Attr) and isinstance(expr.func.obj,A.Name):
        b=symbols.get(expr.func.obj.name)
        if b and b.type=='context' and expr.func.name in b.context.actions:
            return f'{b.code}.actions[{json.dumps(expr.func.name)}]()'
    return None


def action_statements(d):
    actions={}
    for name,body in d.actions.items():
        out=[]
        symbols=dict(d.symbols)
        if name in d.action_params:
            param=d.action_params[name]
            if param in symbols or param in d.unit.imports:
                F.fail(d.line,'Event parameter cannot shadow a member/import.','NW1002')
            symbols[param]=Binding(('record','WebEvent',(('value','str'),('checked','bool'))),'event')
        for node,line in body:
            if isinstance(node,A.Assign) and isinstance(node.target,A.Name):
                target=node.target.name
                if target not in d.states or node.op not in ('=','+=','-=','*='):
                    F.fail(line,'Actions may assign only local/context state, never properties.','NW1001')
                if isinstance(node.value,A.ListLit) and not node.value.items and isinstance(d.states[target].type,tuple) and d.states[target].type[0]=='list':
                    js,typ='Object.freeze([])',d.states[target].type
                else:js,typ,_=F.value(node.value,symbols,line)
                if typ!=d.states[target].type or (node.op!='=' and typ!='int'):
                    F.fail(line,'State assignment type mismatch.','NW1002')
                if node.op!='=':js=f'int(s[{json.dumps(target)}]{node.op[0]}({js}))'
                out.append(f'api.set({json.dumps(target)}, {js});')
            elif isinstance(node,A.ExprStmt):
                js=context_action(node.expr,d.symbols,line)
                resource=D.method(node.expr.func,d.symbols,line) if isinstance(node.expr,A.Call) and not node.expr.args else None
                if resource:js=resource+'()'
                if js:
                    out.append(js+';')
                elif isinstance(node.expr,A.Call) and isinstance(node.expr.func,A.Name) and node.expr.func.name=='navigate' and len(node.expr.args)==1 and not node.expr.args[0].name and d.kind!='context':
                    js,typ,_=F.value(node.expr.args[0].value,symbols,line)
                    if typ!='str':F.fail(line,'navigate requires a local route string.','NW2003')
                    out.append(f'navigate({js});')
                else:F.fail(line,'Actions may assign state, call an injected context action, or navigate.')
            else:F.fail(line,'Unsupported action statement.')
        actions[name]=out
    return actions


def route(d):
    if d.kind!='page':return None
    flags=[]
    for key in ('not_found','error'):
        if key in d.metadata:
            expr,line=d.metadata[key]
            if not isinstance(expr,A.Bool) or not expr.value:F.fail(line,f'{key} requires true.','NW2003')
            flags.append(key)
    if len(flags)>1 or (flags and ('path' in d.metadata or d.props)):
        F.fail(d.line,'Fallback pages have one flag, no path and no parameters.','NW2003')
    if flags:
        if flags[0]=='error' and 'layout' in d.metadata:
            F.fail(d.line,'An error page must be layout-free so layout failures can recover.','NW2003')
        return {'fallback':flags[0]}
    if 'path' not in d.metadata:F.fail(d.line,'A page requires an explicit path.','NW2003')
    expr,line=d.metadata['path']
    if not isinstance(expr,A.Str):F.fail(line,'path must be a literal route pattern.','NW2003')
    pattern=''
    for part in expr.parts:
        if isinstance(part,str):
            if '{' in part or '}' in part:F.fail(line,'Route braces must declare parameter placeholders; encode literal braces.','NW2003')
            pattern+=part
        elif isinstance(part,A.Name):pattern+='{'+part.name+'}'
        else:F.fail(line,'Route placeholders must be simple typed parameter names.','NW2003')
    if not pattern.startswith('/') or (pattern!='/' and pattern.endswith('/')) or any(c in pattern for c in ('?','#','\\')) or '//' in pattern or any(ord(c)<32 or ord(c)==127 for c in pattern):
        F.fail(line,'Use a canonical absolute route path, without query/fragment or empty segments.','NW2003')
    parts=[];names=[]
    for segment in ([] if pattern=='/' else pattern[1:].split('/')):
        match=re.fullmatch(r'\{('+F.IDENT+r')\}',segment)
        if match:
            name=match[1]
            if name not in d.props or name in names or d.props[name].default is not None:
                F.fail(line,'Each route placeholder requires one matching non-defaulted typed parameter.','NW2003')
            names.append(name);parts.append({'name':name,'type':d.props[name].type})
        else:
            if '{' in segment or '}' in segment or re.search(r'%(?![0-9a-fA-F]{2})',segment):F.fail(line,'Invalid route pattern segment.','NW2003')
            try: decoded=unquote(segment,errors='strict')
            except UnicodeError: F.fail(line,'Invalid UTF-8 route segment.','NW2003')
            if any(c in decoded for c in '/\\') or any(ord(c)<32 or ord(c)==127 for c in decoded):F.fail(line,'Unsafe route segment.','NW2003')
            parts.append({'literal':decoded})
    if set(names)!=set(d.props):F.fail(line,'Page parameters must match route placeholders exactly.','NW2003')
    return {'path':pattern,'parts':parts,'score':sum('literal' in p for p in parts)}


def analyze(program):
    declarations=[d for unit in program.units for d in unit.declarations.values()]
    # Normalize contracts before resolving consumers, including later contexts.
    for d in declarations:
        try:
            for p in d.states.values():
                if not isinstance(p.type,(str,tuple)):
                    p.type=D.contract(program,d.unit,p.type,p.line)
        except F.WebError as error:
            if not getattr(error,'file',None):error.file=str(d.unit.path or '<web>')
            raise
    for d in declarations:
        try:
            if (set(d.props)|set(d.states)|set(d.actions)|set(d.injections)|set(d.requests)) & set(d.unit.imports):
                F.fail(d.line,'A module alias cannot be rebound by a local member.','NW2001')
            d.symbols={name:Binding(p.type,f's[{json.dumps(name)}]',name) for name,p in d.props.items()}
            d.providers=[];d.injects={};seen=set()
            for name,line in d.provides:
                ctx=program.resolve(d.unit,name,line,'context')
                if ctx.symbol in seen:F.fail(line,'Duplicate context provider.','NW2004')
                seen.add(ctx.symbol);d.providers.append(ctx)
            for alias,(name,line) in d.injections.items():
                ctx=program.resolve(d.unit,name,line,'context')
                d.injects[alias]=ctx
                d.symbols[alias]=Binding('context',f'x[{json.dumps(alias)}]',context=ctx)
            D.install_contracts(program,d)
            init_symbols={name:b for name,b in d.symbols.items() if not isinstance(b,Binding) or b.type!='context'}
            d.initials={}
            for name,p in d.states.items():
                if isinstance(p.default,A.ListLit) and not p.default.items and isinstance(p.type,tuple) and p.type[0]=='list':
                    js,typ='Object.freeze([])',p.type
                else:js,typ,_=F.value(p.default,init_symbols,p.line)
                if typ!=p.type:F.fail(p.line,'State initializer type mismatch.','NW1002')
                d.initials[name]=js
                d.symbols[name]=Binding(p.type,f's[{json.dumps(name)}]',name,True)
            for name,req in d.requests.items():
                d.symbols[name]=Binding('resource',f'r[{json.dumps(name)}]','$'+name,context=req)
            for name,req in d.requests.items():D.request(program,d,name,req)
            d.action_js=action_statements(d)
            d.children={};d.slots=0
            def check_view(nodes,symbols,structural=False):
                for node in nodes:
                    node.symbols=symbols
                    if node.tag=='#if':
                        for condition,body in node.branches:
                            if condition is not None and F.value(condition,symbols,node.line)[1]!='bool':F.fail(node.line,'View condition requires bool.', 'NW3002')
                            check_view(body,D.narrowed(symbols,condition),True)
                        continue
                    if node.tag=='#for':
                        _,typ,_=F.value(node.text,symbols,node.line)
                        if not isinstance(typ,tuple) or typ[0]!='list':F.fail(node.line,'View for requires a typed JSON list.', 'NW3001')
                        if node.variable in symbols or node.variable in d.unit.imports:F.fail(node.line,'Loop item cannot shadow an existing binding.', 'NW3001')
                        node.item_symbol=f'_item{node.line}'
                        node.row_symbol=f'_row{node.line}'
                        node.item_dep=f'$item{node.line}'
                        local=dict(symbols);local[node.variable]=Binding(typ[1],node.row_symbol+'.value' if node.key is not None else node.item_symbol,node.item_dep if node.key is not None else None)
                        if node.key is not None:
                            _,key_type,key_deps=F.value(node.key,local,node.line)
                            if key_type not in ('str','int') or key_deps!={node.item_dep}:
                                F.fail(node.line,'A list key must read its item and have type str or int.','NW3001')
                        check_view(node.children,local,True)
                        continue
                    if node.tag=='slot':
                        if d.kind=='layout' and structural:F.fail(node.line,'A layout slot must be unconditional and outside loops.', 'NW2005')
                        if node.text is not None or node.attrs or node.children or d.kind=='page':F.fail(node.line,'Use slot() without arguments in a component/layout.','NW2005')
                        d.slots+=1
                    elif node.tag in F.TAGS or node.tag=='a':
                        if node.tag in F.VOID and (node.text is not None or node.children):F.fail(node.line,'Void elements have no children.')
                        if node.text is not None and F.value(node.text,symbols,node.line)[1] not in ('str','int','bool'):F.fail(node.line,'Element text requires scalar data.', 'NW3001')
                        for key,expr in node.attrs:
                            if key.startswith('on_'):
                                if key not in ('on_click','on_input','on_change') or not (D.method(expr,symbols,node.line) or (isinstance(expr,A.Name) and expr.name in d.actions) or (isinstance(expr,A.Attr) and isinstance(expr.obj,A.Name) and expr.obj.name in d.injects and expr.name in d.injects[expr.obj.name].actions)):
                                    F.fail(node.line,'Event handlers must name a declared local/context action.','NW1002' if key in ('on_click','on_input','on_change') else 'NW1004')
                            elif key in F.ATTRS or (key=='href' and node.tag=='a'):
                                _,typ,_=F.value(expr,symbols,node.line)
                                if (key in ('disabled','checked') and typ!='bool') or (key not in ('disabled','checked') and typ not in ('str','int')) or (key=='href' and typ!='str'):
                                    F.fail(node.line,'HTML attribute type mismatch.','NW1002')
                                if key=='href' and isinstance(expr,A.Str) and all(isinstance(p,str) for p in expr.parts):
                                    path=''.join(expr.parts)
                                    if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\\?#') or any(ord(c)<32 or ord(c)==127 for c in path):F.fail(node.line,'Links must use local route paths.','NW1004')
                            else:F.fail(node.line,'Unsupported or unsafe HTML attribute.','NW1004')
                    else:
                        if '.' not in node.tag and not node.tag[0].isupper():F.fail(node.line,'Unsupported or unsafe HTML element.','NW1004')
                        child=program.resolve(d.unit,node.tag,node.line,'component')
                        prop_object(node,child,symbols)
                        d.children[id(node)]=child
                    check_view(node.children,symbols,structural)
            check_view(d.view,d.symbols)
            if d.slots>1 or (d.kind=='layout' and d.slots!=1):F.fail(d.line,'Layouts require exactly one slot(); components allow at most one.','NW2005')
            d.layout=None
            if 'layout' in d.metadata:
                expr,line=d.metadata['layout'];d.layout=program.resolve(d.unit,reference(expr,line),line,'layout')
            d.route=route(d)
            for key in ('title','description'):
                if key in d.metadata:
                    expr,line=d.metadata[key]
                    props={name:b for name,b in d.symbols.items() if name in d.props}
                    if F.value(expr,props,line)[1]!='str':F.fail(line,'Page metadata requires text or route-parameter interpolation.','NW2003')
        except F.WebError as error:
            if not getattr(error,'file',None):error.file=str(d.unit.path or '<web>')
            raise
    for d in declarations:
        for node in members(d.view):
            child=d.children.get(id(node))
            if child and node.children and not child.slots:F.fail(node.line,f'{child.name} does not declare slot().','NW2005')
    # Acyclic composition and layout chains are checked before any browser code runs.
    def graph(d,stack):
        if d.symbol in stack:at(d,'Recursive component/layout ownership is not supported.','NW2002')
        for child in {c.symbol:c for c in d.children.values()}.values():graph(child,[*stack,d.symbol])
    for d in declarations:
        graph(d,[])
        chain=[];parent=d.layout
        while parent:
            if parent.symbol in chain or parent is d:at(d,'Layout cycle.','NW2003')
            chain.append(parent.symbol);parent=parent.layout
    pages=[d for d in declarations if d.kind=='page' and d.public]
    fallbacks={}
    routes=[]
    for page in pages:
        if 'fallback' in page.route:
            key=page.route['fallback']
            if key in fallbacks:F.fail(page.line,'Duplicate fallback page.','NW2003')
            fallbacks[key]=page
        else:routes.append(page)
    for i,left in enumerate(routes):
        for right in routes[i+1:]:
            a,b=left.route,right.route
            if len(a['parts'])==len(b['parts']) and a['score']==b['score'] and all('literal' not in x or 'literal' not in y or x['literal']==y['literal'] for x,y in zip(a['parts'],b['parts'])):
                F.fail(right.line,'Ambiguous/duplicate route patterns.','NW2003')
    roots=pages if pages else [d for d in program.root.declarations.values() if d.public and d.kind=='component'][:1]
    if not roots:F.fail(1,'At least one public component or public page is required.')
    if not pages and any(p.default is None for p in roots[0].props.values()):
        F.fail(roots[0].line,'The application entry component requires defaults for all properties; wrap reusable components in an App.','NW2002')
    def ownership(d,available,stack):
        available=available|{c.symbol for c in d.providers}
        for ctx in d.injects.values():
            if ctx.symbol not in available:at(d,f'Missing provider for context {ctx.name}.','NW2004')
        for child in {c.symbol:c for c in d.children.values()}.values():ownership(child,available,[*stack,d.symbol])
    for root in roots:
        available=set();chain=[];parent=root.layout
        while parent:chain.insert(0,parent);parent=parent.layout
        for layout in chain:
            ownership(layout,available,[]);available|={c.symbol for c in layout.providers}
        ownership(root,available,[])
    return declarations,roots,routes,fallbacks


class Emitter:
    def __init__(self,d):self.d=d;self.lines=[];self.origins=[];self.index=0;self.line=d.line
    def add(self,text):
        self.lines.extend(text.split("\n"));self.origins.extend([(self.d.unit.key,self.line)]*len(text.split("\n")))
    def next(self,prefix):self.index+=1;return f'_{prefix}{self.index}'
    def static_html(self,node,ancestors=()):
        if node.tag not in F.TAGS or node.tag=='pre':return None
        # Keep direct-DOM behavior when HTML parsing would repair the tree.
        phrasing={'span','strong','em','small','code','br','input','button','label','output'}
        if node.tag in ('p','h1','h2','h3','h4','h5','h6','legend',*phrasing) and any(c.tag not in phrasing for c in node.children):return None
        if node.tag in ('ul','ol') and any(c.tag!='li' for c in node.children):return None
        if node.tag not in ('ul','ol') and any(c.tag=='li' for c in node.children):return None
        if node.tag=='form' and 'form' in ancestors:return None
        if node.tag=='button' and 'button' in ancestors:return None
        def literal(expr):
            if isinstance(expr,A.Str) and all(isinstance(p,str) for p in expr.parts):
                value=''.join(expr.parts)
                return None if any(c in value for c in ('\r','\0')) or any(0xd800<=ord(c)<=0xdfff for c in value) else value
            if isinstance(expr,A.Num) and not expr.is_float:return str(int(expr.text.replace('_','')))
            if isinstance(expr,A.Bool):return expr.value
            return None
        attrs={'type':'button'} if node.tag=='button' else {}
        for key,expr in node.attrs:
            value=literal(expr)
            if key.startswith('on_') or value is None:return None
            attrs[key.replace('_','-')]=value
        children=[]
        if node.text is not None:
            value=literal(node.text)
            if value is None:return None
            children.append(html.escape(str(value).lower() if isinstance(value,bool) else str(value)))
        for child in node.children:
            text=self.static_html(child,(*ancestors,node.tag))
            if text is None:return None
            children.append(text)
        attributes=''.join(' '+key if value is True else '' if value is False else ' '+key+'="'+html.escape(str(value),quote=True)+'"' for key,value in attrs.items())
        return '<'+node.tag+attributes+'>'+(''.join(children)+'</'+node.tag+'>' if node.tag not in F.VOID else '')
    def view(self,nodes,parent,owner='owner',api='api'):
        d=self.d
        for node in nodes:
            self.line=node.line
            symbols=node.symbols
            static=self.static_html(node)
            if static is not None:
                self.add(f'appendStatic({parent},{json.dumps(static)});')
                continue
            if node.tag=='#for' and node.key is not None:
                region=self.next('keyed');host=self.next('host');o=self.next('owner');a=self.next('api')
                local=dict(symbols)
                _,typ,_=F.value(node.text,symbols,node.line)
                local[node.variable]=Binding(typ[1],node.row_symbol+'.value',node.item_dep)
                key=F.value(node.key,local,node.line)[0]
                read,_,deps=F.value(node.text,symbols,node.line)
                self.add(f'const {region}=createKeyedRegion({parent},{owner},{json.dumps(node.item_dep)},({node.row_symbol})=>{key},({host},{o},{node.row_symbol})=>{{const {a}={o}.state(s);')
                for b in symbols.values():
                    if isinstance(b,Binding) and b.dep:self.add(f'{o}.link({json.dumps(b.dep)},{owner},{json.dumps(b.dep)});')
                    elif isinstance(b,Binding) and b.type=='context':
                        alias=next(k for k,v in symbols.items() if v is b)
                        for field in b.context.states:
                            dep='@'+alias+'.'+field;self.add(f'{o}.link({json.dumps(dep)},{owner},{json.dumps(dep)});')
                self.view(node.children,host,o,a)
                self.add(f'}});{api}.bind({json.dumps(sorted(deps))},()=>{read},items=>{region}.update(items));')
                continue
            if node.tag in ('#if','#for'):
                region=self.next('region');o=self.next('owner');a=self.next('api');host=self.next('host')
                self.add(f'const {region}=createRegion({parent},{owner});')
                if node.tag=='#if':
                    read='-1';deps=set()
                    for index,(condition,_) in reversed(list(enumerate(node.branches))):
                        if condition is None:read=str(index)
                        else:
                            js,_,names=F.value(condition,symbols,node.line);deps|=names;read=f'({js}?{index}:{read})'
                    self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>{read},choice=>{region}.replace(({host},{o})=>{{const {a}={o}.state(s);')
                else:
                    read,_,deps=F.value(node.text,symbols,node.line)
                    self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>{read},items=>{region}.replace(({host},{o})=>{{const {a}={o}.state(s);')
                for b in symbols.values():
                    if isinstance(b,Binding) and b.dep:self.add(f'{o}.link({json.dumps(b.dep)},{owner},{json.dumps(b.dep)});')
                    elif isinstance(b,Binding) and b.type=='context':
                        for field in b.context.states:
                            key=next((name for name,val in symbols.items() if val is b),'')
                            dep='@'+key+'.'+field;self.add(f'{o}.link({json.dumps(dep)},{owner},{json.dumps(dep)});')
                if node.tag=='#if':
                    for index,(_,body) in enumerate(node.branches):
                        self.add(f'if(choice==={index}){{');self.view(body,host,o,a);self.add('}')
                else:
                    self.add(f'for(const {node.item_symbol} of items){{');self.view(node.children,host,o,a);self.add('}')
                self.add('}));')
            elif node.tag=='slot':
                n=self.next('outlet')
                self.add(f'const {n}=document.createElement("div"); {parent}.append({n}); outlet={n};')
                self.add(f'if(renderSlot){{ const content=renderSlot({n}); {owner}.cleanup(()=>content.destroy()); }}')
            elif id(node) in d.children:
                child=d.children[id(node)];props,deps=prop_object(node,child,symbols)
                slot='null'
                if node.children:
                    fn=self.next('slot');o=self.next('owner');a=self.next('api');r=self.next('root')
                    self.add(f'const {fn}=(host)=>{{const {o}=createOwner(); const {a}={o}.state(s); const {r}=document.createElement("div"); {r}.dataset.nexo={json.dumps(d.scope)}; try{{')
                    # Lexical slot scope with its own lifetime. Link parent dependencies;
                    # no copy of state and no retention after the receiving owner dies.
                    for name,b in symbols.items():
                        if not isinstance(b,Binding):continue
                        if b.type!='context':self.add(f'{o}.link({json.dumps(b.dep or name)},{owner},{json.dumps(b.dep or name)});')
                        else:
                            for field in b.context.states:
                                key=f'@{name}.{field}';self.add(f'{o}.link({json.dumps(key)},{owner},{json.dumps(key)});')
                    self.view(node.children,r,o,a)
                    self.add(f'host.append({r}); return {{destroy(){{{o}.destroy(); {r}.remove();}}}}; }}catch(e){{{o}.destroy(); {r}.remove(); throw e;}} }};')
                    slot=fn
                n=self.next('child')
                self.add(f'const {n}={child.symbol}({parent},{props},env,{slot}); {owner}.cleanup(()=>{n}.destroy());')
                if deps:self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>{props},v=>{n}.update(v));')
            else:
                n=self.next('node')
                self.add(f'const {n}=document.createElement({json.dumps(node.tag)});')
                if node.tag=='button':self.add(f'{n}.type="button";')
                self.add(f'{parent}.append({n});')
                if node.text is not None:
                    value,_,deps=F.value(node.text,symbols,node.line);t=self.next('text')
                    self.add(f'const {t}=document.createTextNode(""); {n}.append({t});')
                    self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>String({value}),v=>{{{t}.data=v;}});' if deps else f'{t}.data=String({value});')
                for key,expr in node.attrs:
                    if key.startswith('on_'):
                        handler=D.method(expr,symbols,node.line) or ('a_'+expr.name if isinstance(expr,A.Name) else f'x[{json.dumps(expr.obj.name)}].actions[{json.dumps(expr.name)}]')
                        payload=',true' if isinstance(expr,A.Name) and expr.name in d.action_params else ''
                        self.add(f'{api}.listen({n},{json.dumps(key[3:])},{handler}{payload});')
                    else:
                        value,_,deps=F.value(expr,symbols,node.line)
                        if key in ('disabled','checked'):read=value;write=f'{n}.{key}=v;'
                        elif key=='value':read=f'String({value})';write=f'{n}.value=v;'
                        elif key=='href':read=f'routeHref({value})';write=f'{n}.setAttribute("href",v);'
                        else:read=f'String({value})';write=f'{n}.setAttribute({json.dumps(key.replace("_","-"))},v);'
                        self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>{read},v=>{{{write}}});' if deps else '{const v='+read+';'+write+'}')
                self.view(node.children,n,owner,api)

    def emit(self):
        d=self.d
        hot=self.hmr and d.kind!='context'
        self.add(f'function {d.symbol+("_impl" if hot else "")}(target,props={{}},parentEnv=new Map(),renderSlot=null'+(',retained={}' if hot else '')+'){')
        self.add(f'const s=checkedProps({schema(d)},props); const x=Object.create(null); const r=Object.create(null); const owner=createOwner(); const api=owner.state(s); const env=new Map(parentEnv); let outlet=null;')
        if d.kind!='context':self.add(f'const root=document.createElement("div"); root.dataset.nexo={json.dumps(d.scope)};')
        self.add('try{')
        if self.has_data:self.add('const dataScope=useDataScope(env,owner);')
        for ctx in d.providers:
            self.add(f'{{const provided={ctx.symbol}(); env.set({json.dumps(ctx.symbol)},provided); owner.cleanup(()=>provided.destroy());}}')
        for alias,ctx in d.injects.items():
            self.add(f'x[{json.dumps(alias)}]=injected(env,{json.dumps(ctx.symbol)});')
            for name in ctx.states:
                key=f'@{alias}.{name}'
                self.add(f'owner.link({json.dumps(key)},x[{json.dumps(alias)}],{json.dumps(name)});')
        for name,js in d.initials.items():
            self.line=d.states[name].line
            value=f'Object.hasOwn(retained,{json.dumps(name)})?retained[{json.dumps(name)}]:({js})' if hot else js
            self.add(f's[{json.dumps(name)}]={value};')
        for name,body in d.action_js.items():
            self.line=d.actions[name][0][1]-1
            self.add(f'const a_{name}=('+('event' if name in d.action_params else '')+')=>owner.batch(()=>{')
            for statement,(_,line) in zip(body,d.actions[name]):
                self.line=line;self.add(statement)
            self.add('});')
        for name,req in d.requests.items():
            self.line=req.line
            invalid='['+','.join('()=>r['+json.dumps(n)+'].reload()' for n in req.invalid)+']'
            self.add(f'r[{json.dumps(name)}]=createResource(dataScope,owner,{json.dumps("$"+name)},()=>{req.spec},{json.dumps(req.kind)},{invalid});')
        for name,req in d.requests.items():
            if req.kind=='load':self.add(f'api.bind({json.dumps(sorted(req.deps))},()=>{req.spec}.url,()=>r[{json.dumps(name)}].initial());')
        if d.kind=='context':
            methods=','.join(f'[{json.dumps(name)}]:a_{name}' for name in d.actions)
            self.add(f'return {{values:s,actions:{{{methods}}},subscribe:owner.subscribe,destroy:()=>owner.destroy()}};')
            self.add('}catch(e){owner.destroy(); throw e;} }')
        else:
            self.view(d.view,'root')
            if self.observe:self.add('owner.cleanup(()=>nexoObserve("mount",{phase:"disposed"}));')
            self.add('target.append(root);')
            if self.observe:self.add('nexoObserve("mount",{phase:"ready"});')
            snapshot='snapshot(){return {'+','.join('['+json.dumps(n)+']:s['+json.dumps(n)+']' for n in d.states)+'};},' if hot else ''
            self.add('return {'+snapshot+'element:root,env,outlet,update(props){const next=checkedProps('+schema(d)+',props); owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},destroy(){owner.destroy(); root.remove();}};')
            self.add('}catch(e){owner.destroy();root.remove();throw e;} }')
            if hot:
                key='Symbol.for("nexo.web.hmr")'
                self.add(f'function {d.symbol}(target,props={{}},env=new Map(),slot=null){{const hot=env.get({key});return hot?hot.mount({json.dumps(d.symbol)},{d.symbol}_impl,target,props,env,slot):{d.symbol}_impl(target,props,env,slot);}}')
        return '\n'.join(self.lines)


def compile_program(program, mapping=None, observe=False, css_mapping=None, hmr=None):
    namespace=(hmr or {}).get('scope_seed') or hashlib.sha256(json.dumps({'units':[(u.key,u.source) for u in program.units],'styles':program.css_sources},sort_keys=True).encode()).hexdigest()
    for unit in program.units:
        for d in unit.declarations.values():
            d.scope='nw-'+hashlib.sha256((namespace+'\n'+unit.key+'\n'+d.name).encode()).hexdigest()[:16]
    if hmr is not None:hmr['scope_seed']=namespace
    declarations,roots,routes,fallbacks=analyze(program)
    runtime=Path(__file__).with_name('web_runtime.js').read_text()
    if hmr is not None or any(node.tag=='#for' and node.key is not None for d in declarations for node in members(d.view)):
        runtime+='\n'+Path(__file__).with_name('web_keyed.js').read_text()
    has_data=any(d.requests for d in declarations)
    has_router=any(d.public and d.kind=='page' for d in declarations)
    if observe:runtime+='\n'+Path(__file__).with_name('web_observe.js').read_text()
    elif has_data or has_router or hmr is not None:runtime+='\nfunction nexoObserve(){}\n'
    if has_data or hmr is not None:
        runtime+='\n'+Path(__file__).with_name('web_data.js').read_text()
    if hmr is not None:runtime+='\n'+Path(__file__).with_name('web_router.js').read_text()
    js=[runtime];origins=[None]*len(runtime.split("\n"));css=[];css_origins=[]
    if hmr is not None:hmr.update(runtime=runtime,definitions={})
    for d in declarations:
        emitter=Emitter(d);emitter.has_data=has_data;emitter.observe=observe;emitter.hmr=hmr is not None
        emitted=emitter.emit();js.append(emitted);origins.extend(emitter.origins)
        if hmr is not None:
            signature=json.dumps({'kind':d.kind,'public':d.public,'route':d.route,'layout':d.layout.symbol if d.layout else None,'metadata':{k:F.value(expr,{n:b for n,b in d.symbols.items() if n in d.props},line)[0] for k,(expr,line) in d.metadata.items() if k in ('title','description')},'props':schema(d),'state':{k:p.type for k,p in d.states.items()},'providers':[c.symbol for c in d.providers],'injects':{a:c.symbol for a,c in d.injects.items()}},sort_keys=True)
            hmr['definitions'][d.symbol]={'kind':d.kind,'signature':signature,'fingerprint':hashlib.sha256(emitted.encode()).hexdigest()}
        if d.kind!='context':
            css.append(F.scoped_css(d.css,d.scope,d.line))
            css_origins.extend([None,*d.css_origins,None,None])
    for d in program.root.declarations.values():
        if d.public and d.kind!='context':js.append(f'export {{ {d.symbol} as {d.name} }};')
    pages=[d for d in declarations if d.kind=='page' and d.public]
    if pages:
        if hmr is None:js.append(Path(__file__).with_name('web_router.js').read_text())
        def descriptor(d):
            chain=[];parent=d.layout
            while parent:chain.insert(0,parent);parent=parent.layout
            metadata=[]
            for key in ('title','description'):
                if key in d.metadata:
                    expr,line=d.metadata[key];metadata.append(f'{key}:{F.value(expr,{n:b for n,b in d.symbols.items() if n in d.props},line)[0]}')
            layoutjs=','.join('{id:'+json.dumps(p.symbol)+',create:'+p.symbol+'}' for p in chain)
            return '{'+f'create:{d.symbol},layouts:[{layoutjs}],meta:(s)=>({{{",".join(metadata)}}})'+(','+','.join(f'{json.dumps(k)}:{json.dumps(v)}' for k,v in d.route.items()) if 'fallback' not in d.route else '')+'}'
        routes=sorted(routes,key=lambda d:-d.route['score'])
        js.append('export function mountApp(target,env=new Map()){return createRouter(target,['+','.join(descriptor(p) for p in routes)+'],{'+','.join(f'{key}:{descriptor(p)}' for key,p in fallbacks.items())+'},env);}')
        exports=['mountApp']
    else:exports=[d.name for d in roots]
    if mapping is not None:
        from .web_maps import source_map
        mapping.update(source_map(program,origins))
    if css_mapping is not None:
        from .web_maps import css_source_map
        css_mapping.update(css_source_map(program,css_origins))
    return '\n'.join(js)+'\n','\n'.join(css),exports
