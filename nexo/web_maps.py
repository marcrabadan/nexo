"""Deterministic Source Map v3 line mappings for compiler-generated JS."""

ALPHABET='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'


def vlq(value):
    number=((-value)<<1|1) if value<0 else value<<1
    out=''
    while True:
        digit=number&31;number>>=5
        out+=ALPHABET[digit|(32 if number else 0)]
        if not number:return out


def source_map(program,origins):
    from .modules import project_root
    root=project_root(str(program.root.path)) if program.root.path else None
    units={u.key:u for u in program.units}
    keys=[u.key for u in program.units]
    source=previous_line=0;segments=[]
    for origin in origins:
        if origin is None:segments.append('');continue
        key,line=origin;index=keys.index(key)
        segments.append(vlq(0)+vlq(index-source)+vlq(line-1-previous_line)+vlq(0))
        source=index;previous_line=line-1
    names=[]
    for key in keys:
        unit=units[key]
        name=unit.path.relative_to(root).as_posix() if root and unit.path else (key.replace('.','/') or 'app')+'.nexo'
        names.append('nexo://project/'+name)
    return {'version':3,'file':'app.js','sources':names,'sourcesContent':[units[k].source for k in keys],
            'names':[],'mappings':';'.join(segments)}


def css_source_map(program,origins):
    names=list(program.css_sources);source=previous_line=0;segments=[]
    for origin in origins:
        if origin is None:segments.append('');continue
        name,line=origin;index=names.index(name)
        segments.append(vlq(0)+vlq(index-source)+vlq(line-1-previous_line)+vlq(0))
        source=index;previous_line=line-1
    return {'version':3,'file':'app.css','sources':names,'sourcesContent':list(program.css_sources.values()),'names':[],'mappings':';'.join(segments)}
