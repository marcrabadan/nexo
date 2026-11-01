"""Evidence-bounded browser source audit. Never claims backend authorization."""
from dataclasses import fields
from pathlib import Path
import re

from . import ast as A
from .web_model import load_program
from .web_codegen import analyze

CREDENTIAL=re.compile(r'(?:^|_)(?:password|passwd|secret|token|api_key|private_key)(?:$|_)',re.I)


def walk(node):
    if isinstance(node,A.Node):
        yield node
        for field in fields(node):yield from walk(getattr(node,field.name))
    elif isinstance(node,(tuple,list)):
        for value in node:yield from walk(value)


def literal(expr):
    return isinstance(expr,A.Str) and all(isinstance(p,str) for p in expr.parts) and bool(''.join(expr.parts).strip())


def audit(file,security=True,accessibility=False):
    file=Path(file).resolve();program=load_program(file.read_text(encoding='utf-8'),file)
    declarations,_,_,_=analyze(program);out=[];seen=set()
    def finding(d,line,code,message):
        identity=(str(d.unit.path),line,code)
        if identity in seen:return
        seen.add(identity)
        out.append({'file':identity[0],'line':line,'column':1,'severity':'warning','code':code,
                    'message':message+' Fix: '+('move credentials to an authorized backend; use secure HttpOnly sessions.' if code=='NW4101' else 'review data sensitivity and keep cache_ms: 0 for sensitive data; destroy/remount on identity changes.'),
                    'snippet':'[redacted: security review location]'})
    if security:
        for d in declarations:
            for name,p in {**d.props,**d.states}.items():
                if CREDENTIAL.search(name) and literal(p.default):finding(d,p.line,'NW4101','Possible credential literal in a public browser bundle.')
            for body in d.actions.values():
                for node,line in body:
                    if isinstance(node,A.Assign) and isinstance(node.target,A.Name) and CREDENTIAL.search(node.target.name) and literal(node.value):finding(d,line,'NW4101','Possible credential assignment in a public browser bundle.')
            for req in d.requests.values():
                for node in walk(req.expr):
                    if isinstance(node,A.Arg) and node.name and CREDENTIAL.search(node.name) and literal(node.value):finding(d,req.line,'NW4101','Possible credential literal in a JSON request.')
                    if isinstance(node,A.Str) and re.search(r'[?&](?:access_token|token|api_key|password|secret)=',''.join(p if isinstance(p,str) else '*' for p in node.parts),re.I):finding(d,req.line,'NW4101','Possible credential in a URL query.')
                    if isinstance(node,A.Arg) and node.name=='cache_ms' and isinstance(node.value,A.Num) and int(node.value.text.replace('_',''))>0:finding(d,req.line,'NW4102','Opt-in cache needs an application-specific identity/sensitivity review.')
    if accessibility:
        from .web_accessibility import findings
        out.extend(findings(declarations))
    return {'schema_version':1,'target':'browser','diagnostics':out,
            'scope':'Static source review only; absence of findings is not a security certification.',
            'accessibility':'Static checks only' if accessibility else 'Not assessed',
            'backend_authorization':'Not assessed','sessions_csrf_rate_limits':'Backend validation required',
            'shield':{'browser_execution':'Unsupported','memory':'Backend in-process dispatch only',
                      'loopback':'Backend bounded ingress: 127.0.0.1, at most 8 connections, 3-second accept window, 6-second hard payload lifetime; not persistent production hosting',
                      'reference':'docs/shield-ingress.md'}}
