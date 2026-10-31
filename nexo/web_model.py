"""Browser typed declaration model, contextual parser and D124 project loader."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re

from . import ast as A
from . import web_frontend as F
from .modules import project_root, shown_path

SCALARS = {'int', 'str', 'bool'}


@dataclass
class Property:
    type: str
    default: A.Expr | None
    line: int


@dataclass
class Binding:
    type: str
    code: str
    dep: str | None = None
    writable: bool = False
    context: Declaration | None = None


@dataclass
class Declaration:
    name: str
    kind: str
    public: bool
    line: int
    unit: Unit
    props: dict[str, Property] = field(default_factory=dict)
    states: dict[str, Property] = field(default_factory=dict)
    actions: dict[str, list] = field(default_factory=dict)
    provides: list = field(default_factory=list)
    injections: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    view: list = field(default_factory=list)
    css: str = ''
    scope: str = ''
    symbol: str = ''
    slots: int = 0


@dataclass
class Unit:
    key: str
    path: Path | None
    source: str
    declarations: dict = field(default_factory=dict)
    imports: dict = field(default_factory=dict)


@dataclass
class Program:
    root: Unit
    units: list[Unit]
    dependencies: set[Path]

    def resolve(self, unit, name, line, kind=None):
        pieces = name.split('.')
        if len(pieces) == 1:
            declaration = unit.declarations.get(name)
        elif len(pieces) == 2 and pieces[0] in unit.imports:
            declaration = unit.imports[pieces[0]].declarations.get(pieces[1])
            if declaration and not declaration.public:
                F.fail(line, f'{name} is private; export it with pub.', 'NW2001')
        else:
            declaration = None
        if declaration is None:
            F.fail(line, f'Unknown browser declaration {name}.', 'NW2001')
        if kind and declaration.kind != kind:
            F.fail(line, f'{name} must be a {kind}.', 'NW2002')
        return declaration


def reference(expr, line):
    if isinstance(expr, A.Name):
        return expr.name
    if isinstance(expr, A.Attr) and isinstance(expr.obj, A.Name):
        return expr.obj.name + '.' + expr.name
    F.fail(line, 'Expected a declaration name, qualified for imported modules.', 'NW2001')


def identifier(name, line):
    if not re.fullmatch(F.IDENT, name) or name in F.KEYWORDS:
        F.fail(line, 'State, property, action and binding names cannot be reserved words.')


def properties(text, line):
    if text is None:
        return {}
    if not text:
        F.fail(line, 'Omit empty property parentheses.')
    try:
        params = F.core('fn __props(' + text + '):\n    pass', line).params
    except AttributeError:
        F.fail(line, 'Invalid typed properties.', 'NW2002')
    out = {}
    for param in params:
        identifier(param.name, line)
        typ = param.type
        if not isinstance(typ, A.TypeName) or typ.name not in SCALARS or typ.params or typ.optional or typ.fallible or typ.weak:
            F.fail(line, 'Properties require an explicit int, str or bool type.', 'NW2002')
        if param.name in out:
            F.fail(line, 'Duplicate property.', 'NW2002')
        if param.default is not None:
            _, actual, deps = F.value(param.default, {}, line)
            if actual != typ.name or deps:
                F.fail(line, 'Property defaults must be constants of the declared type.', 'NW2002')
        out[param.name] = Property(typ.name, param.default, line)
    return out


def element(text, line):
    block = text.endswith(':')
    expr = F.expression(text[:-1] if block else text, line)
    if isinstance(expr, (A.Name, A.Attr)) and block:
        tag, args = reference(expr, line), []
    elif isinstance(expr, A.Call):
        tag, args = reference(expr.func, line), expr.args
    else:
        F.fail(line, 'Expected an element/component call or block.')
    node = F.Element(tag, line=line)
    named = set()
    for arg in args:
        if arg.name is None:
            if node.text is not None or named:
                F.fail(line, 'Use at most one positional text value before named arguments.')
            node.text = arg.value
        else:
            if arg.name in named:
                F.fail(line, 'Duplicate attribute/property.')
            named.add(arg.name)
            node.attrs.append((arg.name, arg.value))
    return node, block


def parse_unit(unit):
    lines = unit.source.splitlines()
    for line, text in enumerate(lines, 1):
        if '\t' in text:
            F.fail(line, 'Use spaces instead of tabs.')
    items = [(i+1, len(s)-len(s.lstrip(' ')), s.strip()) for i,s in enumerate(lines) if s.strip() and not s.lstrip().startswith('#')]
    if not items or items[0][1:] != (0, 'use nexo.web'):
        F.fail(1, 'Browser files must start with use nexo.web.')
    uses, i = [], 1
    while i < len(items):
        line, indent, text = items[i]
        if not indent and text.startswith('use '):
            if unit.declarations:
                F.fail(line, 'Place imports before declarations.', 'NW2001')
            node = F.core(text, line)
            if not isinstance(node, A.Use) or node.lang or node.name == 'nexo' or node.name.startswith('nexo.'):
                F.fail(line, 'Browser files cannot import server/native capabilities.', 'NW1004')
            alias = node.alias or node.name.rsplit('.', 1)[-1]
            identifier(alias, line)
            if any(alias == old[1] for old in uses):
                F.fail(line, 'Duplicate import alias.', 'NW2001')
            uses.append((node.name, alias, line))
            i += 1
            continue
        match = re.fullmatch(r'(pub )?(component|page|layout|context) ([A-Z][A-Za-z_0-9]*)(?:\((.*)\))?:', text)
        if indent or not match:
            F.fail(line, 'Expected component, page, layout or context with an uppercase name.')
        public, kind, name, params = match.groups()
        if name in unit.declarations or any(name == alias for _,alias,_ in uses):
            F.fail(line, 'Duplicate declaration/import name.', 'NW2001')
        if params is not None and kind not in ('component','page'):
            F.fail(line, 'Only components and pages declare scalar properties.', 'NW2002')
        d = Declaration(name, kind, bool(public), line, unit, props=properties(params, line))
        unit.declarations[name] = d
        seen = set(d.props)
        i += 1
        while i < len(items) and items[i][1]:
            line, indent, text = items[i]
            if indent != 4:
                F.fail(line, 'Declaration members require four spaces.')
            state = re.fullmatch(r'state ('+F.IDENT+r'): (int|str|bool) = (.+)', text)
            action = re.fullmatch(r'fn ('+F.IDENT+r')\(\):', text)
            if state:
                key,typ,initial = state.groups()
                identifier(key, line)
                if key in seen:
                    F.fail(line, 'Duplicate member.')
                d.states[key] = Property(typ, F.expression(initial,line), line)
                seen.add(key)
                i += 1
            elif action:
                key = action[1]
                identifier(key, line)
                if key in seen:
                    F.fail(line, 'Duplicate member.')
                seen.add(key)
                i += 1
                body = []
                while i < len(items) and items[i][1] > 4:
                    ln,ind,statement = items[i]
                    if ind != 8:
                        F.fail(ln, 'Action statements require eight spaces.')
                    body.append((F.core(statement,ln),ln))
                    i += 1
                if not body:
                    F.fail(line, 'An action requires a statement.')
                d.actions[key] = body
            elif text == 'view:':
                if d.kind == 'context' or 'view' in seen:
                    F.fail(line, 'Contexts have no view; UI declarations have one view.')
                seen.add('view')
                i += 1
                stack = [(4,d.view)]
                while i < len(items) and items[i][1] > 4:
                    ln,ind,statement = items[i]
                    while stack and ind <= stack[-1][0]:
                        stack.pop()
                    if not stack or ind != stack[-1][0]+4:
                        F.fail(ln, 'View indentation requires four spaces per level.')
                    node,block = element(statement,ln)
                    stack[-1][1].append(node)
                    if block:
                        stack.append((ind,node.children))
                    i += 1
            elif text == 'style:':
                if d.kind == 'context' or 'style' in seen:
                    F.fail(line, 'Contexts have no style; UI declarations have at most one style.')
                seen.add('style')
                start = line
                i += 1
                while i < len(items) and items[i][1] > 4:
                    i += 1
                end = items[i][0]-1 if i<len(items) else len(lines)
                d.css = '\n'.join(s[8:] if s.startswith('        ') else s for s in lines[start:end]).strip()
            elif text.startswith('provide '):
                if d.kind == 'context':
                    F.fail(line, 'Contexts cannot own other contexts in M2.', 'NW2004')
                d.provides.append((reference(F.expression(text[8:],line),line),line))
                i += 1
            else:
                node = F.core(text,line)
                if not isinstance(node,A.Assign) or node.op!='=' or not isinstance(node.target,A.Name):
                    F.fail(line, 'Unsupported declaration member.')
                key = node.target.name
                if isinstance(node.value,A.Call) and isinstance(node.value.func,A.Name) and node.value.func.name=='inject':
                    identifier(key,line)
                    if d.kind=='context' or key in seen or len(node.value.args)!=1 or node.value.args[0].name:
                        F.fail(line, 'Use a unique binding = inject(Context).', 'NW2004')
                    d.injections[key]=(reference(node.value.args[0].value,line),line)
                    seen.add(key)
                elif key in ('path','layout','title','description','not_found','error') and d.kind in ('page','layout'):
                    if key in d.metadata or (d.kind=='layout' and key!='layout'):
                        F.fail(line, 'Unsupported or duplicate routing metadata.', 'NW2003')
                    d.metadata[key]=(node.value,line)
                else:
                    F.fail(line, 'Unsupported declaration member.')
                i += 1
        if d.kind!='context' and not d.view:
            F.fail(d.line,'A UI declaration requires a nonempty view.')
        if d.kind=='context' and not d.states:
            F.fail(d.line,'A context requires typed state.', 'NW2004')
        suffix=hashlib.sha256((unit.key+'\n'+name).encode()).hexdigest()[:16]
        d.symbol='_nw_'+suffix
        d.scope='nw-'+hashlib.sha256((unit.key+'\n'+name+'\n'+unit.source).encode()).hexdigest()[:16]
    return uses


def load_program(source, source_path=None):
    root_path=Path(source_path).resolve() if source_path else None
    root_dir=project_root(str(root_path)) if root_path else None
    units, by_path, visiting, dependencies = [], {}, [], set()
    if root_dir and (root_dir/'nexo.toml').is_file():
        dependencies.add(root_dir/'nexo.toml')

    def inside(path,line):
        if not root_dir or not path.is_relative_to(root_dir):
            F.fail(line, 'Browser imports must remain inside the project root.', 'NW2001')
        return path

    def css_expand(text,folder,line,stack):
        pattern=r'@import\s+"([^"\n]+)"\s*;'
        def replace(match):
            name=match[1]
            if ':' in name or name.startswith(('/', '\\')) or '\\' in name:
                F.fail(line,'External CSS resources are outside the browser subset.', 'NW1003')
            if folder is None:
                F.fail(line,'Local CSS imports require a source file path.', 'NW2001')
            path=inside((folder/name).resolve(),line)
            if path.suffix!='.css':
                F.fail(line,'Style imports must name a local .css file.', 'NW2001')
            if path in stack:
                F.fail(line,'CSS import cycle.', 'NW2001')
            dependencies.add(path)
            return css_expand(path.read_text(encoding='utf-8'),path.parent,line,[*stack,path])
        # Strip no comments here: @import in comments/strings must not read files.
        pieces=re.findall(r'/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|@import\s+"[^"\n]+"\s*;|[^"\'/@]+|.',text)
        return ''.join(re.sub(pattern,replace,p) if p.startswith('@import') else p for p in pieces)

    def visit(text,path,key):
        if path and path in visiting:
            F.fail(1,'Browser module cycle: '+' -> '.join(p.name for p in [*visiting,path]),'NW2001')
        if path and path in by_path:
            return by_path[path]
        unit=Unit(key,path,text)
        if path:
            by_path[path]=unit
            visiting.append(path)
            dependencies.add(path)
        try:
            uses=parse_unit(unit)
            for module,alias,line in uses:
                if not path:
                    F.fail(line,'Module imports require a source file path.', 'NW2001')
                if not all(re.fullmatch(F.IDENT,p) for p in module.split('.')):
                    F.fail(line,'Invalid project module name.', 'NW2001')
                target=inside(root_dir.joinpath(*module.split('.')).with_suffix('.nexo').resolve(),line)
                unit.imports[alias]=visit(target.read_text(encoding='utf-8'),target,module)
            for d in unit.declarations.values():
                d.css=css_expand(d.css,path.parent if path else None,d.line,[])
                F.scoped_css(d.css,d.scope,d.line)
        except F.WebError as error:
            if not getattr(error,'file',None):error.file=shown_path(path) if path else '<web>'
            raise
        except OSError as error:
            F.fail(1,f'Cannot read browser dependency: {error}.','NW2001')
        finally:
            if path:visiting.pop()
        units.append(unit)
        return unit

    root=visit(source,root_path,'')
    return Program(root,units,dependencies)
