"""The kernel is its own distribution: standard library only."""
import ast
import os
from pathlib import Path
import subprocess
import sys

SRC = Path(__file__).resolve().parents[1] / 'src'
PACKAGE = SRC / 'indeterminate'
BLOCK = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('fastapi','uvicorn','starlette','pydantic','pypdf','openpyxl','psutil',
                                      'numpy','pandas','matplotlib'):
            raise RuntimeError('the kernel reached outside itself: '+fullname)
sys.meta_path.insert(0,Block())
'''


def imported(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield node.lineno, 0, [n.name for n in node.names]
        elif isinstance(node, ast.ImportFrom):
            yield node.lineno, node.level, [node.module or '']


def test_no_module_imports_a_third_party_package():
    """Every import statement in the package, including the ones inside functions."""
    failures = []
    for path in PACKAGE.rglob('*.py'):
        depth = len(path.relative_to(PACKAGE).parts)        # how many dots reach the package root
        for line, level, names in imported(ast.parse(path.read_text(encoding='utf-8'))):
            for name in names:
                top = name.split('.')[0]
                if level:
                    if level > depth:
                        failures.append(f'{path.relative_to(SRC)}:{line}: relative import leaves the package')
                elif top and top != 'indeterminate' and top not in sys.stdlib_module_names:
                    failures.append(f'{path.relative_to(SRC)}:{line}: imports {name}')
    assert not failures, '\n'.join(failures)


def run(script, cwd):
    return subprocess.run([sys.executable, '-X', 'utf8', '-c', BLOCK + script], capture_output=True, text=True,
                          encoding='utf-8', cwd=cwd, timeout=30,
                          env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'} | {'PYTHONPATH': str(SRC)})


def test_engine_imports_with_third_party_packages_blocked(tmp_path):
    result = run('''
import indeterminate
from indeterminate import Kernel, HostAdapters, Extension, TaskType
from indeterminate.agents.runtime import AgentManager
from indeterminate.mcp.daemon import Engine
from indeterminate.mcp.server import Server
from indeterminate.mcp import cli
import indeterminate.mcp_bridge
from indeterminate.tools import registry
registry.load_generic()
assert {'fs.read','fs.write','graph.view','graph.publish'} <= registry.TOOLS.keys()
print('kernel imported', indeterminate.__version__)
''', tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'kernel imported' in result.stdout


def test_default_roles_and_tools_are_task_neutral(tmp_path):
    result = run('''
from indeterminate.agents import role_catalog, specs
from indeterminate.tools import registry
registry.load_generic()
roles = set(role_catalog.ROLE_TOOLS)
assert roles == {'file_keeper','lead','worker','reviewer','coder','analyst','researcher','writer','sublead'}, roles
spawn = specs.AGENT_TOOLS['agent.spawn']['parameters']['properties']
assert 'question' not in spawn and set(spawn['role']['enum']) <= roles
names = set(registry.TOOLS)
assert all(n.split('.')[0] in ('fs','code','graph','access','lineage') for n in names), names
print('neutral')
''', tmp_path)
    assert result.returncode == 0, result.stderr
