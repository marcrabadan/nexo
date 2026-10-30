"""Test-only walker for the closed JSON Schema subset used by our OTLP profile."""
import re


def validate(value,schema):
    if 'const' in schema: assert type(value) is type(schema['const']) and value==schema['const']
    if 'enum' in schema: assert any(type(value) is type(v) and value==v for v in schema['enum'])
    kind=schema.get('type')
    if kind=='object':
        assert isinstance(value,dict)
        assert set(schema.get('required',[]))<=set(value)
        if schema.get('additionalProperties') is False: assert set(value)<=set(schema['properties'])
        for key,child in value.items(): validate(child,schema['properties'][key])
    elif kind=='array':
        assert isinstance(value,list)
        assert schema.get('minItems',0)<=len(value)<=schema.get('maxItems',len(value))
        for child in value: validate(child,schema['items'])
    elif kind=='string':
        assert isinstance(value,str)
        if 'pattern' in schema: assert re.search(schema['pattern'],value)
