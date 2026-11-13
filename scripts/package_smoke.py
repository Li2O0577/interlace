"""Run against an installed wheel, from a directory that is not the source tree: the package imports, its
public names resolve, a graph runs, and the examples run.

    python scripts/package_smoke.py examples/graphs
"""
import runpy
import sys
from pathlib import Path

import indeterminate
from indeterminate import DEFAULT, Chain, Engine, FunctionNode, Port

source = Path(__file__).resolve().parents[1] / "src"
assert source not in Path(indeterminate.__file__).resolve().parents, "the source tree is being imported, not the wheel"
for name in indeterminate.__all__:
    getattr(indeterminate, name)

node = lambda id, f: FunctionNode(id, f, inputs=[Port("x")], outputs=[Port("y")])  # noqa: E731
assert Engine(Chain(node("a", lambda x: x + 1), node("b", lambda x: x * 2)).build(DEFAULT), DEFAULT).run({"in": 1}).last("out") == 4

examples = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(examples))
for example in sorted(examples.glob("*.py")):
    runpy.run_path(str(example), run_name="__main__")
print(f"indeterminate {indeterminate.__version__}: {len(indeterminate.__all__) - 1} public names, {len(list(examples.glob('*.py')))} examples ran")
