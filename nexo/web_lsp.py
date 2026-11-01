"""Explicit Python browser LSP; core/Rust LSP behavior is left unchanged."""
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit,unquote

from . import lsp as core
from .web_frontend import compile_web,WebError,TAGS
from .web_model import Unit,parse_unit
from .web_diagnostics import diagnostic

WORDS='component page layout context state fn view style provide inject slot load mutation api get post put patch delete loading ready success error data reload cancel run navigate if elif else for pub use type int str bool list true false none'.split()


def diagnostics(text,name):
    try:compile_web(text,name);return []
    except (WebError,OSError,UnicodeError) as error:
        d=diagnostic(error,name,text)
        imported=Path(d['file']).resolve()!=Path(name).resolve()
        if imported:
            local_range=core.lsp_range(text.split('\n'),1,1,1,1)
        else:local_range=core.lsp_range(text.split('\n'),d['line'],d['column'],d['line'],d['end_column'])
        result={'range':local_range,'severity':1,'source':'nexo-web','code':d['code'],'message':d['message']}
        if imported:
            try:lines=Path(d['file']).read_text().split('\n')
            except (OSError,UnicodeError):lines=['']
            result['relatedInformation']=[{'location':{'uri':Path(d['file']).resolve().as_uri(),'range':core.lsp_range(lines,d['line'],d['column'],d['line'],d['end_column'])},'message':'Browser dependency diagnostic'}]
        return [result]


def outline(text,name):
    try:
        unit=Unit('',Path(name),text);parse_unit(unit)
    except (WebError,OSError,UnicodeError):return []
    lines=text.split('\n');out=[]
    def item(label,line,kind,detail):
        match=re.search(r'\b'+re.escape(label)+r'\b',lines[line-1])
        col=match.start()+1 if match else 1
        return {'name':label,'kind':kind,'detail':detail,'range':core.lsp_range(lines,line,1,line,len(lines[line-1])+1),
                'selectionRange':core.lsp_range(lines,line,col,line,col+len(label))}
    for record in unit.contracts.values():out.append(item(record.name,getattr(record,'web_line',record.span.line),23,'JSON record'))
    for d in unit.declarations.values():
        symbol=item(d.name,d.line,23,d.kind);children=[]
        for key,p in {**d.props,**d.states}.items():children.append(item(key,p.line,8,str(p.type)))
        for key,body in d.actions.items():children.append(item(key,body[0][1]-1,12,'fn '+key+'()'))
        for key,req in d.requests.items():children.append(item(key,req.line,13,req.kind))
        symbol['children']=children;out.append(symbol)
    out.sort(key=lambda v:v['range']['start']['line'])
    for index,symbol in enumerate(out):
        end=(out[index+1]['range']['start']['line']-1) if index+1<len(out) else len(lines)-1
        while end>symbol['range']['start']['line'] and not lines[end].strip():end-=1
        symbol['range']['end']={'line':end,'character':core.utf16_col(lines[end],len(lines[end])+1)}
    return out


class Server(core.Server):
    def name(self,uri):
        return unquote(urlsplit(uri).path) if uri.startswith('file:') else super().name(uri)
    def publish(self,uri):
        text=self.docs.get(uri)
        self.send({'jsonrpc':'2.0','method':'textDocument/publishDiagnostics','params':{'uri':uri,'diagnostics':diagnostics(text,self.name(uri)) if text is not None else []}})
    def handle(self,method,params):
        if method=='initialize':
            result=super().handle(method,params)
            result['capabilities']['documentFormattingProvider']=False
            result['capabilities']['codeActionProvider']=False
            result['serverInfo']['name']='nexo-web'
            return result
        uri=(params.get('textDocument') or {}).get('uri','') if params else ''
        text=self.docs.get(uri)
        if method in ('textDocument/formatting','textDocument/codeAction'):return []
        if method=='textDocument/completion':
            names=set(WORDS)|TAGS
            if text is not None:
                for entry in outline(text,self.name(uri)):
                    names.add(entry['name']);names.update(child['name'] for child in entry.get('children',[]))
            return [{'label':name,'kind':14 if name in WORDS else 6} for name in sorted(names)]
        if method=='textDocument/documentSymbol':return outline(text,self.name(uri)) if text is not None else []
        if method in ('textDocument/definition','textDocument/hover'):
            if text is None:return None
            line,col=self.position(uri,params);lines=text.split('\n')
            if not 1<=line<=len(lines):return None
            word=next((m.group() for m in re.finditer(r'[A-Za-z_][A-Za-z_0-9]*',lines[line-1]) if m.start()<=col-1<m.end()),None)
            top=outline(text,self.name(uri));local=[]
            for i,entry in enumerate(top):
                if entry['range']['start']['line']<=line-1<(top[i+1]['range']['start']['line'] if i+1<len(top) else len(lines)):local=entry.get('children',[])
            found=next((v for v in [*local,*top] if v['name']==word),None)
            if found:
                if method.endswith('definition'):return {'uri':uri,'range':found['selectionRange']}
                detail=found['detail'] if found['detail'].startswith('fn ') else found['detail']+' '+found['name']
                return {'contents':{'kind':'markdown','value':'```nexo\n'+detail+'\n```'},'range':found['selectionRange']}
            return None
        return super().handle(method,params)


def serve():return Server(sys.stdin.buffer,sys.stdout.buffer).run()
