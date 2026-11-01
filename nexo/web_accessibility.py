"""Bounded source checks; real rendered accessibility still needs browser testing."""
from . import ast as A
from .web_codegen import members


def text(expr):
    if isinstance(expr,A.Str) and all(isinstance(p,str) for p in expr.parts):return ''.join(expr.parts)
    return None


def has_text(node):
    if node.text is not None:
        value=text(node.text)
        if value is None or value.strip():return True
    if node.tag[0].isupper() or '.' in node.tag:return True # Unknown component output is not assumed empty.
    return any(has_text(child) for child in node.children if child.tag not in ('input','br','hr'))


def findings(declarations):
    out=[]
    for declaration in declarations:
        nodes=list(members(declaration.view))
        ids={text(dict(n.attrs).get('id')):n for n in nodes if text(dict(n.attrs).get('id'))}
        labels={text(dict(n.attrs).get('for')) for n in nodes if n.tag=='label' and has_text(n) and text(dict(n.attrs).get('for'))}
        wrapped={id(child) for n in nodes if n.tag=='label' and has_text(n) for child in members(n.children)}
        def name(node):
            attrs=dict(node.attrs)
            if 'aria_label' in attrs:
                value=text(attrs['aria_label'])
                if value is None or value.strip():return True
            references=text(attrs.get('aria_labelledby'))
            if references and all(key in ids and has_text(ids[key]) for key in references.split()):return True
            if node.tag=='input':
                return id(node) in wrapped or text(attrs.get('id')) in labels or text(attrs.get('type')) in ('hidden','submit','reset')
            return has_text(node)
        def add(node,code,message,fix):
            out.append({'file':str(declaration.unit.path),'line':node.line,'column':1,'code':code,'severity':'warning',
                        'message':message+' Fix: '+fix,'snippet':'[redacted: source audit location]'})
        for node in nodes:
            attrs=dict(node.attrs)
            if node.tag in ('button','a','input') and not name(node):
                add(node,'NW5101','Interactive element has no statically available accessible name.','provide meaningful text, aria_label/aria_labelledby, or an associated input label.')
            tab=attrs.get('tabindex');positive=isinstance(tab,A.Num) and not tab.is_float and int(tab.text)>0
            value=text(tab)
            if value and value.lstrip('+').isdigit():positive=int(value)>0
            if positive:add(node,'NW5102','Positive tabindex changes the natural keyboard order.','use native focus order, 0 or -1.')
            if node.tag in ('button','a','input') and text(attrs.get('aria_hidden'))=='true':
                add(node,'NW5103','Interactive element is hidden from the accessibility tree.','remove aria_hidden from interactive controls.')
    return out
