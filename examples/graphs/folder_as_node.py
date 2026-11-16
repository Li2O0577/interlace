"""A whole folder as one node: examples/apps/word_count is an application with a node.json beside it.

    python examples/graphs/folder_as_node.py

The application is started in its folder for each activation, reads the inputs as one JSON object and
answers with one. It could be any program in any language, of any size; the graph sees only its ports.
"""
from pathlib import Path

from interlacing import FolderNode, run

APPS = Path(__file__).resolve().parents[1] / "apps"


def main():
    count = FolderNode(APPS / "word_count")
    print(sorted(count.inputs), sorted(count.outputs))
    result = run(count, {"text": "the cat and the dog and the bird", "top": 2})
    print(result.last("common"), result.last("total"))
    return result


if __name__ == "__main__":
    main()
