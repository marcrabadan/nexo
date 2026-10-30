#!/usr/bin/env python3
"""Create paired FIXTURE-ONLY evaluation input, not AI benchmark results."""
import hashlib
import json
from pathlib import Path
import argparse

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]

def fixture():
    tasks=json.loads((HERE/'source/examples.json').read_text())
    index=json.loads((HERE/'versions.json').read_text())
    current=next(v for v in index['versions'] if v['revision']==index['current'])
    prompts=json.loads((ROOT/'docs'/current['bundle']/'prompts.json').read_text())['prompts']
    records=[]
    for task in tasks['examples']:
        for arm in ('guide','without-guide'):
            records.append({'task':task['id'],'arm':arm,
                'model_configuration':{'fixture_only':True,'model':'none','temperature':0,'seed':0},
                'candidate':str((ROOT/task['path']).resolve()),
                'iterations':[{'input':task['purpose']+('\n'+prompts['developer-compact-offline']['text'] if arm=='guide' else ''),
                               'output':(ROOT/task['path']).read_text()}]})
    return {'schema_version':1,'fixture_only':True,'guide_revision':index['current'],
        'task_catalog_sha256':hashlib.sha256((HERE/'source/examples.json').read_bytes()).hexdigest(),'records':records}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path);args=parser.parse_args()
    args.output.write_text(json.dumps(fixture(),indent=2)+'\n')
