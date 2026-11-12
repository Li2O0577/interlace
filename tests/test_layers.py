"""The kernel's layers: each module imports only from its own layer or below.

    4 interface      kernel, extensions, mcp
    3 orchestration  graph (scheduler, run, store, ...), agents runtime, tools
    2 entities       plan, agents.kinds, agents.state, agents.records, graph.nodes
    1 foundation     core, host, agents.providers
"""
import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / 'src' / 'indeterminate'

# module -> the kernel modules it may import (anything else from indeterminate is a layer violation)
ENTITIES = {
    'plan': {'agents.kinds'},
    'agents.kinds': set(),
    'agents.state': set(),
    'agents.records': {'core.jsonfile', 'agents.state'},
    'graph.nodes': {'plan'},
}
INTERFACE = ('kernel', 'extensions', 'mcp')


def _module(path):
    return '.'.join(path.relative_to(SRC).with_suffix('').parts).removesuffix('.__init__')


def _imports(path):
    """Kernel modules a file imports (relative imports resolved), at any depth of the file."""
    name = _module(path)
    package = name.split('.')[:-1] if path.name != '__init__.py' else name.split('.')
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)] if node.level > 1 else package
                target = '.'.join(base + ([node.module] if node.module else []))
                targets = [target] if node.module else ['.'.join(base + [a.name]) for a in node.names]
            elif node.module and node.module.startswith('indeterminate'):
                targets = [node.module.removeprefix('indeterminate').lstrip('.')]
            else:
                continue
            for t in targets:
                for alias in node.names:   # "from .agents import kinds" names a module
                    if (SRC / Path(*(t.split('.') + [alias.name]))).with_suffix('.py').exists():
                        found.add(f'{t}.{alias.name}'.lstrip('.'))
                        break
                else:
                    found.add(t)
        elif isinstance(node, ast.Import):
            found |= {a.name.removeprefix('indeterminate').lstrip('.') for a in node.names if a.name.startswith('indeterminate')}
    return {f for f in found if f}


@pytest.mark.parametrize('module', sorted(ENTITIES))
def test_entity_modules_depend_only_on_what_they_declare(module):
    path = SRC / Path(*module.split('.'))
    path = path.with_suffix('.py') if path.with_suffix('.py').exists() else path / '__init__.py'
    assert _imports(path) <= ENTITIES[module]


def test_nothing_below_the_interface_imports_it():
    offenders = []
    for path in SRC.rglob('*.py'):
        name = _module(path)
        if name.startswith(INTERFACE) or name == '':
            continue
        bad = {m for m in _imports(path) if m.startswith(INTERFACE)}
        if bad:
            offenders.append((name, sorted(bad)))
    assert offenders == []
