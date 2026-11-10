"""The kernel is its own distribution: standard library only, and never the application."""
import ast
import os
from pathlib import Path
import subprocess
import sys

SRC = Path(__file__).resolve().parents[1] / 'src'
PACKAGE = SRC / 'mch_harness'
BLOCK = '''
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] in ('mch','fastapi','uvicorn','starlette','pydantic','pypdf','openpyxl','psutil',
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


def test_no_module_imports_the_application_or_a_third_party_package():
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
                elif top and top != 'mch_harness' and top not in sys.stdlib_module_names:
                    failures.append(f'{path.relative_to(SRC)}:{line}: imports {name}')
    assert not failures, '\n'.join(failures)


def test_no_source_names_an_application_module_path():
    """Subprocess commands and dynamic imports are strings: none may point back at the application."""
    hits = []
    for path in PACKAGE.rglob('*.py'):
        for number, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if any(mark in text for mark in ('"mch.', "'mch.", 'import mch\n', '-m mch ', 'from mch ')):
                hits.append(f'{path.relative_to(SRC)}:{number}: {text.strip()[:100]}')
    assert not hits, '\n'.join(hits)


def run(script, cwd):
    return subprocess.run([sys.executable, '-X', 'utf8', '-c', BLOCK + script], capture_output=True, text=True,
                          encoding='utf-8', cwd=cwd, timeout=30,
                          env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'} | {'PYTHONPATH': str(SRC)})


def test_engine_imports_with_the_application_and_its_dependencies_blocked(tmp_path):
    result = run('''
import mch_harness
from mch_harness import Kernel, HostAdapters, Extension, TaskType
from mch_harness.agents.runtime import AgentManager
from mch_harness.mcp.daemon import Engine
from mch_harness.mcp.server import Server
from mch_harness.mcp import cli
import mch_harness.mcp_bridge
from mch_harness.tools import registry
registry.load_generic()
assert {'fs.read','fs.write','graph.view','graph.publish'} <= registry.TOOLS.keys()
assert not [m for m in sys.modules if m == 'mch' or m.startswith('mch.')]
print('kernel imported', mch_harness.__version__)
''', tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'kernel imported' in result.stdout


def test_default_roles_and_tools_are_task_neutral(tmp_path):
    result = run('''
from mch_harness.agents import role_catalog, specs
from mch_harness.tools import registry
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
