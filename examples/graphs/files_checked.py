"""Files travel as paths, and what a node sends is checked.

    python examples/graphs/files_checked.py

Three nodes work in one directory. The first writes a table and sends its path. The second is careless: the
first time it names a chart it has not drawn; it is told so and run again, and then it draws the chart. The
third only reads what arrives. Nothing but paths and a few numbers travels along the edges.
"""
import tempfile
from pathlib import Path

from indeterminate.base import GraphBuilder, Port
from indeterminate.engine import Engine
from indeterminate.nodes import FunctionNode
from indeterminate.protocols import DEFAULT, FileOf, with_files


def build(work: Path):
    def measure(n):
        (work / "data").mkdir(exist_ok=True)
        (work / "data" / "table.csv").write_text("x,y\n" + "".join(f"{i},{i * i}\n" for i in range(n)), newline="\n")
        return "data/table.csv"

    def draw(ctx, table):
        if ctx.problems:                       # told what was wrong the first time: now do the work
            rows = (work / table["path"]).read_text().splitlines()[1:]
            (work / "chart.svg").write_text(f"<svg><!-- {len(rows)} points --></svg>")
        return "chart.svg"

    def report(chart, table):
        return f"{chart['path']} ({chart['size']} bytes) drawn from {table['path']} ({table['size']} bytes)"

    return (GraphBuilder()
            .add(FunctionNode("measure", measure, inputs=[Port("n", int)], outputs=[Port("table", FileOf(".csv"))]),
                 FunctionNode("draw", draw, context=True, inputs=[Port("table")],
                              outputs=[Port("chart", FileOf(".svg", max_size=10_000), repairs=1)]),
                 FunctionNode("report", report, inputs=[Port("chart"), Port("table")], outputs=[Port("line", str)]))
            .connect("measure.table", "draw.table", "file").connect("measure.table", "report.table", "file")
            .connect("draw.chart", "report.chart", "file")
            .input("in", "measure.n").output("out", "report.line"))


def main(work=None):
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(work or temporary)
        protocols = with_files(DEFAULT, work)
        result = Engine(build(work).compile(protocols), protocols).run({"in": 5})
    print(result.last("out"))
    for problem in result.problems:
        print(f"  {problem['node']}.{problem['port']} was {problem['handled']}: {problem['problem']}")
    return result


if __name__ == "__main__":
    main()
