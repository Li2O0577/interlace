"""Pure eligibility calculation. Reserved records are not model instances."""
from __future__ import annotations
from ..plan import LEAD


def ready(plan, run):
    if not run.get('confirmed'):
        return []
    result = []
    blocks = {b['id']:b for b in plan['blocks']}
    for node in plan['nodes']:
        key = node['id']
        if key == LEAD or key in run.get('input_holds', {}):
            continue
        current = run.get('instances', {}).get(run.get('by_node', {}).get(key))
        if current and not (current['status'] == 'ended' and key in run.get('wake_pending', {})):
            continue
        block = blocks[node['block']]
        if any(not run['blocks'][prior]['released'] for prior in block['after']):
            continue
        if any(run.get('submissions', {}).get(e['from'], {}).get('status') != 'submitted'
               or e['from'] in run.get('input_holds', {})
               for e in plan['edges'] if e['to'] == key and e['start']):
            continue
        result.append(key)
    return result
