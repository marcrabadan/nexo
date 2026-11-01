"""Shared browser diagnostics for CLI, Vite and the explicit browser LSP."""
from pathlib import Path


def diagnostic(error,entry,source=None):
    file=getattr(error,'file',None) or str(entry)
    if file=='<web>':file=str(entry)
    text=source or ''
    try:
        if Path(file).is_file() and (source is None or Path(file).resolve()!=Path(entry).resolve()):text=Path(file).read_text(encoding='utf-8')
    except (OSError,UnicodeError):pass
    lines=text.splitlines();line=max(1,getattr(error,'line',1))
    snippet=lines[line-1] if line<=len(lines) else ''
    column=len(snippet)-len(snippet.lstrip())+1
    return {'code':getattr(error,'code','NW1000'),'file':file,'line':line,'column':column,
            'end_column':max(column+1,len(snippet)+1),'severity':'error','message':str(error),'snippet':snippet}
