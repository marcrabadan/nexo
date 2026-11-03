"""Scalar and conditional hydration emitter. Existing client codegen remains unchanged."""
import hashlib
import json
from pathlib import Path

from . import web_frontend as F
from .web_codegen import Emitter, prop_object, schema
from .web_model import Binding
from .web_static import validate
from . import ast as A
from .web_data import json_schema


class HydrationEmitter(Emitter):
    def links(self, symbols, local, owner):
        for name, binding in symbols.items():
            if isinstance(binding, Binding) and binding.dep:
                self.add(f'{local}.link({json.dumps(binding.dep)},{owner},{json.dumps(binding.dep)});')
            elif isinstance(binding, Binding) and binding.type == 'context':
                for field in binding.context.states:
                    dep = '@' + name + '.' + field
                    self.add(f'{local}.link({json.dumps(dep)},{owner},{json.dumps(dep)});')

    def view(self, nodes, cursor, owner='owner', api='api'):
        d = self.d
        for node in nodes:
            symbols = node.symbols
            if node.tag == '#for':
                read, _, deps = F.value(node.text, symbols, node.line)
                region = self.next('list'); host = self.next('host')
                local = self.next('owner'); local_api = self.next('api')
                key = 'null' if node.key is None else f'({node.row_symbol})=>' + F.value(node.key, node.children[0].symbols, node.line)[0]
                self.add(f'const {region}=session.list({cursor},{owner},{node.line},()=>{read},{key},(session,{host},{node.row_symbol})=>{{const {local}=session.owner(),{local_api}=session.state({local},s);{node.row_symbol}.owner={local};')
                if node.key is None:
                    self.add(f'const {node.item_symbol}={node.row_symbol}.value;')
                self.links(symbols, local, owner)
                self.view(node.children, host, local, local_api)
                self.add(f'}}, {json.dumps(node.item_dep)});{api}.bind({json.dumps(sorted(deps))},()=>{read},items=>{region}.update(items));')
            elif node.tag == '#if':
                read = '-1'; deps = set()
                for index, (condition, _) in reversed(list(enumerate(node.branches))):
                    if condition is None:
                        read = str(index)
                    else:
                        js, _, names = F.value(condition, symbols, node.line)
                        deps |= names
                        read = f'({js}?{index}:{read})'
                region = self.next('region'); host = self.next('host')
                local = self.next('owner'); local_api = self.next('api')
                self.add(f'const {region}=session.region({cursor},{owner},{node.line},()=>{read},(session,{host},choice)=>{{const {local}=session.owner(),{local_api}=session.state({local},s);')
                self.links(symbols, local, owner)
                for index, (_, body) in enumerate(node.branches):
                    self.add(f'if(choice==={index}){{')
                    self.view(body, host, local, local_api)
                    self.add('}')
                self.add(f'}});{api}.bind({json.dumps(sorted(deps))},()=>{read},choice=>{region}.replace(choice));')
            elif node.tag == 'slot':
                outlet = self.next('outlet'); child_cursor = self.next('cursor')
                self.add(f'const {outlet}=session.element({cursor},"div",{{}});const {child_cursor}=session.cursor({outlet});')
                self.add(f'if(renderSlot)renderSlot(session,{child_cursor},env);session.end({child_cursor});')
            elif id(node) in d.children:
                child = d.children[id(node)]
                props, deps = prop_object(node, child, symbols)
                slot = 'null'
                if node.children:
                    slot = self.next('slot'); host = self.next('host'); root = self.next('root')
                    local = self.next('owner'); local_api = self.next('api'); local_cursor = self.next('cursor')
                    self.add(f'const {slot}=(session,{host})=>{{const {root}=session.element({host},"div",{{"data-nexo":{json.dumps(d.scope)}}});const {local_cursor}=session.cursor({root});const {local}=session.owner();const {local_api}=session.state({local},s);')
                    self.links(symbols, local, owner)
                    self.view(node.children, local_cursor, local, local_api)
                    self.add(f'session.end({local_cursor});}};')
                instance = self.next('child')
                self.add(f'const {instance}=h_{child.symbol}(session,{cursor},{props},{slot},env);')
                if deps:
                    self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>{props},value=>{instance}.update(value));')
            else:
                name = self.next('node'); child_cursor = self.next('cursor')
                attrs = {'type': '"button"'} if node.tag == 'button' else {}
                bindings = []
                for key, expr in node.attrs:
                    if key.startswith('on_'):
                        continue
                    js, _, deps = F.value(expr, symbols, node.line)
                    read = js if key in ('disabled', 'checked') else f'String({js})'
                    if key == 'href':
                        read = f'hydrationHref({js})'
                    attrs[key.replace('_', '-')] = read
                    if deps:
                        write = f'{name}.{key}=value;' if key in ('value', 'checked', 'disabled') else f'{name}.setAttribute({json.dumps(key.replace("_", "-"))},value);'
                        bindings.append(f'{api}.bind({json.dumps(sorted(deps))},()=>{read},value=>{{{write}}});')
                attributes = '{' + ','.join(json.dumps(k) + ':' + v for k, v in attrs.items()) + '}'
                self.add(f'const {name}=session.element({cursor},{json.dumps(node.tag)},{attributes});const {child_cursor}=session.cursor({name});')
                if node.text is not None:
                    js, _, deps = F.value(node.text, symbols, node.line)
                    text = self.next('text')
                    self.add(f'const {text}=session.text({child_cursor},String({js}));')
                    if deps:
                        self.add(f'{api}.bind({json.dumps(sorted(deps))},()=>String({js}),value=>{{{text}.data=value;}});')
                for binding in bindings:
                    self.add(binding)
                for key, expr in node.attrs:
                    if key.startswith('on_'):
                        handler = 'a_' + expr.name if isinstance(expr, A.Name) else f'x[{json.dumps(expr.obj.name)}].actions[{json.dumps(expr.name)}]'
                        payload = isinstance(expr, A.Name) and expr.name in d.action_params
                        self.add(f'{api}.listen({name},{json.dumps(key[3:])},{handler},{str(payload).lower()});')
                self.view(node.children, child_cursor, owner, api)
                self.add(f'session.end({child_cursor});')

    def emit(self):
        d = self.d
        context = d.kind == 'context'
        state_schema = json.dumps({name: json_schema(p.type) for name, p in d.states.items()})
        self.add(f'function h_{d.symbol}(session,target,props={{}},renderSlot=null,parentEnv=new Map()){{')
        self.add(f'const s=checkedProps({schema(d)},props),owner=session.owner(),api=session.state(owner,s),env=new Map(parentEnv),x=Object.create(null);')
        for ctx in d.providers:
            self.add(f'env.set({json.dumps(ctx.symbol)},h_{ctx.symbol}(session,null));')
        for alias, ctx in d.injects.items():
            self.add(f'x[{json.dumps(alias)}]=injected(env,{json.dumps(ctx.symbol)});')
            for field in ctx.states:
                self.add(f'owner.link({json.dumps("@"+alias+"."+field)},x[{json.dumps(alias)}],{json.dumps(field)});')
        if not context:
            self.add(f'const root=session.element(target,"div",{{"data-nexo":{json.dumps(d.scope)}}}),cursor=session.cursor(root);')
        for name, js in d.initials.items():
            initial = f'Object.hasOwn(session.entryState,{json.dumps(name)})?session.entryState[{json.dumps(name)}]:({js})' if d.kind == 'page' else js
            self.add(f's[{json.dumps(name)}]={initial};')
        for name, body in d.action_js.items():
            argument = 'event' if name in d.action_params else ''
            statements = ''.join(statement.replace('navigate(', 'hydrationNavigate(', 1) if isinstance(stmt, A.ExprStmt) and isinstance(stmt.expr, A.Call) and isinstance(stmt.expr.func, A.Name) and stmt.expr.func.name == 'navigate' else statement for statement, (stmt, _) in zip(body, d.actions[name]))
            self.add(f'const a_{name}=({argument})=>owner.batch(()=>{{' + statements + '});')
        methods = ','.join('['+json.dumps(name)+']:a_'+name for name in d.actions)
        if not context:
            self.view(d.view, 'cursor')
            self.add('session.end(cursor);')
        self.add('const stateSchema=JSON.parse('+json.dumps(state_schema)+');')
        self.add('const controller={env,values:s,actions:{'+methods+'},subscribe:owner.subscribe,update(props){const next=checkedProps('+schema(d)+',props);owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},setState(patch){const next=m6Patch(stateSchema,patch);owner.batch(()=>{for(const key of Object.keys(next))api.set(key,next[key]);});},snapshot(){return Object.freeze(Object.fromEntries(Object.keys(stateSchema).map(key=>[key,s[key]])));}};')
        if d.kind == 'page':
            self.add('session.entry=controller;')
        self.add('return controller;}')
        return '\n'.join(self.lines)


def compile_hydration(program, *, wasm=False, subscriptions=False):
    validate(program, routed=True, allow_error=True, interactive=True)
    declarations = [d for unit in program.units for d in unit.declarations.values()]
    root = Path(__file__).parent
    js = (root/'web_runtime.js').read_text(encoding='utf-8') + '\n' + (root/'web_m6.js').read_text(encoding='utf-8') + '\n' + (root/'web_hydrate.js').read_text(encoding='utf-8')
    js += '\n' + '\n'.join(HydrationEmitter(d).emit() for d in declarations)
    pages = [d for d in declarations if d.kind == 'page' and d.public]
    descriptors = []
    for page in pages:
        call = f'h_{page.symbol}(session,target,props,null,env)'
        parent = page.layout
        while parent:
            call = f'h_{parent.symbol}(session,target,{{}},(session,target,env)=>{call},env)'
            parent = parent.layout
        types = '{' + ','.join('[' + json.dumps(k) + ']:' + json.dumps(p.type) for k, p in page.props.items()) + '}'
        states = json.dumps({name: json_schema(p.type) for name, p in page.states.items()})
        descriptors.append(json.dumps(page.symbol) + f':{{types:{types},states:JSON.parse({json.dumps(states)}),create:(session,target,props)=>{{const env=new Map();return {call};}}}}')
    js += '\nconst hydrationPages=Object.freeze({' + ','.join(descriptors) + '});\n'
    contexts = [d for d in declarations if d.kind == 'context']
    names = {d.unit.key + '::' + d.name: d.symbol for d in contexts}
    for d in contexts:
        if sum(other.name == d.name for other in contexts) == 1:
            names[d.name] = d.symbol
    js += 'const hydrationContexts=JSON.parse(' + json.dumps(json.dumps(names)) + ');\n'
    js += 'const hydrationRoutes=' + json.dumps([p.route['parts'] for p in pages if 'fallback' not in p.route]) + ';\n'
    worker = (root/'web_wasm_worker.js').read_bytes() if wasm else b''
    js += 'const hydrationWorkerURL=' + json.dumps('/_nexo/wasm-worker-' + hashlib.sha256(worker).hexdigest() + '.js') + ';\n'
    js += 'const hydrationCapabilities=Object.freeze(' + json.dumps({'wasm':wasm,'subscriptions':subscriptions}) + ');\n'
    tail = 'export {loadWasm,connectEvents};\nexport function hydrate(target){return hydrateRoot(target,"@BUILD@");}\n'
    tail += 'const target=document.querySelector("#app");if(target){try{hydrate(target);target.dataset.nexoHydration="ready";}catch{target.dataset.nexoHydration="mismatch";}}\n'
    build = hashlib.sha256((js + tail).encode('utf-8')).hexdigest()
    return build, js + tail.replace('"@BUILD@"', json.dumps(build), 1)
