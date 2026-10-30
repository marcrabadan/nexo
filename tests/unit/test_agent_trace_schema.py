"""The trace profile rejects payload attributes and invalid OTLP identifiers."""
import copy
import json
from pathlib import Path
import pytest
from otlp_profile import validate

SCHEMA=json.loads((Path(__file__).resolve().parents[2]/'docs/schemas/agent-trace-v1.json').read_text())
SPAN=SCHEMA['properties']['resourceSpans']['items']['properties']['scopeSpans']['items']['properties']['spans']['items']


@pytest.mark.parametrize('mutation',['zero_trace','short_span','message','attributes','string_enum','numeric_time'])
def test_trace_schema_rejects_invalid_or_sensitive_span(mutation):
    value={'traceId':'a'*32,'spanId':'b'*16,'name':'nexo.agent.run','kind':1,
           'startTimeUnixNano':'100','endTimeUnixNano':'200','status':{'code':1}}
    validate(value,SPAN)
    if mutation=='zero_trace':value['traceId']='0'*32
    elif mutation=='short_span':value['spanId']='b'
    elif mutation=='message':value['status']['message']='secret'
    elif mutation=='attributes':value['attributes']=[{'key':'prompt','value':{'stringValue':'secret'}}]
    elif mutation=='string_enum':value['kind']='SPAN_KIND_INTERNAL'
    else:value['startTimeUnixNano']=100
    with pytest.raises(AssertionError):validate(value,SPAN)
