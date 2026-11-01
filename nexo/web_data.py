"""M3 JSON contracts, request semantics and typed resource access."""
from dataclasses import dataclass, replace
import json

from . import ast as A
from . import web_frontend as F
from .web_model import Binding, reference


@dataclass
class Request:
    kind: str
    expr: A.Expr
    line: int
    response: object = None


def contract(program, unit, expr, line, stack=()):
    if isinstance(expr, A.TypeName):
        if expr.optional or expr.fallible or expr.weak or expr.rel:
            F.fail(line, 'JSON contracts require non-optional scalar, record or list fields.', 'NW3001')
        if expr.name == 'list' and len(expr.params) == 1:
            return ('list', contract(program, unit, expr.params[0], line, stack))
        if expr.params:
            F.fail(line, 'Unsupported JSON type parameters.', 'NW3001')
        name = expr.name
    elif isinstance(expr, A.Index) and isinstance(expr.obj, A.Name) and expr.obj.name == 'list':
        return ('list', contract(program, unit, expr.index, line, stack))
    else:
        name = reference(expr, line)
    if name in ('int', 'str', 'bool'):
        return name
    parts = name.split('.')
    target = unit
    if len(parts) == 2 and parts[0] in unit.imports:
        target = unit.imports[parts[0]]
    elif len(parts) != 1:
        F.fail(line, 'Unknown JSON record type.', 'NW3001')
    node = target.contracts.get(parts[-1])
    if node is None or (target is not unit and not node.pub):
        F.fail(line, 'Unknown or private JSON record type: ' + name, 'NW3001')
    identity = target.key + '.' + node.name
    if identity in stack:
        F.fail(line, 'Recursive JSON contracts are not supported.', 'NW3001')
    fields = tuple((p.name, contract(program, target, p.type, line, (*stack, identity))) for p in node.fields)
    return ('record', identity, fields)


def json_schema(typ):
    if isinstance(typ, str): return {'kind': typ}
    if typ[0] == 'list': return {'kind': 'list', 'item': json_schema(typ[1])}
    return {'kind': 'record', 'fields': {name: json_schema(t) for name, t in typ[2]}}


def schema_js(typ):
    # Parse JSON data instead of treating __proto__ as JavaScript object syntax.
    return 'JSON.parse('+json.dumps(json.dumps(json_schema(typ)))+')'


def install_contracts(program, d):
    # Constructor bindings are compiler metadata, never mutable browser values.
    for name in d.unit.contracts:
        d.symbols['$type:' + name] = contract(program, d.unit, A.Name(None, name), d.line)
    for alias, unit in d.unit.imports.items():
        for name, node in unit.contracts.items():
            if node.pub:
                key = alias + '.' + name
                d.symbols['$type:' + key] = contract(program, d.unit, A.Attr(None, A.Name(None, alias), name, None), d.line)


def constructor(expr, symbols, line):
    if not isinstance(expr, A.Call): return None
    if not isinstance(expr.func, (A.Name, A.Attr)): return None
    name = reference(expr.func, line)
    typ = symbols.get('$type:' + name)
    if typ is None: return None
    supplied = {}
    deps = set()
    fields = dict(typ[2])
    for arg in expr.args:
        if arg.name not in fields or arg.name in supplied:
            F.fail(line, 'Record constructors require each declared named field exactly once.', 'NW3001')
        js, actual, names = F.value(arg.value, symbols, line)
        if actual != fields[arg.name]: F.fail(line, 'JSON request field type mismatch.', 'NW3001')
        supplied[arg.name] = js; deps |= names
    if set(supplied) != set(fields): F.fail(line, 'Missing JSON request field.', 'NW3001')
    return 'Object.freeze({' + ','.join('[' + json.dumps(k) + ']:' + v for k,v in supplied.items()) + '})', typ, deps


def resource_value(expr, symbols, line):
    if not isinstance(expr, A.Attr) or not isinstance(expr.obj, A.Name): return None
    binding = symbols.get(expr.obj.name)
    if not isinstance(binding, Binding) or binding.type != 'resource': return None
    req = binding.context
    fields = {'loading': 'bool', 'ready': 'bool', 'success': 'bool', 'error': 'error?', 'data': req.response}
    if expr.name not in fields: F.fail(line, 'Unknown request resource field.', 'NW3002')
    if expr.name == 'data' and not getattr(binding, 'ready', False):
        F.fail(line, 'Read resource data only inside if/elif resource.ready (or mutation.success).', 'NW3002')
    if expr.name == 'error' and getattr(binding, 'error_ready', False):
        typ = ('record', 'RequestError', (('kind', 'str'), ('message', 'str'), ('status', 'int')))
    else: typ = fields[expr.name]
    return binding.code + '.' + expr.name, typ, {binding.dep}


def narrowed(symbols, condition):
    out = dict(symbols)
    target = None; flag = None
    if isinstance(condition, A.Attr) and condition.name in ('ready', 'success'):
        target=condition.obj; flag='ready'
    if isinstance(condition, A.Compare) and condition.ops == ['!='] and isinstance(condition.operands[1], A.NoneLit) and isinstance(condition.operands[0], A.Attr) and condition.operands[0].name == 'error':
        target=condition.operands[0].obj; flag='error_ready'
    if isinstance(target, A.Name) and target.name in symbols:
        old=symbols[target.name]
        if isinstance(old,Binding) and old.type=='resource':
            new=replace(old); setattr(new,flag,True); out[target.name]=new
    return out


def method(expr, symbols, line):
    if isinstance(expr, A.Attr) and isinstance(expr.obj, A.Name):
        b=symbols.get(expr.obj.name)
        if isinstance(b,Binding) and b.type=='resource':
            allowed=('reload','cancel') if b.context.kind=='load' else ('run','cancel')
            if expr.name not in allowed: F.fail(line, 'Invalid load/mutation operation.', 'NW3002')
            return b.code+'.'+expr.name
    return None


def request(program, d, name, req):
    expr=req.expr; line=req.line
    if not isinstance(expr,A.Call) or not isinstance(expr.func,A.Attr) or not isinstance(expr.func.obj,A.Name) or expr.func.obj.name!='api':
        F.fail(line, 'Use api.get/post/put/patch/delete in load or mutation.', 'NW3002')
    verb=expr.func.name
    if (req.kind=='load' and verb!='get') or (req.kind=='mutation' and verb not in ('post','put','patch','delete')):
        F.fail(line, 'load requires GET; mutation requires POST, PUT, PATCH or DELETE.', 'NW3002')
    if not expr.args or expr.args[0].name is not None:
        F.fail(line,'A request requires one positional local URL.', 'NW3002')
    options={}
    for arg in expr.args[1:]:
        if arg.name not in ('as','body','timeout_ms','retries','cache_ms','dedupe','invalidate') or arg.name in options:
            F.fail(line,'Unsupported or duplicate request option.', 'NW3002')
        options[arg.name]=arg.value
    if 'as' not in options: F.fail(line,'A request requires as: ResponseType.', 'NW3001')
    req.response=contract(program,d.unit,options.pop('as'),line)
    url,typ,deps=F.value(expr.args[0].value,d.symbols,line)
    if typ!='str': F.fail(line,'API URL requires str.', 'NW3002')
    # Runtime performs the same check after every reactive interpolation.
    literal=expr.args[0].value
    if isinstance(literal,A.Str) and all(isinstance(p,str) for p in literal.parts):
        path=''.join(literal.parts)
        if not path.startswith('/') or path.startswith('//') or any(c in path for c in '\\#') or any(ord(c)<=32 or ord(c)==127 for c in path):
            F.fail(line,'API URL must be a same-origin absolute path.', 'NW3003')
    fields={'method':json.dumps(verb.upper()),'schema':schema_js(req.response), 'url':url}
    if 'body' in options:
        if req.kind=='load': F.fail(line,'GET cannot have a body.', 'NW3002')
        js,t,names=F.value(options.pop('body'),d.symbols,line); deps|=names
        fields['body']=js; fields['bodySchema']=schema_js(t)
    if verb in ('post','put','patch') and 'body' not in fields: F.fail(line,'POST/PUT/PATCH require a typed body.', 'NW3001')
    for key,default,low,high in [('timeout_ms',10000,1,120000),('retries',0,0,3),('cache_ms',0,0,60000)]:
        val=options.pop(key,None)
        if val is None: number=default
        elif isinstance(val,A.Num) and not val.is_float: number=int(val.text.replace('_',''))
        else: F.fail(line,key+' requires a bounded integer literal.', 'NW3002')
        if not low<=number<=high or (req.kind=='mutation' and key in ('retries','cache_ms') and number):
            F.fail(line,'Invalid request bound; mutations cannot retry or cache.', 'NW3002')
        fields[key]=str(number)
    dedupe=options.pop('dedupe',A.Bool(None,True))
    if not isinstance(dedupe,A.Bool) or (req.kind=='mutation' and not dedupe.value): F.fail(line,'dedupe requires a bool literal (GET only).', 'NW3002')
    fields['dedupe']=str(dedupe.value).lower()
    targets=options.pop('invalidate',A.ListLit(None,[]))
    if not isinstance(targets,A.ListLit) or (req.kind=='load' and targets.items): F.fail(line,'invalidate requires a list of local load names on a mutation.', 'NW3002')
    invalid=[]
    for target in targets.items:
        if not isinstance(target,A.Name) or target.name not in d.requests or d.requests[target.name].kind!='load': F.fail(line,'Invalid mutation invalidation target.', 'NW3002')
        invalid.append(target.name)
    # Reads cannot depend on another resource: avoids request cycles and absent data.
    if any(dep.startswith('$') for dep in deps): F.fail(line,'Request inputs may depend on props/local/context state, not another request.', 'NW3002')
    req.spec='({'+','.join(key+':'+value for key,value in fields.items())+'})'
    req.deps=deps; req.invalid=invalid
