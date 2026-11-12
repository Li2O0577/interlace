"""Run with a clean environment's Python after installing ONLY the 1ndeterminate wheel.

Checks that the kernel works on its own: no web framework, no source checkout.
SIMULATION only: the stdio demo and the example use scripted fake models.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--example', help='path to examples/harness_extension, to run it against the installed kernel')
    args = parser.parse_args()
    import indeterminate
    assert 'site-packages' in str(Path(indeterminate.__file__).resolve()), 'smoke must import the installed wheel'
    for absent in ('fastapi', 'uvicorn'):
        assert importlib.util.find_spec(absent) is None, f'{absent} must not be installed with the kernel alone'
    for name in indeterminate.__all__[1:]:
        getattr(indeterminate, name)
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    with tempfile.TemporaryDirectory(prefix='1ndeterminate-smoke-') as temp:
        base = Path(temp)
        cli = subprocess.run([sys.executable, '-m', 'indeterminate.mcp.cli', '--help'], capture_output=True, text=True,
                             encoding='utf-8', cwd=base, env=env, timeout=20)
        assert cli.returncode == 0, cli.stderr
        requests = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-11-25',
                     'capabilities': {}, 'clientInfo': {'name': 'kernel-smoke', 'version': '1'}}},
                    {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                    {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                    {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'project.create',
                     'arguments': {'request_id': 'kernel-smoke', 'name': 'Kernel smoke', 'task': 'Inspect material'}}}]
        served = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'indeterminate.mcp.cli', '--workspace', str(base / 'w'),
                                 '--home', str(base / 'h'), 'serve', '--demo'],
                                input='\n'.join(json.dumps(r) for r in requests) + '\n', capture_output=True, text=True,
                                encoding='utf-8', cwd=base, env=env, timeout=30)
        assert served.returncode == 0, served.stderr
        replies = [json.loads(line) for line in served.stdout.splitlines()]
        assert len(replies) == 3 and len(replies[1]['result']['tools']) == 8
        assert replies[2]['result']['structuredContent']['runtime_mode'] == 'simulation'
        print('Installed kernel alone: stdio MCP + FakeProvider draft passed (simulation)')
        if args.example:
            ran = subprocess.run([sys.executable, '-X', 'utf8', str(Path(args.example) / 'run_simulation.py'), str(base / 'ex')],
                                 capture_output=True, text=True, encoding='utf-8', cwd=base, env=env, timeout=120)
            assert ran.returncode == 0, ran.stderr[-3000:]
            result = json.loads(ran.stdout)
            assert result['released'] == ['draft'] and result['lines'].endswith('3 行')
            print('Installed kernel alone: third-party extension example passed (simulation)')


if __name__ == '__main__':
    main()
