"""The third-party example in examples/extension: imports only indeterminate, and its custom kind and
tool stay inside the node boundary. SIMULATION: scripted fake models, no model is called."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'extension'
KERNEL = ROOT / 'src'

PROBE = r'''
import importlib.abc, json, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('fastapi','uvicorn','starlette','pypdf','openpyxl','psutil'):
            raise RuntimeError('the example reached outside the kernel: '+fullname)
sys.meta_path.insert(0, Block())
sys.path.insert(0, sys.argv[1])
import run_simulation

def inspect(kernel, pid):
    m = kernel.manager
    st = m._get(pid, kernel.manager.graph(pid).run.state()['instances']['proofreader:r1']['handle'])
    def attempt(name, args):
        content, error = m._execute(st, {'id': 'probe', 'name': name, 'args': args})
        return bool(error)
    w = 'nodes/proofreader/r1/work/'
    return {
        'kind': m.kind(st).__name__, 'bases': [c.__name__ for c in m.kind(st).__mro__],
        'other_kernel_knows_role': 'proofreader' in __import__('indeterminate.agents.kinds', fromlist=['DEFAULT']).DEFAULT,
        'refused_unlisted_input': attempt('review.count_lines', {'path': 'material/source.md', 'out': w + 'x.txt'}),
        'refused_write_elsewhere': attempt('review.count_lines', {'path': 'nodes/drafter/r1/work/summary.md', 'out': 'nodes/drafter/r1/work/x.txt'}),
        'refused_state_read': attempt('review.count_lines', {'path': 'state/graph.json', 'out': w + 'y.txt'}),
        'allowed_own_input': not attempt('review.count_lines', {'path': 'nodes/drafter/r1/work/summary.md', 'out': w + 'z.txt'}),
    }

print('RESULT' + json.dumps(run_simulation.main(__import__('pathlib').Path(sys.argv[2]), inspect), ensure_ascii=False))
'''


def test_example_runs_with_the_kernel_alone_and_keeps_its_boundaries(tmp_path):
    env = {**os.environ, 'PYTHONPATH': str(KERNEL)}
    out = subprocess.run([sys.executable, '-X', 'utf8', '-c', PROBE, str(EXAMPLE), str(tmp_path)], capture_output=True,
                         text=True, encoding='utf-8', env=env, cwd=tmp_path, timeout=120)
    assert out.returncode == 0, out.stderr[-3000:]
    result = json.loads(out.stdout.split('RESULT', 1)[1])
    assert result['lines'].endswith('3 行') and result['released'] == ['draft']
    assert 'nodes/proofreader/r1/work/lines.txt' in result['artifacts']
    seen = result['inspected']
    assert seen['kind'] == 'Proofreader' and 'Reviewer' in seen['bases'] and 'Agent' in seen['bases']
    assert not seen['other_kernel_knows_role']
    assert seen['refused_unlisted_input'] and seen['refused_write_elsewhere'] and seen['refused_state_read']
    assert seen['allowed_own_input']
